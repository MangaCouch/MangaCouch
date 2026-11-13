from __future__ import annotations

import json
import time
from bisect import bisect_left

from .cursors import decode_cursor, encode_cursor
from .errors import InvalidInput, NotFound
from .page_features import PageFeatures, has_contrast, matches

MAX_CANDIDATES = 500
MIN_SHARED_PAGES = 3
MIN_COVERAGE = 0.7
# Share of the longer book that must match too, so an anthology is not a duplicate of each chapter.
MIN_LONGER_COVERAGE = 0.5
IDLE_SECONDS = 30
PAIR_BATCH = 200
PAGE_BATCH = 32


def _common_page_edges(db, feature, others) -> list[tuple]:
    """Edges already written for a page that has since become too common to compare.

    While fewer books held it, the page stayed under the candidate limit and was matched;
    those edges would otherwise keep counting towards pairs that only share common pages.
    """
    similar = [other for other in others if has_contrast(other) and matches(feature, other)]
    pages = [(other["book_id"], other["page"]) for other in similar]
    if not pages:
        return []
    return [
        tuple(row)
        for row in db.execute(
            "WITH common(book_id,page) AS (SELECT value->>0,value->>1 FROM json_each(?)) "
            "SELECT e.* FROM common JOIN duplicate_edge e "
            "ON e.left_id=common.book_id AND e.left_page=common.page "
            "UNION SELECT e.* FROM common JOIN duplicate_edge e "
            "ON e.right_id=common.book_id AND e.right_page=common.page",
            (json.dumps(pages),),
        )
    ]


