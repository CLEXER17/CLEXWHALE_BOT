"""Trade detection: threshold, LONG, SHORT, attribution, no fabrication.

Spec §36 items covered here: "Whale threshold detection", "LONG position
detection", "SHORT position detection".
"""

from __future__ import annotations

from app.utils.formatting import Confidence
from app.whale.detector import WhaleDetector
from app.whale.events import EventType, ValueKind
from tests.factories import (
    BTC_PX,
    WALLET_A,
    WALLET_B,
    make_context,
    make_position,
    make_trade,
    price_map,
)

PRICES = price_map()


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


# ── threshold ──────────────────────────────────────────────────

def test_trade_below_threshold_is_not_an_event():
    trade = make_trade(px=BTC_PX, sz=1.0)  # $100k
    assert detector().from_trade(trade, min_notional=2_000_000) is None


def test_trade_at_threshold_is_an_event():
    trade = make_trade(px=BTC_PX, sz=20.0)  # exactly $2.0M
    event = detector().from_trade(trade, min_notional=2_000_000)
    assert event is not None
    assert event.notional == 2_000_000.0


def test_trade_notional_is_price_times_size():
    event = detector().from_trade(make_trade(px=BTC_PX, sz=37.5), min_notional=0)
    assert event is not None
    assert event.notional == BTC_PX * 37.5


def test_trade_uses_the_trade_value_kind_not_position_notional():
    """§7: a $3M market buy is a cash flow, not a position value."""
    event = detector().from_trade(make_trade(sz=30.0), min_notional=0)
    assert event is not None
    assert event.value_kind is ValueKind.TRADE_VALUE
    assert event.threshold_class == "trade"


# ── direction ──────────────────────────────────────────────────

def test_buyer_aggressor_is_a_buy_for_the_taker():
    event = detector().from_trade(make_trade(side="B"), min_notional=0)
    assert event is not None
    assert event.wallet == WALLET_A          # the buyer took the trade
    assert event.value("trade_side") == "BUY"


def test_seller_aggressor_is_a_sell_for_the_taker():
    event = detector().from_trade(make_trade(side="A"), min_notional=0)
    assert event is not None
    assert event.wallet == WALLET_B          # the seller took the trade
    assert event.value("trade_side") == "SELL"


def test_maker_side_is_the_opposite_of_the_taker_side():
    trade = make_trade(side="B")            # buyer aggressed, so the seller is the maker
    event = detector().from_trade(trade, wallet=WALLET_B, min_notional=0)
    assert event is not None
    assert event.wallet == WALLET_B
    assert event.value("trade_side") == "SELL"
    assert event.value("taker_side") == "BUY"
    assert event.detection == "Large resting order filled"
    assert event.context["role"] == "maker"


def test_a_trade_on_a_long_reports_the_executed_side_not_the_position_side():
    """§10 — an execution is not a position.

    The whale holds a 60 BTC long and buys 30 more. The trade alert reports the
    *executed* side (``BUY``); the position it belongs to is reported separately,
    so a $3M buy that trims a short can never be badged ``SHORT``.
    """
    ctx = make_context(position=make_position(szi=60.0))
    event = detector().from_trade(make_trade(sz=30.0), context=ctx, min_notional=0)
    assert event is not None
    assert event.side == "BUY"
    assert event.value("trade_side") == "BUY"
    assert event.value("position_side") == "LONG"
    assert event.value("position_size") == 60.0


def test_a_sell_on_a_short_still_reports_sell_not_short():
    """§2 — ``SELL EXECUTION ≠ automatically SHORT``."""
    ctx = make_context(position=make_position(szi=-60.0))
    event = detector().from_trade(make_trade(side="A", sz=30.0), context=ctx, min_notional=0)
    assert event is not None
    assert event.side == "SELL"
    assert event.value("position_side") == "SHORT"


def test_event_type_is_whale_trade():
    event = detector().from_trade(make_trade(), min_notional=0)
    assert event is not None
    assert event.event_type is EventType.WHALE_TRADE
    assert event.is_position_event is False
    assert event.is_order_event is False


# ── confidence labelling ───────────────────────────────────────

def test_position_fields_are_confirmed_when_the_account_was_fetched():
    ctx = make_context(position=make_position(szi=60.0, entry_px=98_000.0))
    event = detector().from_trade(make_trade(), context=ctx, min_notional=0)
    assert event is not None
    assert event.confidence("entry_px") is Confidence.CONFIRMED
    assert event.value("entry_px") == 98_000.0
    assert event.confidence("liquidation_px") is Confidence.CONFIRMED
    assert event.confidence("leverage") is Confidence.CONFIRMED


def test_no_position_means_unavailable_never_zero():
    """§34/§35: absent data is labelled, not defaulted to 0."""
    event = detector().from_trade(make_trade(), min_notional=0)
    assert event is not None
    for key in ("position_value", "entry_px", "liquidation_px", "leverage"):
        point = event.point(key)
        assert point.confidence is Confidence.UNAVAILABLE
        assert point.value is None
        assert point.note == "no open position for this coin"


def test_missing_liquidation_price_is_unavailable_not_invented():
    ctx = make_context(position=make_position(liquidation_px=None))
    event = detector().from_trade(make_trade(), context=ctx, min_notional=0)
    assert event is not None
    point = event.point("liquidation_px")
    assert point.confidence is Confidence.UNAVAILABLE
    assert "not returned by Hyperliquid" in (point.note or "")


def test_distance_from_mark_is_labelled_estimated():
    """A derived number must never look like a Hyperliquid-supplied one."""
    event = detector().from_trade(make_trade(px=95_000.0), min_notional=0)
    assert event is not None
    assert event.confidence("distance_pct") is Confidence.ESTIMATED
    assert event.confidence("current_px") is Confidence.CONFIRMED


def test_observed_for_is_estimated_and_says_it_is_not_the_open_time():
    """§6: windows are observation windows, not candle/on-chain timings."""
    from tests.factories import ago

    ctx = make_context(first_seen=ago(600))
    event = detector().from_trade(make_trade(), context=ctx, min_notional=0)
    assert event is not None
    point = event.point("observed_for")
    assert point.confidence is Confidence.ESTIMATED
    assert "first observed" in (point.note or "")
    assert point.value >= 599


def test_price_provider_failure_does_not_break_detection():
    broken = WhaleDetector(lambda coin: (_ for _ in ()).throw(RuntimeError("price feed down")))
    event = broken.from_trade(make_trade(), min_notional=0)
    assert event is not None
    assert event.point("current_px").confidence is Confidence.UNAVAILABLE


def test_trade_context_records_the_source_feed():
    event = detector().from_trade(make_trade(), min_notional=0)
    assert event is not None
    assert event.context["source"] == "ws:trades"
    assert event.context["counterparty"] == WALLET_B
