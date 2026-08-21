"""Whale detection: event model, detectors, filters, dedup, tracking, engine."""

from app.whale.dedup import Deduplicator
from app.whale.detector import OrderState, PositionContext
from app.whale.engine import WhaleEngine
from app.whale.events import EventType, Side, ValueKind, WhaleEvent
from app.whale.filters import WhaleFilter
from app.whale.tracker import TrackedWallet, WalletTracker

__all__ = [
    "Deduplicator",
    "EventType",
    "OrderState",
    "PositionContext",
    "Side",
    "TrackedWallet",
    "ValueKind",
    "WalletTracker",
    "WhaleEngine",
    "WhaleEvent",
    "WhaleFilter",
]