class Duplicates:
    def __init__(self, features: PageFeatures):
        self.features, self.library = features, features.library
        self.idle_until = 0.0
        self.comparison: tuple[str, int, int] | None = None

    def poke(self) -> None:
        """New pages may exist; stop backing off."""
        self.idle_until = 0.0

    def run(self, budget: float = 0.5) -> bool:
        """Index pages and compare features until the time budget is spent; True if work was done."""
        if time.monotonic() < self.idle_until:
            return False
        deadline = time.monotonic() + budget
        worked = False
        while time.monotonic() < deadline:
            indexed = self.features.index(limit=200, time_budget=max(0.0, deadline - time.monotonic()))[
                "processed"
            ]
            compared = self.step(deadline=deadline) if time.monotonic() < deadline else False
            if not indexed and not compared:
                break
            worked = True
        if not worked:
            self.idle_until = time.monotonic() + IDLE_SECONDS
        return worked

    def step(self, *, deadline: float | None = None) -> bool:
        """Compare a bounded page batch; a restart can safely repeat committed batches."""
        with self.library.connection() as db:
            db.execute("BEGIN")
            book = db.execute(
                "SELECT b.id FROM book b WHERE b.availability='available' "
                "AND b.indexed_pages=json_extract(b.content_json,'$.page_count') "
                "AND NOT EXISTS(SELECT 1 FROM duplicate_progress p WHERE p.book_id=b.id) "
                "ORDER BY b.id LIMIT 1"
            ).fetchone()
            if book is None:
                self.comparison = None
                return False
            identifier = book[0]
            signature = db.execute(
                "SELECT coalesce(max(id),0) FROM page_feature WHERE book_id=?", (identifier,)
            ).fetchone()[0]
            after = (
                self.comparison[2]
                if self.comparison and self.comparison[:2] == (identifier, signature)
                else -1
            )
            features = db.execute(
                "SELECT * FROM page_feature WHERE book_id=? AND page>? ORDER BY page LIMIT ?",
                (identifier, after, PAGE_BATCH),
            ).fetchall()
            edges, stale = [], []
            for feature in features:
                if deadline is not None and time.monotonic() >= deadline:
                    break
                if has_contrast(feature):
                    others = db.execute(
                        "SELECT f.* FROM page_feature f WHERE f.id IN ("
                        "SELECT DISTINCT other.feature_id FROM page_band mine "
                        "JOIN page_band other ON other.segment=mine.segment AND other.value=mine.value "
                        "JOIN page_feature candidate ON candidate.id=other.feature_id "
                        "JOIN duplicate_progress p ON p.book_id=candidate.book_id "
                        "WHERE mine.feature_id=? AND candidate.book_id<>? LIMIT ?)",
                        (feature["id"], identifier, MAX_CANDIDATES + 1),
                    ).fetchall()
                    if len(others) <= MAX_CANDIDATES:
                        for other in others:
                            if has_contrast(other) and matches(feature, other):
                                left, right = (
                                    (feature, other) if identifier < other["book_id"] else (other, feature)
                                )
                                edges.append((left["book_id"], right["book_id"], left["page"], right["page"]))
                    else:
                        stale.extend(_common_page_edges(db, feature, others))
                after = feature["page"]
            complete = (
                db.execute(
                    "SELECT 1 FROM page_feature WHERE book_id=? AND page>? LIMIT 1", (identifier, after)
                ).fetchone()
                is None
            )
        # A removal/index reset may have won while candidates were read. The operation lock
        # makes the final validation and insert agree with those mutations.
        with self.features.files.root.mutation(), self.library.transaction() as db:
            if (
                db.execute(
                    "SELECT 1 FROM book WHERE id=? AND availability='available' "
                    "AND indexed_pages=json_extract(content_json,'$.page_count') "
                    "AND (SELECT coalesce(max(id),0) FROM page_feature WHERE book_id=book.id)=?",
                    (identifier, signature),
                ).fetchone()
                is None
            ):
                self.comparison = None
                return True
            db.executemany(
                "INSERT OR IGNORE INTO duplicate_edge SELECT ?,?,?,? "
                "WHERE EXISTS(SELECT 1 FROM book WHERE id=?) AND EXISTS(SELECT 1 FROM book WHERE id=?)",
                ((*edge, edge[0], edge[1]) for edge in edges),
            )
            db.executemany(
                "DELETE FROM duplicate_edge WHERE left_id=? AND right_id=? AND left_page=? AND right_page=?",
                stale,
            )
            if complete:
                db.execute("INSERT OR IGNORE INTO duplicate_progress VALUES(?)", (identifier,))
        self.comparison = None if complete else (identifier, signature, after)
        return True

    def groups(self, *, after: str = "", limit: int = 50) -> dict:
        """Undecided pairs with enough ordered page coverage, keyset-paged by (left_id, right_id)."""
        if not 1 <= limit <= 200:
            raise InvalidInput("Invalid duplicate page")
        cursor: tuple[str, str] = decode_cursor(after, str, str) if after else ("", "")
        result: list[dict] = []
        exhausted = False
        with self.library.connection() as db:
            db.execute("BEGIN")
            comparing = dict(
                zip(
                    ("done", "total"),
                    db.execute(
                        "SELECT count(p.book_id),count(*) FROM book b "
                        "LEFT JOIN duplicate_progress p ON p.book_id=b.id "
                        "WHERE b.availability='available'"
                    ).fetchone(),
                    strict=True,
                )
            )
            while len(result) < limit:
                pairs = db.execute(
                    "SELECT e.left_id,e.right_id FROM duplicate_edge e LEFT JOIN duplicate_decision d "
                    "ON d.left_id=e.left_id AND d.right_id=e.right_id WHERE d.left_id IS NULL "
                    "AND EXISTS(SELECT 1 FROM duplicate_progress WHERE book_id=e.left_id) "
                    "AND EXISTS(SELECT 1 FROM duplicate_progress WHERE book_id=e.right_id) "
                    "AND (e.left_id,e.right_id)>(?,?) "
                    "GROUP BY e.left_id,e.right_id HAVING count(DISTINCT e.left_page)>=? "
                    "AND count(DISTINCT e.right_page)>=? ORDER BY e.left_id,e.right_id LIMIT ?",
                    (*cursor, MIN_SHARED_PAGES, MIN_SHARED_PAGES, PAIR_BATCH),
                ).fetchall()
                if not pairs:
                    exhausted = True
                    break
                books = {
                    book.id: book
                    for book in self.library.get_books(
                        list(dict.fromkeys(identifier for pair in pairs for identifier in pair)), db
                    )
                }
                for pair in pairs:
                    cursor = (pair[0], pair[1])
                    if pair[0] not in books or pair[1] not in books:
                        continue
                    left, right = books[pair[0]], books[pair[1]]
                    tails: list[int] = []
                    for (page,) in db.execute(
                        "SELECT right_page FROM duplicate_edge WHERE left_id=? AND right_id=? "
                        "ORDER BY left_page,right_page DESC",
                        cursor,
                    ):
                        position = bisect_left(tails, page)
                        if position == len(tails):
                            tails.append(page)
                        else:
                            tails[position] = page
                    matched = len(tails)
                    counts = (left.content.page_count, right.content.page_count)
                    coverage = matched / min(counts)
                    if coverage >= MIN_COVERAGE and matched / max(counts) >= MIN_LONGER_COVERAGE:
                        result.append(
                            {
                                "left_id": pair[0],
                                "right_id": pair[1],
                                "matched_pages": matched,
                                "coverage": coverage,
                            }
                        )
                        if len(result) == limit:
                            break
                if len(pairs) < PAIR_BATCH and len(result) < limit:
                    exhausted = True
                    break
        return {
            "items": result,
            "comparing": comparing,
            "after": None if exhausted else encode_cursor(*cursor),
            **self.features.status(),
        }

    def ignore(self, left: str, right: str) -> None:
        if left == right:
            raise InvalidInput("Choose two different books")
        left, right = sorted((left, right))
        with self.library.transaction() as db:
            for identifier in (left, right):
                if db.execute("SELECT 1 FROM book WHERE id=?", (identifier,)).fetchone() is None:
                    raise NotFound("Book not found", code="book_not_found")
            db.execute("INSERT OR IGNORE INTO duplicate_decision VALUES(?,?)", (left, right))
