"""Orders and positions are different objects — the state-separation suite.

Task "POSITION & ORDER STATE FIX" §1: *"An ORDER is NOT a POSITION. NEVER infer
SELL LIMIT = SHORT. Only actual executed position data determines the current
position."* §15 names seven regression tests; they are TEST 1 .. TEST 7 below,
followed by the invariants of :mod:`app.whale.lifecycle`, which is the single
place where "may this event move position state?" is decided.

The renderer is exercised through :meth:`AlertService.render`, which reads only
the event it is handed — so these tests need no database, bot or settings.
"""

from __future__ import annotations

from app.services.alert_service import AlertService
from app.utils.formatting import Confidence, DataPoint
from app.whale.detector import PositionContext, WhaleDetector
from app.whale.events import EventType, ValueKind, WhaleEvent
from app.whale.lifecycle import (
    LIVE_ORDER_STATES,
    TERMINAL_ORDER_STATES,
    OrderStatus,
    PositionStatus,
    can_order_transition,
    can_position_transition,
    has_verified_position,
    may_modify_position,
    order_status_of,
    position_status_of,
)
from tests.factories import (
    BTC_PX,
    WALLET_A,
    make_context,
    make_open_order,
    make_order_state,
    make_order_update,
    make_position,
    make_trade,
    price_map,
)

PRICES = price_map()


def detector() -> WhaleDetector:
    return WhaleDetector(lambda coin: PRICES.get(coin))


def render(event: WhaleEvent) -> str:
    """Render an alert without a live stack.

    ``render`` and every helper it calls read the event only — no queue, bot,
    settings or session — so an uninitialised instance is enough.
    """
    return AlertService.render(AlertService.__new__(AlertService), event)


# ── TEST 1: a resting SELL with no position is an order, not a short ──

def test_1_sell_limit_with_no_position_is_an_order_only():
    order = make_open_order(side="A", limit_px=101_370.0, sz=40.0)   # $4.05M
    event = detector().from_open_order(WALLET_A, order, None, min_notional=2_000_000)

    assert event is not None
    assert event.event_type is EventType.ORDER_PLACED
    assert event.is_order_event and not event.is_position_event
    assert event.side == "SELL"
    assert event.value_kind is ValueKind.ORDER_NOTIONAL

    # No position state was invented, and none may be written from this event.
    assert position_status_of(event) is None
    assert not has_verified_position(event)
    assert not may_modify_position(event)
    assert not event.has("position_side")

    text = render(event)
    assert "🐋 LARGE LIMIT ORDER" in text
    assert "🔴 SELL LIMIT" in text
    assert "📌 <b>Status:</b> OPEN" in text
    assert "SHORT" not in text
    assert "LONG" not in text


# ── TEST 2: a short and a resting SELL stay two separate objects ──

def test_2_short_position_and_sell_limit_remain_separate():
    short = make_position(szi=-60.0, entry_px=99_000.0)
    position_event = detector().from_position_change(
        WALLET_A, "BTC", None, short, context=make_context(position=short)
    )
    order = make_open_order(side="A", limit_px=101_000.0, sz=40.0)
    order_event = detector().from_open_order(WALLET_A, order, None, min_notional=2_000_000)

    assert position_event is not None and order_event is not None
    assert position_event.event_type is EventType.POSITION_OPENED
    assert position_event.side == "SHORT"
    assert order_event.event_type is EventType.ORDER_PLACED
    assert order_event.side == "SELL"

    # Two events, two identities, two value kinds — never merged into one.
    assert position_event.value_kind is not order_event.value_kind
    assert position_event.order_id is None
    assert order_event.order_id == order.oid

    position_text, order_text = render(position_event), render(order_event)
    assert "📉 SHORT" in position_text
    assert "🔴 SELL LIMIT" in order_text
    assert "SHORT" not in order_text          # the order never borrows the position's word
    assert "SELL LIMIT" not in position_text  # nor the position the order's


