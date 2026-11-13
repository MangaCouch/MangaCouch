"""Per-page visual fingerprints that back duplicate detection."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from bisect import bisect_left
from typing import Any

import numpy as np
import pyvips

from .archives import InvalidArchive, Limits, check_dimensions, display_size, image_media_type, orientation
from .database import Book
from .files import RootUnavailable
from .ingest import FileLibrary
from .limits import ImportCancelled
from .maintenance import Retry

LOGGER = logging.getLogger(__name__)
RADIUS = 3
MIN_CONTRAST = 8
PREVIEW = 256
MAX_BATCH = 1000


def bands(value: int) -> list[tuple[int, int]]:
    """Split a 64-bit dhash into RADIUS+1 segments; any two hashes within RADIUS share one."""
    result = []
    for segment in range(RADIUS + 1):
        start, end = 64 * segment // (RADIUS + 1), 64 * (segment + 1) // (RADIUS + 1)
        result.append((segment, (value >> start) & ((1 << (end - start)) - 1)))
    return result


def _preview(data: bytes, limits: Limits) -> tuple[Any, float]:
    """A grey preview of at most PREVIEW pixels per side and the page aspect ratio, both upright
    as the reader shows the page (JPEG EXIF orientation applied).

    JPEG and WebP pages shrink while decoding (libvips shrink-on-load); dimensions are checked
    from the header first, so oversized pages are rejected without a full decode.
    """
    media_type = image_media_type(data)
    header: Any = pyvips.Image.new_from_buffer(data, "", access="sequential")
    check_dimensions(header.width, header.height, header.bands, limits)
    rotation = orientation(header, media_type)
    width, height = display_size(header.width, header.height, rotation)
    aspect = width / height
    image = pyvips.Image.thumbnail_buffer(
        data, PREVIEW, height=PREVIEW, size="down", no_rotate=media_type != "image/jpeg", fail_on="error"
    )
    if image.hasalpha():
        image = image.flatten(background=[255, 255, 255])
    return image.colourspace("b-w").cast("uchar").copy_memory(), aspect


def feature(data: bytes, files: FileLibrary) -> dict:
    try:
        image, aspect = _preview(data, files.limits)
        small = image.thumbnail_image(9, height=8, size="force")
        pixels = np.frombuffer(small.write_to_memory(), dtype=np.uint8).reshape(8, 9)
        gray = bytes(image.thumbnail_image(16, height=16, size="force").write_to_memory())
    except pyvips.Error as error:
        raise InvalidArchive("Page could not be decoded") from error
    bits = pixels[:, 1:] > pixels[:, :-1]
    dhash = sum(int(bit) << index for index, bit in enumerate(bits.flat))
    return {
        "digest": hashlib.sha256(data).hexdigest(),
        "dhash": f"{dhash:016x}",
        "gray": gray,
        "aspect": aspect,
    }


def has_contrast(row) -> bool:
    """Near-blank and undecodable pages match everything, so they never take part in matching."""
    return bool(row["gray"]) and max(row["gray"]) - min(row["gray"]) >= MIN_CONTRAST


def matches(left, right) -> bool:
    distance = (int(left["dhash"], 16) ^ int(right["dhash"], 16)).bit_count()
    if left["digest"] == right["digest"]:
        return True
    aspect = abs(left["aspect"] - right["aspect"]) / max(left["aspect"], right["aspect"])
    difference = (
        np.abs(
            np.frombuffer(left["gray"], dtype=np.uint8).astype(np.int16)
            - np.frombuffer(right["gray"], dtype=np.uint8)
        ).mean()
        / 255
    )
    return bool(distance <= RADIUS and aspect <= 0.08 and difference <= 0.06)


def ordered_matching(edges: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """Longest chain of (left, right) page pairs that keeps both books in reading order."""
    # Descending right index within each left page prevents reuse of either page in the LIS.
    ordered = sorted(edges, key=lambda item: (item[0], -item[1]))
    tails: list[int] = []
    nodes: list[int] = []
    previous: list[int] = []
    for index, edge in enumerate(ordered):
        position = bisect_left(tails, edge[1])
        previous.append(nodes[position - 1] if position else -1)
        if position == len(tails):
            tails.append(edge[1])
            nodes.append(index)
        else:
            tails[position] = edge[1]
            nodes[position] = index
    result = []
    index = nodes[-1] if nodes else -1
    while index >= 0:
        result.append(ordered[index])
        index = previous[index]
    return result[::-1]


def candidates(db: sqlite3.Connection, source, *, after: int, limit: int) -> list[sqlite3.Row]:
    """Features of other books sharing a hash band with `source`, by id; limit+1 rows signal more."""
    segments = bands(int(source["dhash"], 16))
    conditions = " OR ".join("(segment=? AND value=?)" for _ in segments)
    args: list[int | str] = [after, *(item for pair in segments for item in pair), source["book_id"]]
    return db.execute(
        "SELECT f.* FROM page_feature f WHERE f.id>? AND f.id IN (SELECT feature_id FROM page_band WHERE "
        + conditions
        + ") AND f.book_id!=? ORDER BY f.id LIMIT ?",
        (*args, limit + 1),
    ).fetchall()


PENDING = (
    "SELECT b.id FROM book b WHERE b.availability='available' AND b.id>? "
    "AND b.indexed_pages<json_extract(b.content_json,'$.page_count') ORDER BY b.id LIMIT 1"
)


class PageFeatures:
    def __init__(self, files: FileLibrary):
        self.files, self.library = files, files.library
        self.retry = Retry()

    def status(self) -> dict:
        """Progress over available books; undecodable pages count as indexed."""
        with self.library.connection() as db:
            row = db.execute(
                "SELECT coalesce(sum(indexed_pages),0),"
                "coalesce(sum(json_extract(content_json,'$.page_count')),0) FROM book "
                "WHERE availability='available'"
            ).fetchone()
        return {"indexed_pages": row[0], "total_pages": row[1]}

    def index(self, limit: int = 3, *, time_budget: float | None = None) -> dict:
        """Fingerprint up to `limit` unindexed pages, stopping early once `time_budget` seconds pass.

        A missing or changed archive updates the
        book's availability, and a page that cannot be decoded is stored as failed so the indexer
        moves on; one bad page never marks the whole book unreadable. Fingerprints are stored
        together once the batch ends, under one lock and transaction.
        """
        if not 1 <= limit <= MAX_BATCH:
            raise ValueError("Invalid indexing batch")
        deadline = None if time_budget is None else time.monotonic() + time_budget
        computed: list[tuple[Book, int, dict]] = []
        book: Book | None = None
        pending: list[int] = []
        try:
            while len(computed) < limit and (deadline is None or time.monotonic() < deadline):
                if not pending:
                    book, pending = self._next_book(after=book.id if book else None)
                    if book is None:
                        break
                assert book is not None
                page = pending.pop(0)
                try:
                    value = feature(self.files.page_bytes(book.id, page), self.files)
                except (RootUnavailable, ImportCancelled):
                    raise
                except (InvalidArchive, ValueError):
                    value = {"digest": "", "dhash": "", "gray": b"", "aspect": 0.0, "failed": 1}
                except OSError:
                    state, _ = self.files.availability(book)
                    self.files.root.check()
                    if state == "available":
                        self.retry.failed(book.id)
                        pending = []
                        continue
                    self.files.observe_unavailable(book, state)
                    pending = []
                    continue
                self.retry.succeeded(book.id)
                computed.append((book, page, value))
        except BaseException:
            try:
                self._store(computed)
            except sqlite3.Error:
                LOGGER.exception("Page fingerprints remain pending after indexing failed")
            raise
        self._store(computed)
        return {"processed": len(computed)}

    def _next_book(self, *, after: str | None) -> tuple[Book | None, list[int]]:
        """The next book with unindexed pages and those pages, in order. Books of this batch that
        were left with pending pages (`after`) are skipped, so a failing book does not repeat."""
        with self.library.connection() as db:
            row = db.execute(PENDING, (after or "",)).fetchone()
            while row is not None and not self.retry.ready(row[0]):
                row = db.execute(PENDING, (row[0],)).fetchone()
            if row is None:
                return None, []
            book = self.library.get_book(row[0], db)
            done = {
                item[0] for item in db.execute("SELECT page FROM page_feature WHERE book_id=?", (book.id,))
            }
        return book, [index for index in range(book.content.page_count) if index not in done]

    def _store(self, computed: list[tuple[Book, int, dict]]) -> None:
        if not computed:
            return
        with self.files.root.mutation(), self.library.transaction() as db:
            for book, page, value in computed:
                if (
                    db.execute(
                        "SELECT 1 FROM book WHERE id=? AND sha256=?", (book.id, book.content.sha256)
                    ).fetchone()
                    is None
                ):
                    continue
                cursor = db.execute(
                    "INSERT OR IGNORE INTO page_feature(book_id,page,digest,dhash,gray,aspect,failed) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        book.id,
                        page,
                        value["digest"],
                        value["dhash"],
                        value["gray"],
                        value["aspect"],
                        value.get("failed", 0),
                    ),
                )
                if cursor.rowcount and value["dhash"]:
                    db.executemany(
                        "INSERT INTO page_band VALUES(?,?,?)",
                        (
                            (segment, number, cursor.lastrowid)
                            for segment, number in bands(int(value["dhash"], 16))
                        ),
                    )
