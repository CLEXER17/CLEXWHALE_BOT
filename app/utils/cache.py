"""Small bounded caches used by the detection engine."""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Generic, Iterator, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LRUCache(Generic[K, V]):
    """Insertion/access ordered dict with a hard size cap."""

    def __init__(self, maxsize: int = 512) -> None:
        self.maxsize = max(1, maxsize)
        self._data: OrderedDict[K, V] = OrderedDict()
        self.evictions = 0

    def get(self, key: K, default: V | None = None) -> V | None:
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return default

    def set(self, key: K, value: V) -> K | None:
        """Store and return the evicted key, if any."""
        self._data[key] = value
        self._data.move_to_end(key)
        evicted: K | None = None
        while len(self._data) > self.maxsize:
            evicted, _ = self._data.popitem(last=False)
            self.evictions += 1
        return evicted

    def pop(self, key: K, default: V | None = None) -> V | None:
        return self._data.pop(key, default)

    def touch(self, key: K) -> None:
        if key in self._data:
            self._data.move_to_end(key)

    def keys(self) -> list[K]:
        return list(self._data.keys())

    def values(self) -> list[V]:
        return list(self._data.values())

    def items(self) -> list[tuple[K, V]]:
        return list(self._data.items())

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self) -> Iterator[K]:
        return iter(list(self._data.keys()))


class TTLCache(Generic[K, V]):
    """Time-expiring map. Lazy eviction on access plus explicit ``purge()``."""

    def __init__(self, ttl: float = 300.0, maxsize: int = 4096) -> None:
        self.ttl = ttl
        self.maxsize = max(1, maxsize)
        self._data: OrderedDict[K, tuple[float, V]] = OrderedDict()

    def _expired(self, stamp: float, now: float, ttl: float | None = None) -> bool:
        return now - stamp > (self.ttl if ttl is None else ttl)

    def get(self, key: K, default: V | None = None, ttl: float | None = None) -> V | None:
        entry = self._data.get(key)
        if entry is None:
            return default
        stamp, value = entry
        if self._expired(stamp, time.monotonic(), ttl):
            self._data.pop(key, None)
            return default
        return value

    def set(self, key: K, value: V) -> None:
        self._data[key] = (time.monotonic(), value)
        self._data.move_to_end(key)
        while len(self._data) > self.maxsize:
            self._data.popitem(last=False)

    def age(self, key: K) -> float | None:
        entry = self._data.get(key)
        return None if entry is None else time.monotonic() - entry[0]

    def contains(self, key: K, ttl: float | None = None) -> bool:
        return self.get(key, None, ttl) is not None

    def pop(self, key: K) -> None:
        self._data.pop(key, None)

    def purge(self) -> int:
        now = time.monotonic()
        stale = [k for k, (stamp, _) in self._data.items() if self._expired(stamp, now)]
        for key in stale:
            self._data.pop(key, None)
        return len(stale)

    def keys(self) -> list[K]:
        return list(self._data.keys())

    def __len__(self) -> int:
        return len(self._data)
