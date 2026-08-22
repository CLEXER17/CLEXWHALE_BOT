"""The whale event model.

A ``WhaleEvent`` is the single currency of the pipeline: detectors emit it,
filters accept or reject it, the deduplicator keys off it, the repository
persists it and the alert service renders it.

Two design rules are enforced structurally rather than by convention:

1. **Value kinds are never conflated.** ``notional`` is always accompanied by a
   :class:`ValueKind` saying *what* was measured — an executed trade's value, a
   position's notional, the USD delta of a position change, a resting order's
   notional or margin. A $2M threshold means something different for each, so
   the detector states which one it used and the alert prints it.
2. **Optional fields carry confidence.** Anything Hyperliquid may or may not
   expose lives in :attr:`WhaleEvent.points` as a
   :class:`~app.utils.formatting.DataPoint`, so a derived number can never be
   rendered as if the trader had set it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Iterable

from app.utils.formatting import Confidence, DataPoint, utc_now


class EventType(str, Enum):
    #: Large executed trade (taker crossed the book) seen on the trades feed.
    WHALE_TRADE = "WHALE_TRADE"
    #: A position force-closed by the exchange. Deliberately *not* a member of
    #: :data:`POSITION_EVENTS`: it is an execution the exchange reported, not the
    #: diff of two verified snapshots, so it must not write position state. The
    #: ``clearinghouseState`` refetch that follows it emits the real
    #: ``POSITION_CLOSED`` (see :func:`app.whale.lifecycle.may_modify_position`).
    WHALE_LIQUIDATED = "WHALE_LIQUIDATED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_INCREASED = "POSITION_INCREASED"
    POSITION_DECREASED = "POSITION_DECREASED"
    POSITION_CLOSED = "POSITION_CLOSED"
    POSITION_FLIPPED = "POSITION_FLIPPED"
    ORDER_PLACED = "ORDER_PLACED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED = "ORDER_PARTIALLY_FILLED"
    ORDER_MODIFIED = "ORDER_MODIFIED"
    ORDER_REJECTED = "ORDER_REJECTED"
    #: Aggregate order-book level. No wallet attribution exists for these.
    BOOK_LEVEL = "BOOK_LEVEL"


POSITION_EVENTS = frozenset(
    {
        EventType.POSITION_OPENED,
        EventType.POSITION_INCREASED,
        EventType.POSITION_DECREASED,
        EventType.POSITION_CLOSED,
        EventType.POSITION_FLIPPED,
    }
)
ORDER_EVENTS = frozenset(
    {
        EventType.ORDER_PLACED,
        EventType.ORDER_CANCELLED,
        EventType.ORDER_FILLED,
        EventType.ORDER_PARTIALLY_FILLED,
        EventType.ORDER_MODIFIED,
        EventType.ORDER_REJECTED,
    }
)
CANCEL_EVENTS = frozenset({EventType.ORDER_CANCELLED})

#: Events that describe a trade that **actually happened**. This is the only set
#: allowed to reach the primary whale feed, and it is what ``/recent`` and
#: ``/whales`` count. A resting, modified or cancelled order is an *intention*
#: and never appears here.
EXECUTION_EVENTS = frozenset({EventType.WHALE_TRADE, EventType.WHALE_LIQUIDATED})

#: Coarse grouping used by the engine counters, the diagnostics panel and the
#: history commands, so "trades observed" can never be inflated by order events.
EVENT_CATEGORIES = ("execution", "position", "order", "book")


class ValueKind(str, Enum):
    TRADE_VALUE = "TRADE_VALUE"
    POSITION_NOTIONAL = "POSITION_NOTIONAL"
    POSITION_DELTA = "POSITION_DELTA"
    ORDER_NOTIONAL = "ORDER_NOTIONAL"
    MARGIN = "MARGIN"
    BOOK_LEVEL_NOTIONAL = "BOOK_LEVEL_NOTIONAL"
    #: The USD value the exchange force-closed: the liquidation fill's own
    #: ``px * sz``. Not a discretionary trade and not a snapshot notional.
    LIQUIDATION_VALUE = "LIQUIDATION_VALUE"


VALUE_KIND_LABELS = {
    ValueKind.TRADE_VALUE: "Executed trade value",
    ValueKind.POSITION_NOTIONAL: "Position notional",
    ValueKind.POSITION_DELTA: "Position change (cash flow)",
    ValueKind.ORDER_NOTIONAL: "Resting order notional",
    ValueKind.MARGIN: "Margin committed",
    ValueKind.BOOK_LEVEL_NOTIONAL: "Aggregate book level",
    ValueKind.LIQUIDATION_VALUE: "Liquidated position value",
}



class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    BUY = "BUY"
    SELL = "SELL"


#: Which threshold class a detector's value belongs to.
THRESHOLD_CLASS = {
    ValueKind.TRADE_VALUE: "trade",
    ValueKind.POSITION_NOTIONAL: "position",
    ValueKind.POSITION_DELTA: "position_delta",
    ValueKind.ORDER_NOTIONAL: "order",
    ValueKind.MARGIN: "position",
    ValueKind.BOOK_LEVEL_NOTIONAL: "order",
    # A liquidation is position news, not trade news: the figure is the value of
    # a position the exchange closed, so it is measured against the *position*
    # threshold. No fifth threshold class is introduced — an administrator who
    # set "positions over $5M" already said what size of position interests them.
    ValueKind.LIQUIDATION_VALUE: "position",
}


@dataclass(slots=True)
class WhaleEvent:
    event_type: EventType
    coin: str
    notional: float
    value_kind: ValueKind
    event_time: datetime = field(default_factory=utc_now)
    side: str | None = None
    wallet: str | None = None
    #: Human label for the detection route, e.g. "Large market trade".
    detection: str = ""
    order_id: int | None = None
    status: str | None = None
    #: Confidence-labelled optional fields (see module docstring).
    points: dict[str, DataPoint] = field(default_factory=dict)
    #: Non-rendered context kept for auditing (tid, hash, source feed, ...).
    context: dict[str, Any] = field(default_factory=dict)
    #: Assigned by the deduplicator.
    dedup_key: str = ""

    # ── point helpers ─────────────────────────────────────────
    def set(self, key: str, point: DataPoint) -> None:
        self.points[key] = point

    def set_many(self, items: dict[str, DataPoint]) -> None:
        self.points.update(items)

    def point(self, key: str) -> DataPoint:
        return self.points.get(key, DataPoint.unavailable())

    def value(self, key: str, default: Any = None) -> Any:
        point = self.points.get(key)
        return point.value if point is not None and point.available else default

    def numeric(self, key: str) -> float | None:
        raw = self.value(key)
        if isinstance(raw, (int, float)):
            return float(raw)
        return None

    def has(self, key: str) -> bool:
        return self.points.get(key, DataPoint.unavailable()).available

    def confidence(self, key: str) -> Confidence:
        return self.points.get(key, DataPoint.unavailable()).confidence

    # ── persistence ───────────────────────────────────────────
    @property
    def is_position_event(self) -> bool:
        return self.event_type in POSITION_EVENTS

    @property
    def is_order_event(self) -> bool:
        return self.event_type in ORDER_EVENTS

    @property
    def is_execution(self) -> bool:
        """True when this event reports a trade that actually executed."""
        return self.event_type in EXECUTION_EVENTS

    @property
    def category(self) -> str:
        """One of :data:`EVENT_CATEGORIES`. Keeps the counters honest."""
        if self.event_type in EXECUTION_EVENTS:
            return "execution"
        if self.event_type in POSITION_EVENTS:
            return "position"
        if self.event_type in ORDER_EVENTS:
            return "order"
        return "book"

    @property
    def threshold_class(self) -> str:
        return THRESHOLD_CLASS.get(self.value_kind, "trade")

    @property
    def value_kind_label(self) -> str:
        return VALUE_KIND_LABELS.get(self.value_kind, self.value_kind.value)

    def detail_json(self) -> dict[str, Any]:
        """Everything needed to re-render or audit this event later."""
        return {
            "points": {key: point.to_json() for key, point in self.points.items()},
            "context": self.context,
            "detection": self.detection,
            "value_kind": self.value_kind.value,
        }

    def db_fields(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type.value,
            "coin": self.coin,
            "side": self.side,
            "wallet": self.wallet.lower() if self.wallet else None,
            "notional": float(self.notional),
            "value_kind": self.value_kind.value,
            "price": self.numeric("price"),
            "size": self.numeric("size"),
            "entry_px": self.numeric("entry_px"),
            "liquidation_px": self.numeric("liquidation_px"),
            "leverage": self.numeric("leverage"),
            "take_profit_px": self.numeric("take_profit_px"),
            "stop_loss_px": self.numeric("stop_loss_px"),
            "position_value": self.numeric("position_value"),
            "order_id": self.order_id,
            "status": self.status,
            "detail": self.detail_json(),
            "dedup_key": self.dedup_key,
            "event_time": self.event_time,
        }


def coin_of(events: Iterable[WhaleEvent]) -> set[str]:
    return {event.coin for event in events}
