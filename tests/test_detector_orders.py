"""Resting-order lifecycle detection.

Spec §36: "Limit order detection", "Order cancellation detection". Spec §18: the
full lifecycle (placed / modified / partially filled / filled / cancelled), and
§35: a cancellation must never be guessed.
"""

from __future__ import annotations

from app.utils.formatting import Confidence
from app.whale.detector import WhaleDetector
from app.whale.events import CANCEL_EVENTS, EventType, ValueKind
from tests.factories import (
    BTC_PX,
    WALLET_A,
    make_open_order,
    make_order_state,
    make_order_update,
    price_map,
)

PRICES = price_map()


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


# ── placement ──────────────────────────────────────────────────

def test_new_open_order_update_is_a_placement():
    update = make_order_update(limit_px=95_000.0, sz=40.0)   # $3.8M
    event = detector().from_order_update(WALLET_A, update, None, min_notional=2_000_000)
    assert event is not None
    assert event.event_type is EventType.ORDER_PLACED
    assert event.value_kind is ValueKind.ORDER_NOTIONAL
    assert event.threshold_class == "order"
    assert event.side == "BUY"
    assert event.order_id == update.oid
    assert event.notional == 95_000.0 * 40.0


def test_small_limit_order_is_not_an_event():
    update = make_order_update(limit_px=95_000.0, sz=1.0)    # $95k
    assert detector().from_order_update(WALLET_A, update, None, min_notional=2_000_000) is None


def test_limit_order_from_a_snapshot_is_a_placement():
    order = make_open_order(limit_px=95_000.0, sz=40.0)
    event = detector().from_open_order(WALLET_A, order, None, min_notional=2_000_000)
    assert event is not None
    assert event.event_type is EventType.ORDER_PLACED
    assert event.context["source"] == "rest:frontendOpenOrders"
    assert event.detection == "Large resting order detected"


def test_a_far_away_price_alone_does_not_make_an_order_a_whale():
    """§17: distance from the mark is not a whale criterion — notional is."""
    far_but_small = make_open_order(limit_px=10_000.0, sz=1.0)   # 90% away, $10k
    assert detector().from_open_order(WALLET_A, far_but_small, None, min_notional=2_000_000) is None

    near_and_large = make_open_order(limit_px=99_900.0, sz=40.0)  # 0.1% away, $4M
    event = detector().from_open_order(WALLET_A, near_and_large, None, min_notional=2_000_000)
    assert event is not None


# ── modification / partial fill ────────────────────────────────

