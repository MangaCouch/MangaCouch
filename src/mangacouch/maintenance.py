"""Background maintenance worker: scans, bulk jobs, sidecars, duplicate indexing, hourly purges."""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable

from .database import background_thread
from .database import busy as database_busy
from .errors import RevisionConflict
from .files import RootUnavailable, set_thread_cancel
from .limits import ImportCancelled

LOGGER = logging.getLogger(__name__)
HOURLY_SECONDS = 3600
IDLE_SECONDS = 30
BUSY_PAUSE = 0.05

Step = Callable[[], bool]


class Retry:
    """Rotating cursor and per-item backoff for background writes that may keep failing.

    Keys are visited in order from where the previous pass stopped, wrapping around, so a run
    of failing items never starves the ones after them; a failed key waits base * 2^n seconds
    (capped) before it is tried again. The state is process-local: a restart retries at once.
    """

    def __init__(
        self, *, base: float = 30.0, cap: float = 3600.0, clock: Callable[[], float] = time.monotonic
    ):
        self.base, self.cap, self.clock = base, cap, clock
        self.max_failures = max(1, math.ceil(math.log2(cap) - math.log2(base)) + 1)
        self.after = ""
        self.failures: dict[str, tuple[int, float]] = {}

    def ready(self, key: str) -> bool:
        entry = self.failures.get(key)
        return entry is None or entry[1] <= self.clock()

    def failed(self, key: str) -> None:
        count = min(self.max_failures, self.failures.get(key, (0, 0.0))[0] + 1)
        delay = self.cap if count == self.max_failures else min(self.cap, self.base * 2 ** (count - 1))
        self.failures[key] = (count, self.clock() + delay)

    def succeeded(self, key: str) -> None:
        self.failures.pop(key, None)

    def take(self, select: Callable[[str, int], list[str]], limit: int, *, scan: int = 1000) -> list[str]:
        """Up to `limit` ready keys after the cursor; `select(after, n)` lists candidates in key order."""
        chosen: list[str] = []
        examined, wrapped = 0, False
        while len(chosen) < limit and examined < scan:
            keys = select(self.after, limit * 10)
            selected = set(keys)
            for key in list(self.failures):
                if key > self.after and (not keys or key <= keys[-1]) and key not in selected:
                    del self.failures[key]
            if not keys:
                if wrapped or not self.after:
                    break
                self.after, wrapped = "", True
                continue
            for key in keys:
                if key in chosen:
                    return chosen
                examined += 1
                self.after = key
                if self.ready(key):
                    chosen.append(key)
                    if len(chosen) == limit:
                        break
        return chosen


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
            self._stopping.clear()
            self._wake.clear()
            self._thread = threading.Thread(target=self._run, name="mangacouch-maintenance", daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Stop the worker; TimeoutError when its current step does not finish within 30 s."""
        self._stopping.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                raise TimeoutError("Maintenance worker did not stop within 30 s")
            self._thread = None

    def run_hourly(self) -> None:
        for task in self.hourly:
            try:
                task()
            except ImportCancelled:
                if self._stopping.is_set():
                    return
                raise
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
            except ImportCancelled:
                if self._stopping.is_set():
                    return False
                raise
            except sqlite3.Error as error:
                if database_busy(error):
                    LOGGER.warning("Maintenance step %s waits: database busy", _name(step))
                else:
                    LOGGER.exception("Background %s failed", _name(step))
            except (RootUnavailable, RevisionConflict) as error:
                # Expected while storage is offline or a user edit wins a race; the step retries.
                LOGGER.debug("Background %s deferred: %s", _name(step), error)
            except Exception:
                LOGGER.exception("Background %s failed", _name(step))
        return busy

    def _run(self) -> None:
        background_thread()
        set_thread_cancel(self._stopping.is_set)
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
