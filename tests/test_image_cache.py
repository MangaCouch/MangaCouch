from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from mangacouch.image_cache import ImageCache


def test_empty_thumbnail_database_recovers(tmp_path):
    from mangacouch.image_cache import ImageCache

    cache = ImageCache(tmp_path)
    cache.path.touch()
    assert cache.get("page") is None and cache.disk_error is None
    cache.put("page", b"rendered")
    cache.flush()
    assert cache.disk_error is None
    assert ImageCache(tmp_path).get("page") == b"rendered"


def cache(tmp_path: Path, **kwargs) -> ImageCache:
    return ImageCache(tmp_path / "cache", **kwargs)


def test_writes_are_batched_and_shutdown_flushes_last_entries(tmp_path):
    service = cache(tmp_path, batch_size=3, flush_seconds=3600)
    service.put("one", b"a")
    service.put("two", b"b")
    assert not service.path.exists()
    service.put("three", b"c")
    with closing(sqlite3.connect(service.path)) as db:
        assert db.execute("SELECT count(*) FROM image").fetchone()[0] == 3
    service.put("four", b"d")
    service.close()
    assert cache(tmp_path).get("four") == b"d"


def test_memory_and_pending_buffers_stay_bounded(tmp_path):
    service = cache(
        tmp_path,
        memory_bytes=40,
        memory_entries=2,
        item_bytes=100,
        pending_bytes=50,
        batch_size=100,
        flush_seconds=3600,
    )
    for index in range(15):
        service.put(str(index), b"x" * 20)
        assert sum(map(len, service._memory.values())) <= 40 and len(service._memory) <= 2
        assert sum(map(len, service._pending.values())) <= 50
    service.close()
    assert service.disk_error is None


def test_disk_quota_evicts_older_entries_and_keeps_accepting_covers(tmp_path):
    service = cache(tmp_path, disk_bytes=64 * 1024, memory_bytes=1, batch_size=1)
    for index in range(100):
        service.put(str(index), os.urandom(2100))
        assert service.path.stat().st_size <= service.disk_bytes
        assert service.disk_error is None
    assert service.get("0") is None and service.get("99") is not None
    with closing(sqlite3.connect(service.path)) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_disk_hits_do_not_update_access_time_or_disk_eviction_order(tmp_path):
    service = cache(tmp_path, batch_size=1)
    service.put("old", b"first")
    service.put("new", b"second")
    service.close()
    before = service.path.read_bytes()
    reopened = cache(tmp_path)
    assert reopened.get("old") == b"first"
    assert reopened.path.read_bytes() == before


def test_concurrent_same_key_generation_is_coalesced(tmp_path):
    service = cache(tmp_path)
    calls = []

    def render():
        calls.append(1)
        return b"jpeg fixture"

    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda _: service.get_or_create("book", render), range(32)))
    assert values == [b"jpeg fixture"] * 32 and len(calls) == 1


def test_memory_hits_do_not_wait_for_disk_io(tmp_path):
    service = cache(tmp_path, batch_size=1)
    service.put("hot", b"in memory")
    # A slow disk write or read holds only the disk lock.
    with service._disk_lock, ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(service.get, "hot").result(timeout=1) == b"in memory"


def test_oversized_or_foreign_cache_file_is_recreated_not_disabled(tmp_path):
    service = cache(tmp_path, disk_bytes=64 * 1024, batch_size=1)
    service.directory.mkdir()
    with closing(sqlite3.connect(service.path)) as db:
        db.execute("CREATE TABLE filler(data BLOB)")
        db.execute("INSERT INTO filler VALUES(zeroblob(200000))")
        db.commit()
    assert service.get("missing") is None
    service.put("fresh", b"data")
    assert service.disk_error is None
    service.close()
    assert service.path.stat().st_size <= 64 * 1024
    assert cache(tmp_path, disk_bytes=64 * 1024).get("fresh") == b"data"


def test_one_connection_serves_every_read(tmp_path, monkeypatch):
    service = cache(tmp_path, batch_size=1, memory_entries=1)
    service.put("a", b"1")
    service.put("b", b"2")
    opened = []
    connect = sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **kw: opened.append(1) or connect(*a, **kw))
    for _ in range(5):
        assert service.get("a") == b"1" and service.get("b") == b"2"
    assert opened == []
    service.close()
