from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

SCHEMA_VERSION = 3
LOGGER = logging.getLogger(__name__)
CORRUPT = {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}
DELETE_KEYS = "DELETE FROM image WHERE key IN (SELECT value FROM json_each(?)) RETURNING length(data)"


class InvalidCache(sqlite3.DatabaseError):
    pass


class ImageCache:
    """Disposable image cache: an in-memory LRU in front of one SQLite file.

    The file is rebuilt whenever it is unreadable, foreign, over budget or corrupt; a disk
    failure only costs re-rendering. Memory bookkeeping uses `_lock`; disk I/O runs on one
    long-lived connection under `_disk_lock`, so cache hits in memory never wait for the disk.
    The bytes of image data on disk are summed once per connection and then kept up to date by
    this object's writes; the page budget (max_page_count) still bounds the file if another
    process writes too.
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
        # Pages, indexes and free space take the rest; a quarter is generous for 10-50 KiB images.
        self.data_bytes = self.disk_bytes - max(self.disk_bytes // 4, 32 * 1024)
        self.memory_bytes, self.memory_entries = memory_bytes, memory_entries
        pending_bytes = min(pending_bytes, self.disk_bytes // 2)
        self.item_bytes = min(item_bytes, pending_bytes, self.disk_bytes // 4)
        self.batch_size, self.pending_bytes, self.flush_seconds = batch_size, pending_bytes, flush_seconds
        self._memory: OrderedDict[str, bytes] = OrderedDict()
        self._memory_used = 0
        self._pending: OrderedDict[str, bytes] = OrderedDict()
        self._kinds: dict[str, int] = {}
        self._last_flush = time.monotonic()
        self._lock = threading.Lock()
        self._disk_lock = threading.Lock()
        self._db: sqlite3.Connection | None = None
        self._stored: int | None = None
        self._count: int | None = None
        self._render_locks = [threading.Lock() for _ in range(16)]
        self.disk_error: str | None = None
        self.generation = 0
        """Counts rebuilds of the file: every entry written before a rebuild is gone."""

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
            if (
                not isinstance(error, InvalidCache)
                and getattr(error, "sqlite_errorcode", 0) & 0xFF not in CORRUPT
            ):
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
                    "(id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, data BLOB NOT NULL, kind "
                    "INTEGER NOT NULL DEFAULT 0)"
                )
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                raise InvalidCache("Unrecognized image cache")
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=OFF")
            maximum = self.disk_bytes // db.execute("PRAGMA page_size").fetchone()[0]
            if db.execute("PRAGMA page_count").fetchone()[0] > maximum:
                raise InvalidCache("Image cache exceeds the disk budget")
            db.execute(f"PRAGMA max_page_count={maximum}")
        except BaseException:
            db.close()
            raise
        self._db, self._stored = db, None
        return db

    def _stored_bytes(self, db: sqlite3.Connection) -> int:
        """Bytes of image data in the file; the caller holds `_disk_lock`."""
        stored = self._stored
        if stored is None:
            stored = self._stored = db.execute("SELECT coalesce(sum(length(data)),0) FROM image").fetchone()[
                0
            ]
        return stored

    def _discard(self) -> None:
        """Drop the cache file so the next use starts empty."""
        self.generation += 1
        self._stored = self._count = None
        if self._db is not None:
            self._db.close()
            self._db = None
        for suffix in ("", "-journal", "-wal", "-shm"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

    def _failed(self, error: Exception) -> None:
        self.disk_error = type(error).__name__
        if isinstance(error, sqlite3.DatabaseError) and (
            isinstance(error, InvalidCache) or getattr(error, "sqlite_errorcode", 0) & 0xFF in CORRUPT
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
        try:
            db.execute("BEGIN IMMEDIATE")
            if self._count is None:
                self._count = db.execute("SELECT count(*) FROM image").fetchone()[0]
            total = self._stored_bytes(db)
            removed = db.execute(DELETE_KEYS, (json.dumps(list(batch)),)).fetchall()
            total -= sum(size for (size,) in removed)
            assert self._count is not None
            count = self._count - len(removed)
            while (
                total + incoming > self.data_bytes
                or (count + len(batch)) * 4096 + total + incoming > self.disk_bytes - 16384
            ):
                rows = db.execute("SELECT id,length(data) FROM image ORDER BY kind,id LIMIT 128").fetchall()
                if not rows:
                    raise sqlite3.DatabaseError("Image batch exceeds the disk budget")
                for identifier, size in rows:
                    db.execute("DELETE FROM image WHERE id=?", (identifier,))
                    total -= size
                    count -= 1
                    if total + incoming <= self.data_bytes:
                        break
            with self._lock:
                rows = [(key, data, self._kinds.get(key, 0)) for key, data in batch.items()]
            db.executemany("INSERT INTO image(key,data,kind) VALUES(?,?,?)", rows)
            db.execute("COMMIT")
            self._stored = total + incoming
            self._count = count + len(batch)
            return
        except BaseException:
            self._stored = self._count = None
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise

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

    def has(self, key: str) -> bool:
        """Whether an entry is held in memory, pending or on disk, without reading the image."""
        with self._lock:
            if key in self._memory or key in self._pending:
                return True
        with self._disk_lock:
            try:
                db = self._open(create=False)
                if db is None:
                    return False
                return (
                    db.execute(
                        "SELECT 1 FROM image WHERE key=? AND length(data)<=?", (key, self.item_bytes)
                    ).fetchone()
                    is not None
                )
            except (OSError, sqlite3.Error) as error:
                self._failed(error)
                return False

    def has_many(self, keys: list[str]) -> set[str]:
        with self._lock:
            found = {key for key in keys if key in self._memory or key in self._pending}
        missing = [key for key in keys if key not in found]
        if not missing:
            return found
        with self._disk_lock:
            try:
                db = self._open(create=False)
                if db is not None:
                    found.update(
                        row[0]
                        for row in db.execute(
                            "SELECT key FROM image WHERE key IN (SELECT value FROM json_each(?)) "
                            "AND length(data)<=?",
                            (json.dumps(missing), self.item_bytes),
                        )
                    )
            except (OSError, sqlite3.Error) as error:
                self._failed(error)
        return found

    def used_bytes(self) -> int:
        """Size of the cache file on disk (0 while it does not exist)."""
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def stored_bytes(self) -> int:
        """Bytes of image data held on disk and waiting to be written; compare with data_bytes."""
        with self._lock:
            pending = sum(map(len, self._pending.values()))
        with self._disk_lock:
            try:
                db = self._open(create=False)
                if db is None:
                    return pending
                return pending + self._stored_bytes(db)
            except (OSError, sqlite3.Error) as error:
                self._failed(error)
                return pending

    def get_or_create(self, key: str, render: Callable[[], bytes], *, cover: bool = False) -> bytes:
        stripe = int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % len(self._render_locks)
        with self._render_locks[stripe]:
            hit = self.get(key)
            if hit is not None:
                return hit
            data = render()
            self.put(key, data, cover=cover)
            return data

    def put(self, key: str, data: bytes, *, cover: bool = False) -> None:
        if not data or len(data) > self.item_bytes:
            return
        with self._lock:
            self._remember(key, data)
            full = sum(map(len, self._pending.values())) + len(data) > self.pending_bytes
        if full:
            self.flush()
        with self._lock:
            self._pending[key] = data
            self._kinds[key] = int(cover)
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
                        self._kinds.pop(key, None)

    def close(self) -> None:
        self.flush()
        with self._disk_lock:
            if self._db is not None:
                self._db.close()
                self._db = None
        with self._lock:
            self._memory.clear()
            self._memory_used = 0
