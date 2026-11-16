"""A small time-based cache for data every visitor asks for.

Every open page polls the event data every few seconds. Without this, each of
those requests is a database query; with it, at most one per key per second.
Entries are per process, which is fine: a second of staleness is the contract.
"""

import threading
import time
from collections.abc import Callable, Hashable
from typing import TypeVar

T = TypeVar("T")


class TTLCache:
    def __init__(self, ttl_seconds: float, max_entries: int = 256) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._entries: dict[Hashable, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get_or_load(self, key: Hashable, load: Callable[[], T]) -> T:
        if self._ttl <= 0:
            return load()
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] > now:
                return entry[1]  # type: ignore[return-value]
        value = load()  # outside the lock: a slow load must not block other keys
        with self._lock:
            if len(self._entries) >= self._max_entries:
                self._entries.clear()
            self._entries[key] = (now + self._ttl, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
