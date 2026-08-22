"""Two separate state machines: one for resting orders, one for positions.

An order and a position are different objects with different lifecycles, and the
rule that keeps them apart is the whole point of this module:

* **An order event may never move a position between states.** A ``SELL LIMIT``
  can rest while the wallet holds no position at all, holds a long, or holds a
  short — the order says nothing about which. Only executed/verified position
  data (``clearinghouseState``) may change position state.
* **Order lifecycle:** ``PLACED → OPEN → PARTIALLY_FILLED → FILLED``, or
  ``→ CANCELLED``, or ``→ REJECTED``. An order that leaves the book without the
  exchange telling us why is ``UNRESOLVED``, never guessed as cancelled.
* **Position lifecycle:** ``NO_POSITION → OPENED → ACTIVE → REDUCED → CLOSED``.

``ORDER_FILLED`` *may* correlate with a position change, but the correlation is
never assumed: the position only moves when a fresh snapshot proves it moved.
"""

from __future__ import annotations

from enum import Enum

from app.whale.events import ORDER_EVENTS, EventType, WhaleEvent


class OrderStatus(str, Enum):
    """Where a resting order is in its own lifecycle."""

    PLACED = "PLACED"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    #: The order left the book and the ``orderStatus`` lookup could not say
    #: whether it filled or was cancelled. Reported as unresolved, never guessed.
    UNRESOLVED = "UNRESOLVED"


class PositionStatus(str, Enum):
    """Where a position is in its own lifecycle."""

    NO_POSITION = "NO_POSITION"
    OPENED = "OPENED"
    ACTIVE = "ACTIVE"
    REDUCED = "REDUCED"
    CLOSED = "CLOSED"


