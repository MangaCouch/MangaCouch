from __future__ import annotations

import hashlib
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

SCHEMA_VERSION = 2
LOGGER = logging.getLogger(__name__)
TRANSIENT = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


class ImageCache:
    """Disposable image cache: an in-memory LRU in front of one SQLite file.

    The file is rebuilt whenever it is unreadable, foreign, over budget or corrupt; a disk
    failure only costs re-rendering. Memory bookkeeping uses `_lock`; disk I/O runs on one
    long-lived connection under `_disk_lock`, so cache hits in memory never wait for the disk.
    """

    def __init__(
        self,
        directory: Path,
        *,
        name: str = "thumbnails",
        disk_bytes: int = 256 * 1024**2,
        memory_bytes: int = 16 * 1024**2,
        memory_entries: int = 128,
        item_bytes: int = 1024**2,
        batch_size: int = 16,
        pending_bytes: int = 2 * 1024**2,
        flush_seconds: float = 2,
    ):
        if (
            disk_bytes < 64 * 1024
            or min(memory_bytes, memory_entries, item_bytes, batch_size, pending_bytes, flush_seconds) <= 0
        ):
            raise ValueError("Image cache budgets must be positive; disk requires at least 64 KiB")
        self.directory = directory.absolute()
        self.path = self.directory / f"{name}.sqlite"
        self.disk_bytes = disk_bytes // 4096 * 4096
        self.memory_bytes, self.memory_entries = memory_bytes, memory_entries
        pending_bytes = min(pending_bytes, self.disk_bytes // 2)
        self.item_bytes = min(item_bytes, pending_bytes, self.disk_bytes // 4)
        self.batch_size, self.pending_bytes, self.flush_seconds = batch_size, pending_bytes, flush_seconds
        self._memory: OrderedDict[str, bytes] = OrderedDict()
        self._memory_used = 0
        self._pending: OrderedDict[str, bytes] = OrderedDict()
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._render_locks = [threading.Lock() for _ in range(16)]
        self.disk_error: str | None = None

    # Disk ------------------------------------------------------------------------------------

    def _open(self, *, create: bool) -> sqlite3.Connection | None:
        """The cache connection, recreating an unusable file; None if absent and not creating."""
        if self._db is not None:
            return self._db
        if self.directory.is_symlink():
            raise OSError("The cache directory must not be a symbolic link")
        if self.path.is_symlink():
            self.path.unlink()
        if not create and not self.path.exists():
            return None
        self.directory.mkdir(mode=0o700, exist_ok=True)
        try:
            return self._connect()
        except sqlite3.DatabaseError as error:
            if getattr(error, "sqlite_errorcode", 0) & 0xFF in TRANSIENT:
                raise
            self._discard()
            return self._connect()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=0.1, check_same_thread=False, isolation_level=None)
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            tables = db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
            if version == 0 and tables == 0:
                db.execute("PRAGMA page_size=4096")
                db.execute(
                    "CREATE TABLE image "
                    "(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, data BLOB NOT NULL)"
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise sqlite3.DatabaseError("Unrecognized image cache")
            maximum = self.disk_bytes // db.execute("PRAGMA page_size").fetchone()[0]
            if db.execute("PRAGMA page_count").fetchone()[0] > maximum:
                raise sqlite3.DatabaseError("Image cache exceeds the disk budget")
            db.execute(f"PRAGMA max_page_count={maximum}")
        except BaseException:
            db.close()
            raise
        self._db = db
        return db

    def _discard(self) -> None:
        """Drop the cache file so the next use starts empty."""
        if self._db is not None:
            self._db.close()
            self._db = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

    def _failed(self, error: Exception) -> None:
        self.disk_error = type(error).__name__
        if (
            isinstance(error, sqlite3.DatabaseError)
            and getattr(error, "sqlite_errorcode", 0) & 0xFF not in TRANSIENT
        ):
            LOGGER.warning("Rebuilding image cache %s: %s", self.path.name, error)
            with suppress(OSError):
                self._discard()
        else:
            LOGGER.info("Image cache %s unavailable: %s", self.path.name, self.disk_error)

    def _read(self, key: str) -> bytes | None:
        with self._disk_lock:
            try:
                db = self._open(create=False)
                if db is None:
                    return None
                row = db.execute(
                    "SELECT data FROM image WHERE key=? AND length(data)<=?", (key, self.item_bytes)
                ).fetchone()
                return row[0] if row and isinstance(row[0], bytes) else None
            except (OSError, sqlite3.Error) as error:
                self._failed(error)
                return None

    def _write_batch(self, batch: dict[str, bytes]) -> None:
        db = self._open(create=True)
        assert db is not None
        incoming = sum(map(len, batch.values()))
        while True:
            try:
                db.execute("BEGIN IMMEDIATE")
                db.executemany("DELETE FROM image WHERE key=?", ((key,) for key in batch))
                total = db.execute("SELECT coalesce(sum(length(data)),0) FROM image").fetchone()[0]
                # Keep half the file for indexes, row overhead and reusable free pages.
                while total + incoming > self.disk_bytes // 2:
                    rows = db.execute("SELECT id,length(data) FROM image ORDER BY id LIMIT 128").fetchall()
                    if not rows:
                        raise sqlite3.DatabaseError("Image batch exceeds the disk budget")
                    for identifier, size in rows:
                        db.execute("DELETE FROM image WHERE id=?", (identifier,))
                        total -= size
                        if total + incoming <= self.disk_bytes // 2:
                            break
                db.executemany("INSERT INTO image(key,data) VALUES(?,?)", batch.items())
                db.execute("COMMIT")
                return
            except sqlite3.OperationalError as error:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                if error.sqlite_errorcode != sqlite3.SQLITE_FULL:
                    raise
                count = db.execute("SELECT count(*) FROM image").fetchone()[0]
                if not count:
                    raise
                # BLOB lengths alone do not account for page fragmentation: drop the oldest quarter.
                db.execute(
                    "DELETE FROM image WHERE id IN (SELECT id FROM image ORDER BY id LIMIT ?)",
                    (max(1, count // 4),),
                )

    # Public API ------------------------------------------------------------------------------

    def _remember(self, key: str, data: bytes) -> None:
        if len(data) > self.memory_bytes:
            return
        previous = self._memory.pop(key, None)
        self._memory_used += len(data) - (len(previous) if previous is not None else 0)
        self._memory[key] = data
        while len(self._memory) > self.memory_entries or self._memory_used > self.memory_bytes:
            self._memory_used -= len(self._memory.popitem(last=False)[1])

    def get(self, key: str) -> bytes | None:
        with self._lock:
            hit = self._memory.get(key)
            if hit is not None:
                self._memory.move_to_end(key)
                return hit
            hit = self._pending.get(key)
            if hit is not None:
                self._remember(key, hit)
                return hit
        hit = self._read(key)
        if hit is not None:
            with self._lock:
                self._remember(key, hit)
        return hit

    def get_or_create(self, key: str, render: Callable[[], bytes]) -> bytes:
        stripe = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(self._render_locks)
        with self._render_locks[stripe]:
            hit = self.get(key)
            if hit is not None:
                return hit
            data = render()
            self.put(key, data)
            return data

    def put(self, key: str, data: bytes) -> None:
        if not data or len(data) > self.item_bytes:
            return
        with self._lock:
            self._remember(key, data)
            full = sum(map(len, self._pending.values())) + len(data) > self.pending_bytes
        if full:
            self.flush()
        with self._lock:
            self._pending[key] = data
            due = (
                len(self._pending) >= self.batch_size
                or time.monotonic() - self._last_flush >= self.flush_seconds
            )
        if due:
            self.flush()

    def flush(self) -> None:
        with self._disk_lock:
            with self._lock:
                batch = dict(self._pending)
                self._last_flush = time.monotonic()
            if not batch:
                return
            for _ in range(2):
                try:
                    self._write_batch(batch)
                    self.disk_error = None
                    break
                except (OSError, sqlite3.Error) as error:
                    self._failed(error)
                    if self._db is not None or not isinstance(error, sqlite3.DatabaseError):
                        break  # Not rebuilt (transient or not a database problem): drop this batch.
            with self._lock:
                # Written or dropped: either way the memory LRU still serves these entries.
                for key, data in batch.items():
                    if self._pending.get(key) is data:
                        del self._pending[key]

    def close(self) -> None:
        self.flush()
        with self._disk_lock:
            if self._db is not None:
                self._db.close()
                self._db = None
        with self._lock:
            self._memory.clear()
            self._memory_used = 0
