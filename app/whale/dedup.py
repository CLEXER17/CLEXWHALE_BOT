"""Duplicate-alert prevention.

Two independent gates, because "duplicate" means two different things:

**Identity** — the exact same observation arriving twice (a websocket snapshot
replayed after reconnect, the same position state read by two consecutive
polls, the same ``oid``+status pair). Keyed on the strongest identifier the feed
provides (``tid`` for trades, ``oid``+status for orders, a quantised position
state otherwise) and remembered for ``identity_ttl``.

**Cooldown** — a genuinely new observation that a human would still call spam
(a whale slicing one position into forty fills). Keyed on
``type|coin|wallet|side`` plus an order-of-magnitude bucket, and suppressed for
``ALERT_COOLDOWN_SECONDS``. The magnitude bucket is deliberate: a $2M follow-up
is noise, a $200M follow-up is news, so a 10× escalation is allowed through
rather than being swallowed by the cooldown.

After a restart the identity cache is warmed from ``alert_history`` so a
redeploy does not re-announce whatever was live at the time.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from app.utils.cache import TTLCache
from app.utils.logging import get_logger
from app.whale.events import EventType, WhaleEvent

log = get_logger(__name__)

IDENTITY_TTL = 3600.0
DEFAULT_COOLDOWN = 30


def _digest(*parts: object) -> str:
    raw = "|".join("" if part is None else str(part) for part in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def magnitude_bucket(value: float) -> int:
    """Order of magnitude, so 10× escalations are treated as distinct events."""
    value = abs(float(value or 0.0))
    if value < 1.0:
        return 0
    return int(math.floor(math.log10(value)))


def identity_key(event: WhaleEvent) -> str:
    """Strongest available identifier for "this exact observation"."""
    wallet = (event.wallet or "anon").lower()
    base = (event.event_type.value, event.coin, wallet, event.side)

    if event.event_type is EventType.WHALE_TRADE:
        tid = event.context.get("tid")
        if tid is not None:
            return _digest(*base, "tid", tid)
        return _digest(*base, "px", event.value("price"), "sz", event.value("size"),
                       "t", int(event.event_time.timestamp()))

    if event.is_order_event:
        return _digest(*base, "oid", event.order_id, "status", event.status,
                       "sz", round(float(event.value("size") or 0.0), 8))

    if event.event_type is EventType.BOOK_LEVEL:
        return _digest(*base, "px", event.value("price"),
                       "ntl", round(event.notional, -3))

    # Position events: the quantised state itself is the identity.
    return _digest(
        *base,
        "sz", round(float(event.value("size") or 0.0), 8),
        "pv", round(float(event.value("position_value") or 0.0), 2),
        "entry", event.value("entry_px"),
    )


def cooldown_key(event: WhaleEvent) -> str:
    return _digest(
        "cd",
        event.event_type.value,
        event.coin,
        (event.wallet or "anon").lower(),
        event.side,
        magnitude_bucket(event.notional),
    )


@dataclass
class DedupStats:
    checked: int = 0
    duplicate_identity: int = 0
    cooled_down: int = 0
    passed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "checked": self.checked,
            "duplicate_identity": self.duplicate_identity,
            "cooled_down": self.cooled_down,
            "passed": self.passed,
        }


class Deduplicator:
    def __init__(
        self,
        identity_ttl: float = IDENTITY_TTL,
        cache_size: int = 16_384,
    ) -> None:
        self._identity: TTLCache[str, bool] = TTLCache(ttl=identity_ttl, maxsize=cache_size)
        self._cooldown: TTLCache[str, float] = TTLCache(ttl=max(identity_ttl, 3600.0), maxsize=cache_size)
        self.stats = DedupStats()

    def warm(self, keys: Iterable[str]) -> int:
        """Seed the identity cache from persisted alert history after a restart."""
        count = 0
        for key in keys:
            self._identity.set(key, True)
            count += 1
        if count:
            log.info("Dedup cache warmed from alert history", extra={"keys": count})
        return count

    def check(self, event: WhaleEvent, cooldown_seconds: int = DEFAULT_COOLDOWN) -> bool:
        """Assign ``event.dedup_key`` and report whether it should be alerted."""
        self.stats.checked += 1
        key = identity_key(event)
        event.dedup_key = key

        if self._identity.get(key) is not None:
            self.stats.duplicate_identity += 1
            return False

        if cooldown_seconds > 0:
            ckey = cooldown_key(event)
            age = self._cooldown.age(ckey)
            if age is not None and age < cooldown_seconds:
                # Remember the identity anyway: it has now been considered, so a
                # later retry of the same observation stays suppressed.
                self._identity.set(key, True)
                self.stats.cooled_down += 1
                return False
            self._cooldown.set(ckey, event.notional)

        self._identity.set(key, True)
        self.stats.passed += 1
        return True

    def forget(self, event: WhaleEvent) -> None:
        """Undo a ``check`` — used when persistence/alerting failed."""
        if event.dedup_key:
            self._identity.pop(event.dedup_key)

    def purge(self) -> int:
        return self._identity.purge() + self._cooldown.purge()

    def as_dict(self) -> dict[str, object]:
        return {
            **self.stats.as_dict(),
            "identity_cached": len(self._identity),
            "cooldown_cached": len(self._cooldown),
        }
