"""Server-side rate limiter (§5.3) — enforced, not advisory.

A central throttle on archiver/download calls: a configurable minimum interval between calls and a
per-source concurrency of 1. This protects GP and avoids the e-hentai "excessive pageloads" IP ban,
and is the mechanism that enforces a plugin's advisory ``cooldown``.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from contextlib import contextmanager


class RateLimiter:
    def __init__(self, min_interval: float = 5.0, concurrency: int = 1) -> None:
        self.min_interval = max(0.0, min_interval)
        self._guard = threading.Lock()
        self._last: dict[str, float] = defaultdict(float)
        self._sems: dict[str, threading.Semaphore] = {}
        self._concurrency = max(1, concurrency)

    def configure(self, min_interval: float, concurrency: int) -> None:
        with self._guard:
            self.min_interval = max(0.0, min_interval)
            self._concurrency = max(1, concurrency)
            self._sems.clear()

    def _sem(self, source: str) -> threading.Semaphore:
        with self._guard:
            sem = self._sems.get(source)
            if sem is None:
                sem = threading.Semaphore(self._concurrency)
                self._sems[source] = sem
            return sem

    @contextmanager
    def slot(self, source: str):
        """Acquire a per-source slot, sleeping to honour the minimum inter-call interval."""
        sem = self._sem(source)
        sem.acquire()
        try:
            with self._guard:
                wait = self.min_interval - (time.monotonic() - self._last[source])
            if wait > 0:
                time.sleep(wait)
            yield
        finally:
            with self._guard:
                self._last[source] = time.monotonic()
            sem.release()
