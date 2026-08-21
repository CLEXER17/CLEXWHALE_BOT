"""Threshold, coin and detector-toggle filtering.

The filter is the only place that decides whether an event is "a whale". It
applies the threshold that matches the event's :class:`~app.whale.events.ValueKind`
so an order-notional gate is never silently applied to a trade value, and it
keeps per-reason counters so ``/status`` can explain *why* alerts are quiet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.settings_service import RuntimeConfig, SettingsService
from app.whale.events import CANCEL_EVENTS, EventType, WhaleEvent

DETECTOR_OF_EVENT = {
    EventType.WHALE_TRADE: "trade",
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
REASON_CANCELS_OFF = "cancel_alerts_disabled"
REASON_THRESHOLD = "below_threshold"


@dataclass(frozen=True)
class FilterResult:
    accepted: bool
    reason: str = REASON_OK
    threshold: float | None = None

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

        if event.event_type in CANCEL_EVENTS and not cfg.enable_order_cancel_alerts:
            return FilterResult(False, REASON_CANCELS_OFF)

        threshold = cfg.threshold_for(event.threshold_class)
        if abs(event.notional) < threshold:
            return FilterResult(False, REASON_THRESHOLD, threshold)

        return FilterResult(True, REASON_OK, threshold)
