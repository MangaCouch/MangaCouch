from __future__ import annotations

import logging
import threading

from mangacouch.files import RootUnavailable
from mangacouch.maintenance import Maintenance, Retry


def test_retry_delay_saturates_through_thousands_of_failures():
    now = 0.0
    retry = Retry(clock=lambda: now)
    for attempt in range(5000):
        retry.failed("book")
        assert not retry.ready("book")
        expected = min(3600, 30 * 2 ** min(attempt, 8))
        now += expected
        assert retry.ready("book")
    retry.succeeded("book")
    assert not retry.failures


def test_retry_cursor_prunes_deleted_keys_without_losing_live_backoff():
    retry = Retry(clock=lambda: 0)
    for key in ["a", "c", "e", "z"]:
        retry.failed(key)
    retry.after = "c"
    remaining = ["b", "c", "d", "f"]
    assert retry.take(lambda after, count: [key for key in remaining if key > after][:count], 10) == [
        "d",
        "f",
        "b",
    ]
    assert set(retry.failures) == {"c"}
    assert not retry.ready("c")
    assert retry.take(lambda after, count: [], 10) == []
    assert not retry.failures


def test_maintenance_hourly_failure_is_logged_once_without_traceback(caplog):
    calls = []

    def offline():
        calls.append("offline")
        raise RootUnavailable("offline")

    worker = Maintenance(steps=[], hourly=[offline])
    with caplog.at_level(logging.WARNING):
        worker.run_hourly()
    assert calls == ["offline"]
    assert [record.exc_info for record in caplog.records] == [None]


def test_maintenance_steps_rerun_while_busy_and_sleep_when_idle():
    remaining = [3]
    done = []
    idle = threading.Event()

    def work() -> bool:
        if remaining[0]:
            remaining[0] -= 1
            return True
        done.append(True)
        idle.set()
        return False

    worker = Maintenance(steps=[work], hourly=[])
    worker.start()
    assert idle.wait(5)
    worker.close()
    assert remaining == [0] and len(done) == 1
