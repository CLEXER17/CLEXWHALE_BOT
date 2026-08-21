"""Reconnect helpers: exponential backoff with full jitter."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field


@dataclass
class ExponentialBackoff:
    base: float = 1.0
    factor: float = 2.0
    maximum: float = 60.0
    jitter: float = 0.25
    attempt: int = field(default=0, init=False)

    def reset(self) -> None:
        self.attempt = 0

    def next_delay(self) -> float:
        """Delay for the upcoming retry, then advance the attempt counter."""
        raw = min(self.base * (self.factor ** self.attempt), self.maximum)
        self.attempt += 1
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))

    async def sleep(self) -> float:
        delay = self.next_delay()
        await asyncio.sleep(delay)
        return delay