# ── TEST 3: size → 0 closes from the last verified non-zero snapshot ──

def test_3_position_to_zero_is_closed_from_the_pre_close_snapshot():
    before = make_position(szi=60.0, entry_px=98_000.0, unrealized_pnl=120_000.0)
    event = detector().from_position_change(
        WALLET_A, "BTC", before, None, context=PositionContext()
    )

    assert event is not None
    assert event.event_type is EventType.POSITION_CLOSED
    assert position_status_of(event) is PositionStatus.CLOSED
    # $5.88M is the notional that *left* the book, read from the pre-close
    # snapshot — not computed from the new zero-size one.
    assert event.notional == before.notional
    assert event.numeric("closed_position_value") == before.notional
    assert event.numeric("size") == 0.0

    text = render(event)
    assert "🐋 WHALE POSITION CLOSED" in text
    assert "💰 <b>Closed:</b> $5,880,000" in text
    assert "📦 <b>Position size:</b> 0 BTC" in text
    # A position that no longer exists has no entry, leverage, liquidation or
    # TP/SL, and none are reconstructed or padded out with N/A.
    assert "ℹ️ Historical position details unavailable" in text
    for absent in ("Entry:", "Leverage:", "Liquidation:", "TP:", "SL:"):
        assert absent not in text


def test_3b_closed_pnl_is_an_estimate_never_labelled_realized():
    before = make_position(szi=60.0, unrealized_pnl=120_000.0)
    event = detector().from_position_change(WALLET_A, "BTC", before, None)

    assert event is not None
    assert event.point("final_unrealized_pnl").confidence is Confidence.ESTIMATED
    text = render(event)
    assert "<b>Final PnL (est.):</b> $120.00K" in text
    assert "Realized PnL" not in text


def test_3c_realized_pnl_is_printed_when_the_exchange_reported_it():
    """``closedPnl`` from the per-wallet fills feed is the only realised figure."""
    event = detector().from_position_change(WALLET_A, "BTC", make_position(szi=60.0), None)
    assert event is not None
    event.set("realized_pnl", DataPoint.confirmed(-84_000.0, "sum of closedPnl on fills"))

    text = render(event)
    assert "🔴 <b>Realized PnL:</b> -$84.00K" in text
    assert "Final PnL" not in text


def test_3d_pnl_is_na_when_nothing_is_known():
    before = make_position(szi=60.0, unrealized_pnl=None)
    event = detector().from_position_change(WALLET_A, "BTC", before, None)
    assert event is not None
    assert not event.point("final_unrealized_pnl").available
    assert "⚪ <b>Final PnL:</b> N/A" in render(event)


def test_3e_close_with_no_prior_snapshot_says_so():
    """Nothing was observed before the close: the gap is stated, not filled in."""
    after = make_position(szi=0.0, entry_px=98_000.0, position_value=0.0)
    event = detector().from_position_change(WALLET_A, "BTC", make_position(szi=40.0), after)
    assert event is not None
    assert event.event_type is EventType.POSITION_CLOSED

    orphan = detector().from_position_change(
        WALLET_A, "BTC", make_position(szi=40.0, position_value=None), None
    )
    assert orphan is not None
    assert orphan.point("historical_position").confidence is not Confidence.CONFIRMED


# ── TEST 4: a SELL placed after a close is a new order, not a short ──

def test_4_sell_limit_after_a_close_is_not_a_short_opening():
    closed = detector().from_position_change(
        WALLET_A, "BTC", make_position(szi=60.0), None, context=PositionContext()
    )
    later_order = make_open_order(side="A", limit_px=101_370.0, sz=40.0)
    order_event = detector().from_open_order(WALLET_A, later_order, None, min_notional=2_000_000)

    assert closed is not None and order_event is not None
    assert closed.event_type is EventType.POSITION_CLOSED
    assert order_event.event_type is EventType.ORDER_PLACED

    text = render(order_event)
    assert "🐋 LARGE LIMIT ORDER" in text
    assert "🔴 SELL LIMIT" in text
    assert "📌 <b>Status:</b> OPEN" in text
    assert "OPENED" not in text.upper().replace("POSITION CLOSED", "")
    assert "SHORT" not in text
    # The close did not leak into the order, and the order cannot re-close.
    assert not may_modify_position(order_event)


