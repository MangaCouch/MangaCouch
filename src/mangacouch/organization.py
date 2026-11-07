from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .database import Library, write_personal
from .errors import InvalidInput, NotFound, RevisionConflict
from .models import Model, PersonalState, Tag, canonical_json, normalized
from .searching import SearchOptions, rule_conditions, search_count
from .source_checks import enqueue_check

RECEIPT_RETENTION = 7 * 86400
BULK_BATCH = 50


class BulkTarget(Model):
    book_id: Annotated[str, Field(min_length=1, max_length=64)]
    metadata_revision: Annotated[int, Field(ge=1)]
    personal_revision: Annotated[int, Field(ge=1)]


class BulkAction(Model):
    kind: Literal["rating", "completed", "add_collection", "source"]
    rating: Annotated[float, Field(ge=0, le=5, multiple_of=0.5, allow_inf_nan=False)] | None = None
    completed: bool = True
    target_id: Annotated[str, Field(max_length=64)] | None = None
    target_revision: Annotated[int, Field(ge=1)] | None = None

    @model_validator(mode="after")
    def required_values(self):
        if self.kind == "add_collection" and (self.target_id is None or self.target_revision is None):
            raise ValueError("A target and revision are required")
        return self


class BulkRequest(Model):
    id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    action: BulkAction
    targets: Annotated[list[BulkTarget], Field(min_length=1, max_length=500)]

    @model_validator(mode="after")
    def unique_targets(self):
        if len({item.book_id for item in self.targets}) != len(self.targets):
            raise ValueError("Duplicate targets")
        return self


class BulkSearchRequest(Model):
    id: Annotated[str, Field(pattern=r"^[0-9a-f-]{36}$")]
    action: BulkAction
    query: Annotated[str, Field(max_length=1024)] = ""
    options: SearchOptions = Field(default_factory=SearchOptions)
    expected_count: Annotated[int, Field(ge=1, le=100000)]
    exclude: Annotated[list[Annotated[str, Field(min_length=1, max_length=64)]], Field(max_length=500)] = (
        Field(default_factory=list)
    )