def test_price_change_is_a_modification():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    update = make_order_update(oid=previous.oid, limit_px=96_000.0, sz=40.0)
    event = detector().from_order_update(WALLET_A, update, previous, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.ORDER_MODIFIED


def test_size_reduction_while_open_is_a_partial_fill():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    update = make_order_update(oid=previous.oid, limit_px=95_000.0, sz=25.0, orig_sz=40.0)
    event = detector().from_order_update(WALLET_A, update, previous, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.ORDER_PARTIALLY_FILLED
    assert event.value("filled_size") == 15.0
    assert event.value("remaining_notional") == 95_000.0 * 25.0


def test_identical_open_update_is_not_re_reported():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    update = make_order_update(oid=previous.oid, limit_px=95_000.0, sz=40.0)
    assert detector().from_order_update(WALLET_A, update, previous, min_notional=0) is None


def test_size_increase_from_a_snapshot_is_a_modification():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    order = make_open_order(oid=previous.oid, limit_px=95_000.0, sz=60.0)
    event = detector().from_open_order(WALLET_A, order, previous, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.ORDER_MODIFIED


# ── fill / cancel ──────────────────────────────────────────────

def test_filled_status_is_an_order_fill():
    update = make_order_update(status="filled", sz=0.0, orig_sz=40.0, limit_px=95_000.0)
    event = detector().from_order_update(WALLET_A, update, None, min_notional=2_000_000)
    assert event is not None
    assert event.event_type is EventType.ORDER_FILLED


def test_cancelled_order_is_judged_on_its_original_notional():
    """A cancel of a $3.8M order must not be sized by the $0 remaining."""
    update = make_order_update(status="canceled", sz=0.0, orig_sz=40.0, limit_px=95_000.0)
    event = detector().from_order_update(WALLET_A, update, None, min_notional=2_000_000)
    assert event is not None
    assert event.event_type is EventType.ORDER_CANCELLED
    assert event.notional == 95_000.0 * 40.0
    assert event.event_type in CANCEL_EVENTS


def test_rejected_order_is_reported_as_rejected():
    update = make_order_update(status="rejected", sz=40.0, limit_px=95_000.0)
    event = detector().from_order_update(WALLET_A, update, None, min_notional=0)
    assert event is not None
    assert event.event_type is EventType.ORDER_REJECTED


def test_unknown_status_is_ignored_rather_than_guessed():
    update = make_order_update(status="someNewStatusHyperliquidAdded", limit_px=95_000.0, sz=40.0)
    assert detector().from_order_update(WALLET_A, update, None, min_notional=0) is None


def test_triggered_status_is_reported_as_activation():
    update = make_order_update(status="triggered", limit_px=95_000.0, sz=40.0)
    event = detector().from_order_update(WALLET_A, update, None, min_notional=0)
    assert event is not None
    assert event.detection == "Trigger order activated"


# ── disappearance from the book ────────────────────────────────

def test_vanished_order_resolved_as_cancelled():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    event = detector().from_order_disappearance(
        WALLET_A, previous, "canceled", min_notional=2_000_000
    )
    assert event is not None
    assert event.event_type is EventType.ORDER_CANCELLED
    assert event.detection == "Large order cancelled"
    assert event.status == "canceled"
    # A resolved outcome carries no "unresolved" caveat.
    assert event.point("status_note").note is None


def test_vanished_order_resolved_as_filled():
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    event = detector().from_order_disappearance(
        WALLET_A, previous, "filled", min_notional=2_000_000
    )
    assert event is not None
    assert event.event_type is EventType.ORDER_FILLED


def test_unresolved_disappearance_says_so_instead_of_guessing():
    """§35: cancelled and filled are materially different; never invent one."""
    previous = make_order_state(limit_px=95_000.0, size=40.0)
    event = detector().from_order_disappearance(WALLET_A, previous, None, min_notional=2_000_000)
    assert event is not None
    assert event.event_type is EventType.ORDER_MODIFIED
    assert event.event_type not in CANCEL_EVENTS
    assert event.detection == "Large order left the book (outcome unresolved)"
    note = event.point("status_note")
    assert note.confidence is Confidence.UNAVAILABLE
    assert "cancel vs fill unresolved" in (note.note or "")


def test_small_vanished_order_is_ignored():
    previous = make_order_state(limit_px=95_000.0, size=1.0)
    assert (
        detector().from_order_disappearance(WALLET_A, previous, "canceled", min_notional=2_000_000)
        is None
    )


# ── decoration ─────────────────────────────────────────────────

def test_order_event_carries_price_size_and_resting_time():
    order = make_open_order(limit_px=95_000.0, sz=40.0)
    event = detector().from_open_order(WALLET_A, order, None, min_notional=0)
    assert event is not None
    assert event.value("price") == 95_000.0
    assert event.value("size") == 40.0
    assert event.numeric("resting_for") is not None
    assert event.confidence("current_px") is Confidence.CONFIRMED
    assert event.confidence("distance_pct") is Confidence.ESTIMATED


def test_trigger_order_notional_uses_the_trigger_price():
    order = make_open_order(
        limit_px=None, sz=40.0, is_trigger=True, trigger_px=90_000.0, order_type="Stop Market"
    )
    event = detector().from_open_order(WALLET_A, order, None, min_notional=0)
    assert event is not None
    assert event.notional == 90_000.0 * 40.0
    assert event.value("trigger_px") == 90_000.0


def test_order_event_is_classified_as_an_order_event():
    event = detector().from_open_order(WALLET_A, make_open_order(), None, min_notional=0)
    assert event is not None
    assert event.is_order_event is True
    assert event.is_position_event is False


# ── book levels ────────────────────────────────────────────────

def test_large_book_level_has_no_wallet_attribution():
    """§35: ``l2Book`` is aggregated; claiming an owner would be fabrication."""
    from tests.factories import make_book

    book = make_book(bids=[(99_000.0, 40.0)], asks=[(101_000.0, 1.0)])
    events = detector().from_book(book, min_notional=2_000_000)
    assert len(events) == 1
    event = events[0]
    assert event.event_type is EventType.BOOK_LEVEL
    assert event.wallet is None
    point = event.point("wallet_attribution")
    assert point.confidence is Confidence.UNAVAILABLE
    assert "aggregated" in (point.note or "")
    assert event.value_kind is ValueKind.BOOK_LEVEL_NOTIONAL


def test_book_scan_is_capped():
    from tests.factories import make_book

    levels = [(99_000.0 - i * 100, 40.0) for i in range(10)]
    events = detector().from_book(make_book(bids=levels), min_notional=0, max_events=3)
    assert len(events) == 3


def test_book_level_notional_is_price_times_size():
    from tests.factories import make_book

    events = detector().from_book(make_book(bids=[(BTC_PX, 25.0)]), min_notional=0)
    assert events[0].notional == BTC_PX * 25.0
