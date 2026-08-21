"""Position lifecycle detection and TP/SL extraction.

Spec §36: "LONG position detection", "SHORT position detection". Spec §8/§9:
open / increase / decrease / close / flip. Spec §11 and §35: TP/SL are shown
only when a real resting trigger order exists.
"""

from __future__ import annotations

from app.utils.formatting import Confidence
from app.whale.detector import WhaleDetector, extract_tpsl
from app.whale.events import EventType, ValueKind
from tests.factories import (
    BTC_PX,
    WALLET_A,
    make_context,
    make_open_order,
    make_position,
    make_stop_loss,
    make_take_profit,
    price_map,
)

PRICES = price_map()


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


# ── lifecycle classification ───────────────────────────────────

def test_position_opened():
    after = make_position(szi=60.0)
    event = detector().from_position_change(WALLET_A, "BTC", None, after, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_OPENED
    assert event.side == "LONG"
    assert event.detection == "Position opened"


def test_short_position_opened():
    after = make_position(szi=-60.0)
    event = detector().from_position_change(WALLET_A, "BTC", None, after, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_OPENED
    assert event.side == "SHORT"


def test_position_increased_is_measured_by_the_delta_not_the_whole_position():
    """§7: a $1M add to a $50M position is a $1M event."""
    before = make_position(szi=500.0)      # $50M at mark
    after = make_position(szi=510.0)       # +10 BTC = $1M
    event = detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_INCREASED
    assert event.value_kind is ValueKind.POSITION_DELTA
    assert event.notional == 10.0 * BTC_PX
    assert event.threshold_class == "position_delta"


def test_position_decreased():
    before = make_position(szi=60.0)
    after = make_position(szi=40.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_DECREASED
    assert event.notional == 20.0 * BTC_PX
    assert event.value("size_delta") == -20.0


def test_position_closed_reports_the_full_position_notional():
    before = make_position(szi=60.0, position_value=6_000_000.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, None, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_CLOSED
    assert event.value_kind is ValueKind.POSITION_NOTIONAL
    assert event.notional == 6_000_000.0
    assert event.threshold_class == "position"
    assert event.value("closed_position_value") == 6_000_000.0


def test_position_flipped_long_to_short():
    before = make_position(szi=60.0)
    after = make_position(szi=-40.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.POSITION_FLIPPED
    assert event.side == "SHORT"
    assert event.notional == 100.0 * BTC_PX   # the whole 100 BTC changed hands


def test_unchanged_position_produces_nothing():
    position = make_position(szi=60.0)
    assert detector().from_position_change(WALLET_A, "BTC", position, position, min_notional=0) is None


def test_dust_change_produces_nothing():
    before = make_position(szi=60.0)
    after = make_position(szi=60.0 + 1e-15)
    assert detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=0) is None


def test_position_delta_below_threshold_is_rejected():
    before = make_position(szi=500.0)
    after = make_position(szi=501.0)          # $100k
    assert (
        detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=2_000_000)
        is None
    )


def test_closed_position_reports_no_open_position_fields():
    """After a close there is no position, and the event says so."""
    before = make_position(szi=60.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, None, min_notional=0)
    assert event is not None
    assert event.point("entry_px").confidence is Confidence.UNAVAILABLE
    assert event.point("entry_px").note == "no open position for this coin"


def test_delta_value_is_labelled_estimated():
    before = make_position(szi=60.0)
    after = make_position(szi=80.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, after, min_notional=0)
    assert event is not None
    point = event.point("delta_value")
    assert point.confidence is Confidence.ESTIMATED
    assert point.note == "|size delta| × mark price"


def test_position_event_records_its_rest_source():
    event = detector().from_position_change(
        WALLET_A, "BTC", None, make_position(szi=60.0), min_notional=0
    )
    assert event is not None
    assert event.context["source"] == "rest:clearinghouseState"
    assert event.is_position_event is True


# ── margin vs notional ─────────────────────────────────────────

def test_margin_and_position_value_are_separate_fields():
    """§7: margin is not the position notional and must not be conflated."""
    position = make_position(szi=60.0, position_value=6_000_000.0, margin_used=1_200_000.0)
    event = detector().from_position_change(
        WALLET_A, "BTC", None, position, context=make_context(position=position), min_notional=0
    )
    assert event is not None
    assert event.value("position_value") == 6_000_000.0
    assert event.value("margin_used") == 1_200_000.0
    assert event.value("position_value") != event.value("margin_used")


# ── TP / SL extraction ─────────────────────────────────────────

def test_no_trigger_orders_means_no_tpsl_detected():
    tp, sl = extract_tpsl([], "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.UNAVAILABLE
    assert sl.confidence is Confidence.UNAVAILABLE
    assert tp.note == "no resting trigger order detected"


def test_labelled_take_profit_and_stop_are_confirmed():
    orders = [make_take_profit(115_000.0), make_stop_loss(88_000.0)]
    tp, sl = extract_tpsl(orders, "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.CONFIRMED
    assert tp.value == 115_000.0
    assert sl.confidence is Confidence.CONFIRMED
    assert sl.value == 88_000.0


def test_plain_limit_order_is_not_treated_as_a_tp():
    """A resting limit far from the mark is not a take-profit."""
    orders = [make_open_order(limit_px=130_000.0, side="A", order_type="Limit")]
    tp, sl = extract_tpsl(orders, "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.UNAVAILABLE
    assert sl.confidence is Confidence.UNAVAILABLE


def test_unrecognised_trigger_type_is_estimated_not_confirmed():
    orders = [
        make_open_order(
            limit_px=118_000.0,
            side="A",
            order_type="Some Future Trigger Type",
            is_trigger=True,
            trigger_px=118_000.0,
        )
    ]
    tp, sl = extract_tpsl(orders, "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.ESTIMATED
    assert "inferred from price" in (tp.note or "")
    assert sl.confidence is Confidence.UNAVAILABLE


def test_nearest_of_several_take_profits_is_reported_with_a_count():
    orders = [make_take_profit(110_000.0), make_take_profit(130_000.0)]
    tp, _sl = extract_tpsl(orders, "BTC", "LONG", BTC_PX)
    assert tp.value == 110_000.0                 # fires first
    assert "2 resting TP" in (tp.note or "")


def test_short_position_take_profit_is_below_the_mark():
    orders = [make_take_profit(85_000.0), make_stop_loss(112_000.0)]
    tp, sl = extract_tpsl(orders, "BTC", "SHORT", BTC_PX)
    assert tp.value == 85_000.0
    assert sl.value == 112_000.0


def test_trigger_orders_for_another_coin_are_ignored():
    orders = [make_take_profit(115_000.0)]
    orders[0].coin = "ETH"
    tp, _sl = extract_tpsl(orders, "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.UNAVAILABLE


def test_nested_child_trigger_orders_are_found():
    parent = make_open_order(limit_px=99_000.0, children=[make_take_profit(120_000.0)])
    tp, _sl = extract_tpsl([parent], "BTC", "LONG", BTC_PX)
    assert tp.confidence is Confidence.CONFIRMED
    assert tp.value == 120_000.0


def test_orders_never_fetched_is_distinct_from_no_tpsl_set():
    """"We did not look" must not render as "the trader has no stop"."""
    ctx = make_context(position=make_position(szi=60.0), orders_known=False)
    event = detector().from_position_change(
        WALLET_A, "BTC", None, make_position(szi=60.0), context=ctx, min_notional=0
    )
    assert event is not None
    assert event.point("take_profit_px").note == "trigger orders not fetched for this wallet"

    ctx_known = make_context(position=make_position(szi=60.0), orders_known=True)
    event2 = detector().from_position_change(
        WALLET_A, "BTC", None, make_position(szi=60.0), context=ctx_known, min_notional=0
    )
    assert event2 is not None
    assert event2.point("take_profit_px").note == "no resting trigger order detected"


def test_confirmed_tpsl_reaches_the_event():
    ctx = make_context(
        position=make_position(szi=60.0),
        trigger_orders=[make_take_profit(115_000.0), make_stop_loss(88_000.0)],
    )
    event = detector().from_position_change(
        WALLET_A, "BTC", None, make_position(szi=60.0), context=ctx, min_notional=0
    )
    assert event is not None
    assert event.value("take_profit_px") == 115_000.0
    assert event.value("stop_loss_px") == 88_000.0
    assert event.db_fields()["take_profit_px"] == 115_000.0
