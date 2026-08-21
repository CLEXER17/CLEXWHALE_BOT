"""Rate limiting.

``WeightedRateLimiter`` models Hyperliquid's documented REST budget: an
aggregated *weight* of 1200 per minute per IP, where ``clearinghouseState``
costs 2 and most other info requests cost 20. We spend only a configurable
fraction of that budget so a burst of whale activity can never trip a 429.

``TokenBucket`` is the per-Telegram-user command limiter.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field


class WeightedRateLimiter:
    """Sliding-window weight limiter, safe for concurrent callers."""

    def __init__(self, budget_per_minute: float, window: float = 60.0) -> None:
        self.budget = max(1.0, float(budget_per_minute))
        self.window = window
        self._events: deque[tuple[float, float]] = deque()
        self._spent = 0.0
        self._lock = asyncio.Lock()
        self.total_spent = 0.0
        self.total_waits = 0

    def _evict(self, now: float) -> None:
        cutoff = now - self.window
        while self._events and self._events[0][0] <= cutoff:
            _, weight = self._events.popleft()
            self._spent -= weight
        if not self._events:
            self._spent = 0.0

    @property
    def spent(self) -> float:
        self._evict(time.monotonic())
        return self._spent

    @property
    def available(self) -> float:
        return max(0.0, self.budget - self.spent)

    def try_acquire(self, weight: float) -> bool:
        """Spend immediately if the budget allows, else return False."""
        now = time.monotonic()
        self._evict(now)
        if self._spent + weight > self.budget:
            return False
        self._events.append((now, weight))
        self._spent += weight
        self.total_spent += weight
        return True

    async def acquire(self, weight: float, timeout: float | None = None) -> bool:
        """Block until ``weight`` fits in the window. False on timeout."""
        deadline = None if timeout is None else time.monotonic() + timeout
        async with self._lock:
            while True:
                now = time.monotonic()
                self._evict(now)
                if self._spent + weight <= self.budget:
                    self._events.append((now, weight))
                    self._spent += weight
                    self.total_spent += weight
                    return True
                if deadline is not None and now >= deadline:
                    return False
                # Wait until the oldest event leaves the window.
                wait = (self._events[0][0] + self.window) - now if self._events else 0.05
                if deadline is not None:
                    wait = min(wait, deadline - now)
                self.total_waits += 1
                await asyncio.sleep(max(0.01, min(wait, self.window)))


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class TokenBucket:
    """Per-key token bucket; used to throttle Telegram commands per user."""

    rate: float = 1.0  # tokens per second
    capacity: float = 8.0
    _buckets: dict[int | str, _Bucket] = field(default_factory=dict)

    def consume(self, key: int | str, tokens: float = 1.0) -> bool:
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(self.capacity, now)
            self._buckets[key] = bucket
        elapsed = now - bucket.updated
        bucket.updated = now
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate)
        if bucket.tokens < tokens:
            return False
        bucket.tokens -= tokens
        return True

    def prune(self, older_than: float = 900.0) -> None:
        cutoff = time.monotonic() - older_than
        for key in [k for k, b in self._buckets.items() if b.updated < cutoff]:
            self._buckets.pop(key, None)
