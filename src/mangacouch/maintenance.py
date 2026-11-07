"""Background maintenance worker: scans, bulk jobs, sidecars, duplicate indexing, hourly purges."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from .errors import RevisionConflict
from .files import RootUnavailable

LOGGER = logging.getLogger(__name__)
HOURLY_SECONDS = 3600
IDLE_SECONDS = 30
BUSY_PAUSE = 0.05

Step = Callable[[], bool]


class Maintenance:
    """One daemon thread that runs work while there is backlog and sleeps when idle.

    Each step returns True when it did work (so another pass follows at once). Callers
    that create work (a scan, a bulk job, an import) call wake() to skip the idle wait.
    """

    def __init__(self, *, steps: list[Step], hourly: list[Callable[[], object]]):
        self.steps, self.hourly = steps, hourly
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="mangacouch-maintenance", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def run_hourly(self) -> None:
        for task in self.hourly:
            try:
                task()
            except RootUnavailable:
                LOGGER.warning("Maintenance %s skipped: library root unavailable", _name(task))
            except Exception:
                LOGGER.exception("Maintenance %s failed", _name(task))

    def run_steps(self) -> bool:
        busy = False
        for step in self.steps:
            if self._stopping.is_set():
                return False
            try:
                busy = bool(step()) or busy
            except (RootUnavailable, RevisionConflict):
                pass
            except Exception:
                LOGGER.exception("Background %s failed", _name(step))
        return busy

    def _run(self) -> None:
        last_hourly = None
        while not self._stopping.is_set():
            now = time.monotonic()
            if last_hourly is None or now - last_hourly >= HOURLY_SECONDS:
                # Set before running so a failing purge is retried hourly, not every pass.
                last_hourly = now
                self.run_hourly()
            busy = self.run_steps()
            self._wake.wait(BUSY_PAUSE if busy else IDLE_SECONDS)
            self._wake.clear()


def _name(task: object) -> str:
    return getattr(task, "__name__", type(task).__name__)
