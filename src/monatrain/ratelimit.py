"""Per-key sliding-window rate limiter (in memory, one process)."""

import time
from collections import deque
from collections.abc import Callable
from threading import Lock


_SWEEP_AT = 10_000  # distinct keys held before expired ones are dropped


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: int, clock: Callable[[], float] = time.monotonic) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def hit(self, key: str) -> bool:
        """Record a hit for `key`; return False (and record nothing) once `limit` hits fall in the window."""
        now = self._clock()
        with self._lock:
            if len(self._hits) > _SWEEP_AT:
                self._sweep(now)
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self._window_s:
                hits.popleft()
            if len(hits) >= self._limit:
                return False
            hits.append(now)
            return True

    def _sweep(self, now: float) -> None:
        for key in [k for k, hits in self._hits.items() if not hits or now - hits[-1] >= self._window_s]:
            del self._hits[key]