#: States an order can never leave.
TERMINAL_ORDER_STATES = frozenset(
    {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
)

#: States in which an order is still working in the book.
LIVE_ORDER_STATES = frozenset(
    {OrderStatus.PLACED, OrderStatus.OPEN, OrderStatus.PARTIALLY_FILLED}
)

ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PLACED: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNRESOLVED,
        }
    ),
    OrderStatus.OPEN: frozenset(
        {
            OrderStatus.OPEN,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNRESOLVED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.UNRESOLVED,
        }
    ),
    # An unresolved disappearance can still be explained later by a lookup.
    OrderStatus.UNRESOLVED: frozenset(
        {OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}

POSITION_TRANSITIONS: dict[PositionStatus, frozenset[PositionStatus]] = {
    PositionStatus.NO_POSITION: frozenset({PositionStatus.OPENED}),
    PositionStatus.OPENED: frozenset(
        {PositionStatus.ACTIVE, PositionStatus.REDUCED, PositionStatus.CLOSED}
    ),
    PositionStatus.ACTIVE: frozenset(
        {PositionStatus.ACTIVE, PositionStatus.REDUCED, PositionStatus.CLOSED}
    ),
    PositionStatus.REDUCED: frozenset(
        {PositionStatus.ACTIVE, PositionStatus.REDUCED, PositionStatus.CLOSED}
    ),
    # A close is terminal; a later entry is a *new* OPENED position.
    PositionStatus.CLOSED: frozenset({PositionStatus.OPENED}),
}

#: Order events map onto the order machine only.
ORDER_STATUS_OF_EVENT: dict[EventType, OrderStatus] = {
    EventType.ORDER_PLACED: OrderStatus.PLACED,
    EventType.ORDER_MODIFIED: OrderStatus.OPEN,
    EventType.ORDER_PARTIALLY_FILLED: OrderStatus.PARTIALLY_FILLED,
    EventType.ORDER_FILLED: OrderStatus.FILLED,
    EventType.ORDER_CANCELLED: OrderStatus.CANCELLED,
    EventType.ORDER_REJECTED: OrderStatus.REJECTED,
}

#: Position events map onto the position machine only. Note what is *absent*:
#: no order event appears here, and ``WHALE_TRADE`` does not either — an
#: execution is evidence a position may have moved, not a position state.
POSITION_STATUS_OF_EVENT: dict[EventType, PositionStatus] = {
    EventType.POSITION_OPENED: PositionStatus.OPENED,
    EventType.POSITION_INCREASED: PositionStatus.ACTIVE,
    EventType.POSITION_DECREASED: PositionStatus.REDUCED,
    EventType.POSITION_FLIPPED: PositionStatus.OPENED,
    EventType.POSITION_CLOSED: PositionStatus.CLOSED,
}

#: Side words that only ever belong to a position, never to an order.
POSITION_SIDES = frozenset({"LONG", "SHORT"})
#: Side words that only ever belong to an order or a raw execution.
ORDER_SIDES = frozenset({"BUY", "SELL"})

#: Events that may never write position state, whatever data they happen to
#: carry. Order events and aggregate book levels for the reasons in the module
#: docstring; ``WHALE_LIQUIDATED`` because the only position snapshot available
#: to it is the *pre-liquidation* one. Writing that would restore a position the
#: exchange has just closed. The forced ``clearinghouseState`` refetch that
#: follows a liquidation produces the real ``POSITION_CLOSED``.
NEVER_MODIFY_POSITION = ORDER_EVENTS | frozenset(
    {EventType.BOOK_LEVEL, EventType.WHALE_LIQUIDATED}
)


def order_status_of(event: WhaleEvent) -> OrderStatus | None:
    """The order-machine state this event represents, or ``None`` if it is not
    an order event. A disappearance whose outcome was never resolved reports
    :attr:`OrderStatus.UNRESOLVED` rather than a guess."""
    if event.event_type not in ORDER_EVENTS:
        return None
    if event.status in {None, "", "unknown"} and not event.has("order_status"):
        return OrderStatus.UNRESOLVED
    return ORDER_STATUS_OF_EVENT.get(event.event_type)


def position_status_of(event: WhaleEvent) -> PositionStatus | None:
    """The position-machine state this event represents, or ``None``.

    Returns ``None`` for every order event by construction: an order event has
    no position state to report.
    """
    return POSITION_STATUS_OF_EVENT.get(event.event_type)


def can_order_transition(current: OrderStatus | None, nxt: OrderStatus) -> bool:
    if current is None:
        return nxt in {OrderStatus.PLACED, OrderStatus.OPEN}
    return nxt in ORDER_TRANSITIONS.get(current, frozenset())


def can_position_transition(current: PositionStatus | None, nxt: PositionStatus) -> bool:
    return nxt in POSITION_TRANSITIONS.get(current or PositionStatus.NO_POSITION, frozenset())


def has_verified_position(event: WhaleEvent) -> bool:
    """True when the event carries position data read from ``clearinghouseState``.

    Side alone never counts: a ``BUY`` on the trades feed is not evidence of a
    long, and a ``SELL LIMIT`` is not evidence of a short.
    """
    return event.has("position_side") or event.has("position_value") or event.has("position_size")


def may_modify_position(event: WhaleEvent) -> bool:
    """Whether this event is allowed to write position state.

    Anything in :data:`NEVER_MODIFY_POSITION`: never. Position-lifecycle events:
    always, because they are themselves the diff of two verified snapshots — and
    that includes ``POSITION_CLOSED``, which must be able to close the record
    even though the position it describes no longer exists. An executed trade:
    only when a verified position snapshot came with it.
    """
    if event.event_type in NEVER_MODIFY_POSITION:
        return False
    if event.is_position_event:
        return True
    return has_verified_position(event)


__all__ = [
    "LIVE_ORDER_STATES",
    "NEVER_MODIFY_POSITION",
    "ORDER_SIDES",
    "ORDER_STATUS_OF_EVENT",
    "ORDER_TRANSITIONS",
    "POSITION_SIDES",
    "POSITION_STATUS_OF_EVENT",
    "POSITION_TRANSITIONS",
    "TERMINAL_ORDER_STATES",
    "OrderStatus",
    "PositionStatus",
    "can_order_transition",
    "can_position_transition",
    "has_verified_position",
    "may_modify_position",
    "order_status_of",
    "position_status_of",
]