class Organization:
    def __init__(self, library: Library):
        self.library = library

    def aliases(self, *, after: str = "", limit: int = 100) -> list[dict]:
        if not 1 <= limit <= 200 or len(after) > 2048:
            raise InvalidInput("Invalid alias page")
        with self.library.connection() as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT namespace,value,translation FROM tag_alias "
                    "WHERE namespace||char(9)||value>? ORDER BY namespace||char(9)||value LIMIT ?",
                    (after, limit),
                )
            ]

    def alias(self, tag: Tag, translation: str, expected: str | None) -> None:
        namespace, value = normalized(tag.namespace), normalized(tag.value)
        if any("\t" in item for item in (namespace, value)) or len(translation) > 512:
            raise InvalidInput("Invalid alias")
        translation = translation.strip()
        with self.library.transaction() as db:
            row = db.execute(
                "SELECT translation FROM tag_alias WHERE namespace=? AND value=?", (namespace, value)
            ).fetchone()
            if (row[0] if row else None) != expected:
                raise RevisionConflict("Alias changed")
            if translation:
                db.execute(
                    "INSERT INTO tag_alias VALUES(?,?,?,?) ON CONFLICT(namespace,value) "
                    "DO UPDATE SET translation=excluded.translation,search_text=excluded.search_text",
                    (namespace, value, translation, normalized(translation)),
                )
            else:
                db.execute("DELETE FROM tag_alias WHERE namespace=? AND value=?", (namespace, value))

    def create_bulk(self, request: BulkRequest | BulkSearchRequest) -> dict:
        """Record a bulk job; the maintenance loop applies it through advance()."""
        data = canonical_json(request.model_dump())
        with self.library.transaction() as db:
            old = db.execute("SELECT request_json FROM bulk_job WHERE id=?", (request.id,)).fetchone()
            if old:
                if old[0] != data:
                    raise RevisionConflict("Bulk identifier belongs to another operation")
            else:
                action = request.action
                if action.kind == "add_collection":
                    row = db.execute("SELECT * FROM collection WHERE id=?", (action.target_id,)).fetchone()
                    if row is None:
                        raise NotFound("Collection not found", code="collection_not_found")
                    if db.execute(
                        "SELECT 1 FROM collection_rule WHERE collection_id=?", (action.target_id,)
                    ).fetchone():
                        raise InvalidInput(
                            "Dynamic collections follow their rule; freeze it before adding books",
                            code="dynamic_collection",
                        )
                    if row["revision"] != action.target_revision:
                        raise RevisionConflict("Organization target changed")
                db.execute(
                    "INSERT INTO bulk_job VALUES(?,?,'running',?)", (request.id, data, int(time.time()))
                )
                if isinstance(request, BulkSearchRequest):
                    if search_count(db, request.query, request.options) != request.expected_count:
                        raise RevisionConflict("Search result count changed; refresh before selecting all")
                    conditions, parameters = rule_conditions(request.query, request.options)
                    if request.exclude:
                        conditions.append("b.id NOT IN (" + ",".join("?" for _ in request.exclude) + ")")
                        parameters.extend(request.exclude)
                    where = " AND ".join(conditions) or "1"
                    db.execute(
                        "INSERT INTO bulk_item(job_id,position,book_id,metadata_revision,personal_revision) "
                        "SELECT ?,row_number() OVER (ORDER BY b.id)-1,b.id,b.metadata_revision,p.revision "
                        f"FROM book b JOIN personal p ON p.book_id=b.id WHERE {where}",
                        (request.id, *parameters),
                    )
                else:
                    db.executemany(
                        "INSERT INTO bulk_item(job_id,position,book_id,metadata_revision,personal_revision) "
                        "VALUES(?,?,?,?,?)",
                        (
                            (request.id, index, item.book_id, item.metadata_revision, item.personal_revision)
                            for index, item in enumerate(request.targets)
                        ),
                    )
        return self.bulk(request.id)

    def bulk(self, identifier: str, *, after: int = -1, limit: int = 200) -> dict:
        if after < -1 or not 1 <= limit <= 500:
            raise InvalidInput("Invalid bulk report page")
        with self.library.connection() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM bulk_job WHERE id=?", (identifier,)).fetchone()
            if row is None:
                raise NotFound("Bulk job not found")
            items = [
                dict(item)
                for item in db.execute(
                    "SELECT position,book_id,state FROM bulk_item WHERE job_id=? AND position>? "
                    "ORDER BY position LIMIT ?",
                    (identifier, after, limit + 1),
                )
            ]
            return {
                "id": identifier,
                "state": row["state"],
                "action": json.loads(row["request_json"])["action"],
                "items": items[:limit],
                "after_position": items[limit - 1]["position"] if len(items) > limit else None,
                "counts": dict.fromkeys(("pending", "done", "conflict", "missing", "error"), 0)
                | dict(
                    db.execute(
                        "SELECT state,count(*) FROM bulk_item WHERE job_id=? GROUP BY state", (identifier,)
                    ).fetchall()
                ),
            }

    def purge(self) -> int:
        """Drop finished bulk-job receipts older than the retention window."""
        cutoff = int(time.time()) - RECEIPT_RETENTION
        with self.library.transaction() as db:
            return db.execute("DELETE FROM bulk_job WHERE state='done' AND created_at<?", (cutoff,)).rowcount

    def advance(self, *, budget: float = 0.5, limit: int | None = None) -> bool:
        """Apply pending items of running jobs in batches; True while work remains."""
        deadline = time.monotonic() + budget
        remaining = limit
        while time.monotonic() < deadline and (remaining is None or remaining > 0):
            size = BULK_BATCH if remaining is None else min(BULK_BATCH, remaining)
            with self.library.transaction() as db:
                job = db.execute(
                    "SELECT * FROM bulk_job WHERE state='running' ORDER BY created_at,id LIMIT 1"
                ).fetchone()
                if job is None:
                    return False
                items = db.execute(
                    "SELECT * FROM bulk_item WHERE job_id=? AND state='pending' ORDER BY position LIMIT ?",
                    (job["id"], size),
                ).fetchall()
                if not items:
                    db.execute("UPDATE bulk_job SET state='done' WHERE id=?", (job["id"],))
                    continue
                action = BulkAction.model_validate_json(
                    canonical_json(json.loads(job["request_json"])["action"])
                )
                if action.kind == "add_collection":
                    results = self._add_to_collection(db, action, items)
                elif action.kind == "source":
                    results = [self._queue_source(db, item) for item in items]
                else:
                    results = [self._apply_personal(db, item, action) for item in items]
                db.executemany(
                    "UPDATE bulk_item SET state=? WHERE job_id=? AND position=?",
                    (
                        (state, job["id"], item["position"])
                        for item, state in zip(items, results, strict=True)
                    ),
                )
            if remaining is not None:
                remaining -= len(items)
        with self.library.connection() as db:
            return db.execute("SELECT 1 FROM bulk_job WHERE state='running' LIMIT 1").fetchone() is not None

    @staticmethod
    def _queue_source(db: sqlite3.Connection, item: sqlite3.Row) -> str:
        row = db.execute(
            "SELECT metadata_revision,json_extract(metadata_json,'$.source') FROM book WHERE id=?",
            (item["book_id"],),
        ).fetchone()
        if row is None:
            return "missing"
        if row[0] != item["metadata_revision"]:
            return "conflict"
        if row[1] is None:
            return "error"
        enqueue_check(db, item["book_id"])
        db.execute(
            "UPDATE source_check SET state='queued',generation=generation+1,error_code=NULL,"
            "reasons_json='[]',retry_at=0,updated_at=? WHERE book_id=?",
            (int(time.time()), item["book_id"]),
        )
        return "done"

    @staticmethod
    def _apply_personal(db: sqlite3.Connection, item: sqlite3.Row, action: BulkAction) -> str:
        row = db.execute(
            "SELECT state_json,revision FROM personal WHERE book_id=?", (item["book_id"],)
        ).fetchone()
        if row is None:
            return "missing"
        if row["revision"] != item["personal_revision"]:
            return "conflict"
        old = PersonalState.model_validate_json(row["state_json"])
        new = old.model_copy(deep=True)
        if action.kind == "rating":
            new.rating = action.rating
        else:
            new.completed = action.completed
            if not action.completed:
                new.resume_page = None
        write_personal(db, item["book_id"], old, new, row["revision"])
        return "done"

    @staticmethod
    def _add_to_collection(db: sqlite3.Connection, action: BulkAction, items: list[sqlite3.Row]) -> list[str]:
        """Append one batch in a single set-based insert; membership edits commute with other edits."""
        target = action.target_id
        if (
            db.execute("SELECT 1 FROM collection WHERE id=?", (target,)).fetchone() is None
            or db.execute("SELECT 1 FROM collection_rule WHERE collection_id=?", (target,)).fetchone()
        ):
            return ["missing"] * len(items)
        identifiers = canonical_json([item["book_id"] for item in items])
        existing = {
            row[0]
            for row in db.execute(
                "SELECT id FROM book WHERE id IN (SELECT value FROM json_each(?))", (identifiers,)
            )
        }
        base = db.execute(
            "SELECT coalesce(max(position),-1) FROM collection_item WHERE collection_id=?", (target,)
        ).fetchone()[0]
        inserted = db.execute(
            "INSERT INTO collection_item(collection_id,book_id,position) "
            "SELECT ?,j.value,?+row_number() OVER (ORDER BY j.key) FROM json_each(?) j "
            "WHERE j.value IN (SELECT id FROM book) AND NOT EXISTS("
            "SELECT 1 FROM collection_item c WHERE c.collection_id=? AND c.book_id=j.value)",
            (target, base, identifiers, target),
        ).rowcount
        if inserted:
            db.execute("UPDATE collection SET revision=revision+1 WHERE id=?", (target,))
        return ["done" if item["book_id"] in existing else "missing" for item in items]
