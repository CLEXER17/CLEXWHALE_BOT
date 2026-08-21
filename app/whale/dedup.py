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
    """Strongest available identifier for "this exact observation".

    **Only feed-derived facts may appear in this key.** Enrichment runs
    asynchronously, so the same exchange event observed twice a second apart can
    carry different *derived* attributes on each pass — and the worst offender is
    :attr:`WhaleEvent.side`, which is the position side once a
    ``clearinghouseState`` snapshot has landed and the trade side before that
    (:meth:`WhaleDetector.from_trade`). Including it produced the reported
    defect: one ETH buy of $8.13M announced twice, one second apart, because the
    first pass keyed on ``BUY`` and the second on ``LONG``. Same for ``wallet``,
    which is ``None`` until a participant is resolved.

    So when the exchange gives a unique identifier — ``tid`` for a fill, ``oid``
    for an order — that identifier *is* the identity, and nothing enrichment can
    touch is mixed in.
    """
    if event.event_type is EventType.WHALE_TRADE:
        tid = event.context.get("tid")
        if tid is not None:
            # One fill, one alert. ``role`` distinguishes the taker's event from
            # the maker's for the same fill and comes straight from the trade
            # payload, so it cannot drift between observations.
            return _digest("trade", tid, event.context.get("role"))
        # No tid (a REST-derived fill): fall back to the immutable trade facts.
        # ``wallet`` stays in the key — it is read straight from the trade
        # payload's participants, so two whales trading the same size at the same
        # instant remain distinct — but ``event.side`` does not, because that is
        # the field enrichment rewrites. ``trade_side`` is what the feed said.
        return _digest(
            "trade",
            event.coin,
            (event.wallet or "anon").lower(),
            event.value("trade_side"),
            "px", event.value("price"),
            "sz", event.value("size"),
            "t", int(event.event_time.timestamp()),
        )

    if event.is_order_event:
        # ``oid`` is Hyperliquid's own order identifier: two events with the same
        # oid *and* the same lifecycle status are the same observation, however
        # many feeds delivered it. Size is included because a partial fill of the
        # same still-open order is genuinely new.
        if event.order_id is not None:
            return _digest(
                "order",
                event.order_id,
                event.event_type.value,
                event.status,
                "sz", round(float(event.value("size") or 0.0), 8),
            )
        return _digest(
            "order",
            event.event_type.value,
            event.coin,
            (event.wallet or "anon").lower(),
            # An order event's ``side`` is the order's own direction from the
            # feed, not a position side, so it is stable across observations.
            event.side,
            event.status,
            "px", event.value("price"),
            "sz", round(float(event.value("size") or 0.0), 8),
        )

    if event.event_type is EventType.BOOK_LEVEL:
        # No owner and no identifier — l2Book levels are aggregated — so the
        # level itself is the identity.
        return _digest(
            "book", event.coin, event.side,
            "px", event.value("price"),
            "ntl", round(event.notional, -3),
        )

    # Position events: the quantised state itself is the identity. ``side`` here
    # *is* feed-derived (it comes from the clearinghouseState snapshot), so it
    # belongs in the key.
    return _digest(
        "position",
        event.event_type.value,
        event.coin,
        (event.wallet or "anon").lower(),
        event.side,
        "sz", round(float(event.value("size") or 0.0), 8),
        "pv", round(float(event.value("position_value") or 0.0), 2),
        "entry", event.value("entry_px"),
    )


def stable_side(event: WhaleEvent) -> str | None:
    """The side as the *feed* reported it, ignoring enrichment.

    :attr:`WhaleEvent.side` prefers the position side when one is known, which
    makes it a moving target between two observations of one trade. Any dedup key
    must use this instead.
    """
    if event.event_type is EventType.WHALE_TRADE:
        return event.value("trade_side") or event.value("taker_side")
    return event.side


def cooldown_key(event: WhaleEvent) -> str:
    return _digest(
        "cd",
        event.event_type.value,
        event.coin,
        (event.wallet or "anon").lower(),
        stable_side(event),
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
