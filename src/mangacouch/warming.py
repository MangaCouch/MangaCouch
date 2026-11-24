"""Background cache warm-up: book covers, then the thumbnails of every page, rendered once."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import Literal

from .database import Book
from .files import RootUnavailable
from .image_cache import ImageCache
from .ingest import FileLibrary

LOGGER = logging.getLogger(__name__)
BATCH = 4
"""Images rendered per maintenance step; small so scans and imports interleave."""
CHECKS = 256
"""Cache lookups per step, so a pass over an already warm library still yields often."""
RESERVE = 10
"""The thumbnail pass leaves 1/RESERVE of the cache free instead of evicting covers to fill it."""

WarmKind = Literal["covers", "thumbnails"]


class Warmer:
    """Walks the library in import order and renders the images the cache does not hold.

    `covers` renders the first page of each book; `thumbnails` renders every page (the
    cover included, which shares its cache entry). A pass starts when books were added or
    removed since the last complete pass, when the cache file was rebuilt, or on request.
    Between passes a step costs one count query. The thumbnail pass stops early instead of
    evicting older entries once the cache is nearly full, so it never pushes covers out.
    """

    def __init__(self, files: FileLibrary, cache: ImageCache, kind: WarmKind = "covers"):
        self.files, self.library, self.cache, self.kind = files, files.library, cache, kind
        self._lock = threading.Lock()
        self._cursor: tuple[int, int] | None = None
        """(rowid, next page) of the active pass; None while no pass runs."""
        self._done = self._finished_total = self._rendered = self._failed = 0
        self._signature: tuple[int, int, int] | None = None
        """(books, last rowid, cache generation) when the last pass finished."""
        self._requested = False
        self._full = False
        self._room = 0
        """Estimated bytes the thumbnail pass may still add before it stops."""
        self._finished_at: int | None = None

    def request(self) -> dict:
        """Ask for a full pass even if nothing changed (after the cache was emptied, say)."""
        with self._lock:
            self._requested = True
        return self.summary()

    def active(self) -> bool:
        with self._lock:
            return self._cursor is not None

    def summary(self) -> dict:
        with self._lock:
            cursor, done = self._cursor, self._done
            status = {
                "state": "active" if cursor is not None else "queued" if self._requested else "idle",
                "done": done,
                "total": self._finished_total,
                "rendered": self._rendered,
                "failed": self._failed,
                "full": self._full,
                "finished_at": self._finished_at,
            }
        if cursor is not None:
            # Count the live backlog even while rendering a long book or waiting for covers.
            # Polling pays for the count, not every four-image render batch.
            with self.library.connection() as db:
                status["total"] = done + self._remaining(db, cursor[0])
        return status

    @staticmethod
    def _remaining(db: sqlite3.Connection, rowid: int) -> int:
        return db.execute("SELECT count(*) FROM book WHERE rowid>=?", (rowid,)).fetchone()[0]

    def _signature_now(self, db) -> tuple[int, int, int]:
        count, last = db.execute("SELECT count(*),coalesce(max(rowid),0) FROM book").fetchone()
        return (count, last, self.cache.generation)

    def _has_room(self) -> bool:
        """Thumbnail passes only: whether rendering more stays clear of evicting entries."""
        if self.kind == "covers":
            return True
        reserve = min(self.cache.item_bytes, self.cache.data_bytes // RESERVE)
        if self._room < reserve:
            self._room = self.cache.data_bytes - self.cache.stored_bytes()
        return self._room >= reserve

    def _finish(self, db: sqlite3.Connection, *, stopped_at: int | None = None) -> None:
        signature = self._signature_now(db)
        remaining = self._remaining(db, stopped_at) if stopped_at is not None else 0
        with self._lock:
            self._signature, self._cursor, self._full = signature, None, stopped_at is not None
            self._finished_total = self._done + remaining
            self._finished_at = int(time.time())

    def step(self) -> bool:
        """Renders one batch; True when it did work and another step should follow at once."""
        with self.library.connection() as db:
            # An import after the empty cursor query must still trigger the next pass.
            db.execute("BEGIN")
            with self._lock:
                if self._cursor is None:
                    signature = self._signature_now(db)
                    if not self._requested and signature == self._signature:
                        return False
                    self._requested, self._full, self._room = False, False, 0
                    self._cursor = (0, 0)
                    self._done = self._rendered = self._failed = 0
                rowid, page = self._cursor
            if not self._has_room():
                self._finish(db, stopped_at=rowid)
                return False
            rows = db.execute(
                "SELECT rowid,id,json_extract(content_json,'$.page_count') FROM book "
                "WHERE rowid>=? ORDER BY rowid LIMIT ?",
                (rowid, CHECKS),
            ).fetchall()
            if not rows:
                self._finish(db)
                return False
        renders, checks = BATCH, CHECKS
        # Skip books removed since the rows were listed, just as the live backlog does.
        for current, book_id, page_count in rows:
            start = page if current == rowid else 0
            end = 1 if self.kind == "covers" else max(1, int(page_count or 0))
            index, rendered, failed = start, 0, False
            found = self.library.get_books([book_id])
            book = found[0] if found else None
            keys = (
                [self.files.cover_key(book, n) for n in range(start, min(end, start + checks))]
                if book
                else []
            )
            cached = self.cache.has_many(keys)
            while book is not None and index < end and renders > 0 and checks > 0:
                if self.files.cover_key(book, index) in cached:
                    outcome = 0
                else:
                    if not self._has_room():
                        # Save the pages already rendered. The next step records the full stop.
                        break
                    outcome = self._warm(book, index)
                index, checks = index + 1, checks - 1
                if outcome is None:
                    failed = True
                    break
                if outcome:
                    rendered, renders = rendered + 1, renders - 1
                    self._room -= outcome
            complete = book is None or failed or index >= end
            with self._lock:
                self._rendered += rendered
                if complete:
                    self._cursor = (current + 1, 0)
                    self._done += book is not None
                    self._failed += failed
                else:
                    self._cursor = (current, index)
            if not complete or renders <= 0 or checks <= 0:
                break
        return True

    def _warm(self, book: Book, index: int) -> int | None:
        """Bytes rendered now, 0 when cached already, None when the book cannot be read."""
        try:
            return len(self.files.cover_bytes(book.id, self.cache, index, book=book)) or 1
        except RootUnavailable:
            raise
        except Exception as error:  # noqa: BLE001 - one unreadable book must not stall the pass
            LOGGER.info("%s of %s not warmed (page %d): %s", self.kind, book.id, index, error)
            return None