# ── TEST 5: a fill correlates with a position change, it does not cause one ──

def test_5_order_filled_does_not_open_a_position_by_itself():
    previous = make_order_state(side="A", limit_px=101_000.0, size=40.0)
    update = make_order_update(oid=previous.oid, side="A", limit_px=101_000.0,
                               sz=40.0, status="filled")
    filled = detector().from_order_update(WALLET_A, update, previous, min_notional=0)

    assert filled is not None
    assert filled.event_type is EventType.ORDER_FILLED
    assert order_status_of(filled) is OrderStatus.FILLED
    assert position_status_of(filled) is None
    assert not may_modify_position(filled)
    assert "SHORT" not in render(filled)

    # The position only moves when a fresh clearinghouseState snapshot proves it.
    short = make_position(szi=-40.0, entry_px=101_000.0)
    opened = detector().from_position_change(
        WALLET_A, "BTC", None, short, context=make_context(position=short)
    )
    assert opened is not None
    assert opened.event_type is EventType.POSITION_OPENED
    assert opened.side == "SHORT"
    assert may_modify_position(opened)


# ── TEST 6: a resting BUY with no position is never a long ──

def test_6_buy_limit_with_no_position_is_an_order_only():
    order = make_open_order(side="B", limit_px=99_640.0, sz=45.0)   # $4.48M
    event = detector().from_open_order(WALLET_A, order, None, min_notional=2_000_000)

    assert event is not None
    assert event.event_type is EventType.ORDER_PLACED
    assert event.side == "BUY"
    assert position_status_of(event) is None
    assert not may_modify_position(event)

    text = render(event)
    assert "🟢 BUY LIMIT" in text
    assert "LONG" not in text
    assert "POSITION" not in text.upper()


# ── TEST 7: a cancellation is not a close ──

def test_7_order_cancelled_is_not_position_closed():
    previous = make_order_state(side="A", limit_px=101_000.0, size=40.0)
    update = make_order_update(oid=previous.oid, side="A", limit_px=101_000.0,
                               sz=40.0, status="canceled")
    event = detector().from_order_update(WALLET_A, update, previous, min_notional=0)

    assert event is not None
    assert event.event_type is EventType.ORDER_CANCELLED
    assert event.event_type is not EventType.POSITION_CLOSED
    assert order_status_of(event) is OrderStatus.CANCELLED
    assert position_status_of(event) is None
    assert not may_modify_position(event)

    text = render(event)
    assert "🚨 WHALE ORDER CANCELLED" in text
    assert "POSITION CLOSED" not in text
    assert "Closed:" not in text


def test_7b_unresolved_disappearance_is_never_guessed_as_a_cancel():
    previous = make_order_state(side="A", limit_px=101_000.0, size=40.0)
    event = detector().from_order_disappearance(WALLET_A, previous, None, min_notional=0)

    assert event is not None
    assert event.event_type is not EventType.ORDER_CANCELLED
    assert order_status_of(event) is OrderStatus.UNRESOLVED
    assert position_status_of(event) is None
    assert "unresolved" in render(event).lower()


# ── distance wording (§11 / §12) ────────────────────────────────

def test_order_distance_says_above_or_below():
    above = make_open_order(side="A", limit_px=101_370.0, sz=40.0)
    below = make_open_order(side="B", limit_px=99_640.0, sz=45.0)
    above_text = render(detector().from_open_order(WALLET_A, above, None, min_notional=0))
    below_text = render(detector().from_open_order(WALLET_A, below, None, min_notional=0))

    assert "📐 <b>Distance:</b> +1.37% above" in above_text
    assert "📐 <b>Distance:</b> -0.36% below" in below_text


