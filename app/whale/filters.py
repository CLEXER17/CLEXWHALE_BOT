"""Threshold, coin, margin and detector-toggle filtering.

The filter is the only place that decides whether an event is "a whale". It
applies the threshold that matches the event's :class:`~app.whale.events.ValueKind`
so an order-notional gate is never silently applied to a trade value, and it
keeps per-reason counters so ``/status`` can explain *why* alerts are quiet.

Two gates, not one. The threshold is about *notional value*; the optional margin
gate is about *collateral actually at risk* (Hyperliquid's ``marginUsed``). They
are deliberately separate settings because they measure different things — a
$5M position at 20x carries $250k of margin, and treating those numbers as
interchangeable would misreport risk.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.settings_service import RuntimeConfig, SettingsService
from app.whale.events import CANCEL_EVENTS, ORDER_EVENTS, EventType, WhaleEvent

DETECTOR_OF_EVENT = {
    EventType.WHALE_TRADE: "trade",
    # A liquidation is position news, so the position detector governs it: an
    # admin who turned position alerts off is not asking to still be told about
    # forced closes. No new toggle is added for one event type.
    EventType.WHALE_LIQUIDATED: "position",
    EventType.POSITION_OPENED: "position",
    EventType.POSITION_INCREASED: "position",
    EventType.POSITION_DECREASED: "position",
    EventType.POSITION_CLOSED: "position",
    EventType.POSITION_FLIPPED: "position",
    EventType.ORDER_PLACED: "order",
    EventType.ORDER_CANCELLED: "order",
    EventType.ORDER_FILLED: "order",
    EventType.ORDER_PARTIALLY_FILLED: "order",
    EventType.ORDER_MODIFIED: "order",
    EventType.ORDER_REJECTED: "order",
    EventType.BOOK_LEVEL: "book",
}

REASON_OK = "accepted"
REASON_MONITORING_OFF = "monitoring_disabled"
REASON_COIN = "coin_not_tracked"
REASON_DETECTOR = "detector_disabled"
#: An order event was tracked internally but not published. This is the reason
#: the primary feed stays clean by default, and it is counted separately from
#: ``detector_disabled`` so diagnostics can say "tracked, not alerted" rather
#: than implying order monitoring is off.
REASON_ORDER_ALERTS_OFF = "order_alerts_disabled"
REASON_CANCELS_OFF = "cancel_alerts_disabled"
REASON_THRESHOLD = "below_threshold"
REASON_MARGIN = "below_margin"
REASON_MARGIN_UNKNOWN = "margin_unknown"

#: Event types the margin gate never applies to, because no margin figure
#: exists for them: an aggregate book level has no wallet at all, and a resting
#: order commits no collateral until it fills. Gating these would silence whole
#: detectors for a reason that has nothing to do with the admin's intent.
#:
#: A liquidation is exempt for a sharper reason. The margin *is* the thing that
#: just ran out, and Hyperliquid does not report the wallet's margin at the
#: moment of liquidation on any feed (`.agent/API_NOTES.md` §5). So the gate
#: would read "margin unknown" and reject every liquidation — turning the margin
#: filter into an off switch for the one event it least applies to.
#:
#: Fills stay in the exempt set for the same data-availability reason even though
#: they are executions: an order event is built from ``orderUpdates`` /
#: ``frontendOpenOrders``, neither of which carries ``marginUsed``, so the gate
#: could only ever answer "unknown" and would silence every fill.
MARGIN_EXEMPT_EVENTS = (
    frozenset({EventType.BOOK_LEVEL, EventType.WHALE_LIQUIDATED}) | ORDER_EVENTS
)



@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str = REASON_OK
    threshold: float | None = None
    #: The margin gate in force, when one was applied.
    margin_gate: float | None = None

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.accepted


@dataclass
class FilterStats:
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def record(self, result: FilterResult) -> None:
        if result.accepted:
            self.accepted += 1
        else:
            self.rejected[result.reason] = self.rejected.get(result.reason, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {"accepted": self.accepted, "rejected": dict(self.rejected)}


class WhaleFilter:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings
        self.stats = FilterStats()

    @property
    def config(self) -> RuntimeConfig:
        return self.settings.config

    def evaluate(self, event: WhaleEvent, config: RuntimeConfig | None = None) -> FilterResult:
        cfg = config or self.config
        result = self._evaluate(event, cfg)
        self.stats.record(result)
        return result

    def _evaluate(self, event: WhaleEvent, cfg: RuntimeConfig) -> FilterResult:
        if not cfg.monitoring_enabled:
            return FilterResult(False, REASON_MONITORING_OFF)

        if not cfg.coin_enabled(event.coin):
            return FilterResult(False, REASON_COIN)

        detector = DETECTOR_OF_EVENT.get(event.event_type, "trade")
        if not cfg.detector_enabled(detector):
            return FilterResult(False, REASON_DETECTOR)

        # A resting order is an intention, not a trade. Order tracking keeps
        # running (it is where TP/SL and fill attribution come from); publishing
        # is a separate, off-by-default decision, so the primary feed carries
        # executions and position changes only.
        #
        # The gate is deliberately on ``is_resting_order_event`` and not on
        # ``is_order_event``: a filled order **is** an execution, and silencing
        # "tell me about orders sitting on the book" must not silence "a whale
        # actually traded". That was the reported defect in reverse — the feed used
        # to announce intentions as trades, and the naive fix would have hidden
        # real fills.
        if event.is_resting_order_event and not cfg.enable_order_alerts:
            return FilterResult(False, REASON_ORDER_ALERTS_OFF)

        if event.event_type in CANCEL_EVENTS and not cfg.enable_order_cancel_alerts:
            return FilterResult(False, REASON_CANCELS_OFF)

        threshold = cfg.threshold_for(event.threshold_class)
        if abs(event.notional) < threshold:
            return FilterResult(False, REASON_THRESHOLD, threshold)

        margin_result = self._margin_gate(event, cfg, threshold)
        if margin_result is not None:
            return margin_result

        return FilterResult(True, REASON_OK, threshold, cfg.min_margin_value or None)

    @staticmethod
    def _margin_gate(
        event: WhaleEvent, cfg: RuntimeConfig, threshold: float
    ) -> FilterResult | None:
        """Reject on margin, or return ``None`` to let the event through.

        An unknown margin is a rejection, not a pass: an administrator who set
        "only positions with more than $2M of margin" has not asked to also
        receive the ones whose margin could not be read. The separate
        ``margin_unknown`` counter makes that visible in ``/status`` instead of
        looking like an empty market.
        """
        if not cfg.margin_gate_enabled or event.event_type in MARGIN_EXEMPT_EVENTS:
            return None
        gate = cfg.min_margin_value
        margin = event.numeric("margin_used")
        if margin is None:
            return FilterResult(False, REASON_MARGIN_UNKNOWN, threshold, gate)
        if abs(margin) < gate:
            return FilterResult(False, REASON_MARGIN, threshold, gate)
        return None
