from __future__ import annotations

import time

from .errors import InvalidInput, NotFound
from .models import canonical_json
from .page_features import PageFeatures, candidates, has_contrast, matches, ordered_matching

CANDIDATE_BATCH = 100
MIN_SHARED_PAGES = 3
MIN_COVERAGE = 0.7
IDLE_SECONDS = 30
PAIR_BATCH = 200


class Duplicates:
    def __init__(self, features: PageFeatures):
        self.features, self.library = features, features.library
        self.idle_until = 0.0

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
            compared = self.step()
            if not indexed and not compared:
                break
            worked = True
        if not worked:
            self.idle_until = time.monotonic() + IDLE_SECONDS
        return worked

    def step(self) -> bool:
        """Compare one feature against a batch of its candidates; False when caught up."""
        with self.library.connection() as db:
            cursor = db.execute("SELECT feature_id FROM duplicate_cursor").fetchone()
            pending = db.execute(
                "SELECT 1 FROM page_feature WHERE id>=? LIMIT 1", (max(1, cursor["feature_id"]),)
            ).fetchone()
        if pending is None:
            return False
        with self.library.transaction() as db:
            cursor = db.execute("SELECT * FROM duplicate_cursor").fetchone()
            feature = db.execute(
                "SELECT * FROM page_feature WHERE id>=? ORDER BY id LIMIT 1", (max(1, cursor["feature_id"]),)
            ).fetchone()
            if feature is None:
                return False
            found = (
                candidates(db, feature, after=cursor["candidate_id"], limit=CANDIDATE_BATCH)
                if has_contrast(feature)
                else []
            )
            for other in found[:CANDIDATE_BATCH]:
                # Each pair is considered once, when its later feature is indexed.
                if other["id"] >= feature["id"]:
                    continue
                if has_contrast(other) and matches(feature, other)[0]:
                    left, right = (
                        (feature, other) if feature["book_id"] < other["book_id"] else (other, feature)
                    )
                    db.execute(
                        "INSERT OR IGNORE INTO duplicate_edge VALUES(?,?,?,?)",
                        (left["book_id"], right["book_id"], left["page"], right["page"]),
                    )
            if len(found) > CANDIDATE_BATCH:
                db.execute(
                    "UPDATE duplicate_cursor SET feature_id=?,candidate_id=?",
                    (feature["id"], found[CANDIDATE_BATCH - 1]["id"]),
                )
            else:
                db.execute("UPDATE duplicate_cursor SET feature_id=?,candidate_id=0", (feature["id"] + 1,))
        return True

    def groups(self, *, after: str = "", limit: int = 50) -> dict:
        """Undecided pairs with enough ordered page coverage, keyset-paged by "left_id\tright_id"."""
        if not 1 <= limit <= 200 or len(after) > 256 or (after and "\t" not in after):
            raise InvalidInput("Invalid duplicate page")
        cursor = tuple(after.split("\t", 1)) if after else ("", "")
        result: list[dict] = []
        exhausted = False
        with self.library.connection() as db:
            db.execute("BEGIN")
            while len(result) < limit:
                pairs = db.execute(
                    "SELECT e.left_id,e.right_id FROM duplicate_edge e LEFT JOIN duplicate_decision d "
                    "ON d.left_id=e.left_id AND d.right_id=e.right_id WHERE d.left_id IS NULL "
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
                edges_by_pair: dict[tuple[str, str], list[tuple[int, int, int]]] = {
                    (pair[0], pair[1]): [] for pair in pairs
                }
                for row in db.execute(
                    "SELECT left_id,right_id,left_page,right_page FROM duplicate_edge WHERE "
                    "(left_id,right_id) IN (SELECT json_extract(value,'$[0]'),json_extract(value,'$[1]') "
                    "FROM json_each(?))",
                    (canonical_json([list(pair) for pair in pairs]),),
                ):
                    edges_by_pair[(row[0], row[1])].append((row[2], row[3], 0))
                for pair in pairs:
                    cursor = (pair[0], pair[1])
                    if pair[0] not in books or pair[1] not in books:
                        continue
                    left, right = books[pair[0]], books[pair[1]]
                    matched = len(ordered_matching(edges_by_pair[cursor]))
                    coverage = matched / min(left.content.page_count, right.content.page_count)
                    if coverage >= MIN_COVERAGE:
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
            "groups": result,
            "after": None if exhausted else "\t".join(cursor),
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