def test_position_distance_is_labelled_from_entry_not_distance():
    position = make_position(szi=60.0, entry_px=BTC_PX * 1.0006)   # mark 0.06% below entry
    event = detector().from_position_change(
        WALLET_A, "BTC", None, position, context=make_context(position=position)
    )
    assert event is not None
    text = render(event)
    assert "📐 <b>From Entry:</b> -0.06%" in text
    assert "Distance:" not in text


def test_a_bare_execution_labels_its_distance_as_a_fill():
    """No position behind the trade, so the mark comparison is named for what it is."""
    trade = make_trade(px=BTC_PX * 1.01, sz=50.0, buyer=WALLET_A)
    event = detector().from_trade(trade, wallet=WALLET_A, context=PositionContext())
    assert event is not None
    text = render(event)
    assert "📐 <b>Fill vs mark:</b>" in text
    assert "From Entry:" not in text


# ── lifecycle invariants ────────────────────────────────────────

def test_no_order_event_maps_to_a_position_state():
    for event_type in (
        EventType.ORDER_PLACED,
        EventType.ORDER_MODIFIED,
        EventType.ORDER_PARTIALLY_FILLED,
        EventType.ORDER_FILLED,
        EventType.ORDER_CANCELLED,
        EventType.ORDER_REJECTED,
    ):
        event = WhaleEvent(
            event_type=event_type,
            coin="BTC",
            notional=4_000_000.0,
            value_kind=ValueKind.ORDER_NOTIONAL,
            side="SELL",
            wallet=WALLET_A,
            status="open",
        )
        assert position_status_of(event) is None
        assert may_modify_position(event) is False


def test_an_aggregate_book_level_may_not_modify_position_state():
    event = WhaleEvent(
        event_type=EventType.BOOK_LEVEL,
        coin="BTC",
        notional=9_000_000.0,
        value_kind=ValueKind.BOOK_LEVEL_NOTIONAL,
        side="SELL",
    )
    assert not may_modify_position(event)
    assert position_status_of(event) is None


def test_a_trade_modifies_position_state_only_with_a_verified_snapshot():
    bare = detector().from_trade(
        make_trade(sz=50.0, buyer=WALLET_A), wallet=WALLET_A, context=PositionContext()
    )
    assert bare is not None
    assert not has_verified_position(bare)
    assert not may_modify_position(bare)

    enriched = detector().from_trade(
        make_trade(sz=50.0, buyer=WALLET_A), wallet=WALLET_A, context=make_context()
    )
    assert enriched is not None
    assert has_verified_position(enriched)
    assert may_modify_position(enriched)


def test_position_transitions_follow_the_documented_lifecycle():
    assert can_position_transition(None, PositionStatus.OPENED)
    assert can_position_transition(PositionStatus.OPENED, PositionStatus.ACTIVE)
    assert can_position_transition(PositionStatus.ACTIVE, PositionStatus.REDUCED)
    assert can_position_transition(PositionStatus.REDUCED, PositionStatus.CLOSED)
    # A close is terminal: the next entry is a *new* position, not a resumption.
    assert can_position_transition(PositionStatus.CLOSED, PositionStatus.OPENED)
    assert not can_position_transition(PositionStatus.CLOSED, PositionStatus.ACTIVE)
    assert not can_position_transition(PositionStatus.NO_POSITION, PositionStatus.CLOSED)


def test_order_transitions_end_at_terminal_states():
    assert can_order_transition(None, OrderStatus.PLACED)
    assert can_order_transition(OrderStatus.PLACED, OrderStatus.PARTIALLY_FILLED)
    assert can_order_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED)
    assert can_order_transition(OrderStatus.UNRESOLVED, OrderStatus.CANCELLED)
    for terminal in TERMINAL_ORDER_STATES:
        for live in LIVE_ORDER_STATES:
            assert not can_order_transition(terminal, live)
