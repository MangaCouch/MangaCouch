"""Thumbnails in a single SQLite blob store (``cache/thumbs.sqlite``) — not one file per image (§4).

A library of thousands of galleries × hundreds of pages would otherwise scatter *millions* of tiny
files, which cripples NAS/network shares, wastes cluster slack, risks inode exhaustion on exFAT, and
chokes sync/antivirus tooling. One indexed table — key ``(archive_id, page, variant)``, value a
~KB WebP/JPEG (SQLite's small-blob sweet spot) — sidesteps all of it. It lives in ``cache/``, so it
stays disposable and rebuildable, and never touches ``library.sqlite``.

Covers are generated eagerly at ingest; per-page grid thumbnails lazily on first request (most
galleries never need them). ``thumbnails.prewarm = full`` pre-generates everything as a background
sweep. An optional ``max_cache_mb`` LRU cap evicts page thumbnails before covers.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from . import imaging
from .archives import ArchiveReader, PdfArchiveReader

COVER_PAGE = -1  # sentinel page index for the cover thumbnail
VARIANT_COVER = "cover"
VARIANT_PAGE = "page"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS thumb (
    archive_id  TEXT    NOT NULL,
    page        INTEGER NOT NULL,
    variant     TEXT    NOT NULL,
    mime        TEXT    NOT NULL,
    data        BLOB    NOT NULL,
    bytes       INTEGER NOT NULL,
    accessed_at REAL    NOT NULL,
    PRIMARY KEY (archive_id, page, variant)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS ix_thumb_lru ON thumb (variant, accessed_at);
"""


class ThumbStore:
    """Thread-safe blob store over ``thumbs.sqlite``."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=TRUNCATE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=8000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def get(self, archive_id: str, page: int, variant: str) -> tuple[str, bytes] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT mime, data FROM thumb WHERE archive_id=? AND page=? AND variant=?",
                (archive_id, page, variant),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE thumb SET accessed_at=? WHERE archive_id=? AND page=? AND variant=?",
                (time.time(), archive_id, page, variant),
            )
            self._conn.commit()
            return row[0], row[1]

    def has(self, archive_id: str, page: int, variant: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM thumb WHERE archive_id=? AND page=? AND variant=?",
                (archive_id, page, variant),
            ).fetchone()
            return row is not None

    def put(self, archive_id: str, page: int, variant: str, mime: str, data: bytes) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO thumb (archive_id, page, variant, mime, data, bytes, "
                "accessed_at) VALUES (?,?,?,?,?,?,?)",
                (archive_id, page, variant, mime, data, len(data), time.time()),
            )
            self._conn.commit()

    def delete_archive(self, archive_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM thumb WHERE archive_id=?", (archive_id,))
            self._conn.commit()

    def total_bytes(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM thumb").fetchone()
            return int(row[0])

    def enforce_cap(self, max_bytes: int) -> int:
        """Evict least-recently-used **page** thumbnails first, then covers. Returns bytes freed."""
        if max_bytes <= 0:
            return 0
        freed = 0
        with self._lock:
            total = int(self._conn.execute("SELECT COALESCE(SUM(bytes),0) FROM thumb").fetchone()[0])
            if total <= max_bytes:
                return 0
            # page thumbnails are cheapest to regenerate, so evict them ahead of covers.
            order = (
                "ORDER BY CASE variant WHEN 'page' THEN 0 ELSE 1 END, accessed_at ASC"
            )
            for archive_id, page, variant, nbytes in self._conn.execute(
                f"SELECT archive_id, page, variant, bytes FROM thumb {order}"
            ).fetchall():
                if total - freed <= max_bytes:
                    break
                self._conn.execute(
                    "DELETE FROM thumb WHERE archive_id=? AND page=? AND variant=?",
                    (archive_id, page, variant),
                )
                freed += nbytes
            self._conn.commit()
        return freed

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM thumb")
            self._conn.commit()


def _encode_page_thumb(reader: ArchiveReader, page_id: str, size: int, quality: int) -> bytes:
    """Generate a thumbnail for one page from an open reader, preferring shrink-on-load."""
    if isinstance(reader, PdfArchiveReader):
        # Render the pdf page small directly rather than full-size then shrink.
        index = int(Path(page_id).stem) - 1
        png = reader.render_page_index(index, scale=1.0, fmt="png")
        return imaging.thumbnail_from_bytes(png, size, fmt="webp", quality=quality)
    raw = reader.read_page_bytes(page_id)
    return imaging.thumbnail_from_bytes(raw, size, fmt="webp", quality=quality)


def generate_cover(
    store: ThumbStore, archive_id: str, reader: ArchiveReader, *, size: int, quality: int
) -> bool:
    """Generate + store the cover (first page). Returns ``True`` on success."""
    pages = reader.list_pages()
    if not pages:
        return False
    data = _encode_page_thumb(reader, pages[0], size, quality)
    store.put(archive_id, COVER_PAGE, VARIANT_COVER, "image/webp", data)
    return True


def generate_page_thumb(
    store: ThumbStore, archive_id: str, reader: ArchiveReader, page_index: int, *,
    size: int, quality: int,
) -> bytes | None:
    """Generate, store and return one page-grid thumbnail (0-based ``page_index``)."""
    pages = reader.list_pages()
    if not 0 <= page_index < len(pages):
        return None
    data = _encode_page_thumb(reader, pages[page_index], size, quality)
    store.put(archive_id, page_index, VARIANT_PAGE, "image/webp", data)
    return data
