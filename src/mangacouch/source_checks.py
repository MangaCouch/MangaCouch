from __future__ import annotations

import json
import logging
import time

from .database import RevisionConflict
from .errors import InvalidInput, NotFound
from .models import canonical_json
from .source_http import SourceError


def enqueue_check(db, book_id: str) -> None:
    source = db.execute(
        "SELECT json_extract(metadata_json,'$.source') FROM book WHERE id=?", (book_id,)
    ).fetchone()[0]
    db.execute(
        "INSERT OR IGNORE INTO source_check(book_id,state,error_code,reasons_json,updated_at) "
        "VALUES(?,?,?,?,?)",
        (
            book_id,
            "queued" if source else "needs_review",
            None if source else "source_absent",
            "[]" if source else '["source_absent"]',
            int(time.time()),
        ),
    )


class SourceChecks:
    def __init__(self, online):
        self.online, self.library = online, online.library

    def get(self, book_id: str) -> dict | None:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM source_check WHERE book_id=?", (book_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["reasons"] = json.loads(value.pop("reasons_json"))
        return value

    def recover(self) -> None:
        """Checks interrupted by a restart wait for an explicit retry."""
        with self.library.transaction() as db:
            db.execute(
                "UPDATE source_check SET state='paused',generation=generation+1,"
                "error_code='restart_required' WHERE state IN ('queued','running')"
            )

    def schedule_refresh(self) -> int:
        now = int(time.time())
        with self.library.transaction() as db:
            result = db.execute(
                "UPDATE source_check SET state='queued',generation=generation+1,updated_at=? "
                "WHERE book_id IN (SELECT c.book_id FROM source_check c JOIN book b ON b.id=c.book_id "
                "WHERE c.state='done' AND c.updated_at<? AND "
                "json_extract(b.metadata_json,'$.source') IS NOT NULL "
                "ORDER BY c.updated_at,c.book_id LIMIT 20)",
                (now, now - 7 * 86400),
            )
            return result.rowcount

    def action(self, book_id: str, action: str, generation: int) -> dict:
        if action not in {"pause", "retry"}:
            raise InvalidInput("Unsupported check action")
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM source_check WHERE book_id=?", (book_id,)).fetchone()
            if row is None:
                raise NotFound("Source check not found")
            if row["generation"] != generation:
                raise RevisionConflict("Source check changed")
            if action == "retry":
                source = db.execute(
                    "SELECT json_extract(metadata_json,'$.source') FROM book WHERE id=?", (book_id,)
                ).fetchone()[0]
                if not source:
                    raise InvalidInput("Set an explicit source before retrying")
            db.execute(
                "UPDATE source_check SET state=?,generation=generation+1,error_code=NULL,"
                "reasons_json='[]',retry_at=0,updated_at=? WHERE book_id=?",
                ("paused" if action == "pause" else "queued", int(time.time()), book_id),
            )
        return self.get(book_id) or {}

    def step(self, stopping=lambda: False) -> bool:
        if stopping():
            return False
        with self.library.transaction() as db:
            row = db.execute(
                "SELECT * FROM source_check WHERE state='queued' AND retry_at<=? "
                "ORDER BY updated_at,book_id LIMIT 1",
                (int(time.time()),),
            ).fetchone()
            if row is None:
                return False
            identifier, generation = row["book_id"], row["generation"]
            db.execute("UPDATE source_check SET state='running' WHERE book_id=?", (identifier,))

        def cancelled():
            current = self.get(identifier)
            return (
                stopping()
                or current is None
                or (current["state"], current["generation"]) != ("running", generation)
            )

        try:
            book = self.library.get_book(identifier)
            if book.metadata.source is None:
                raise SourceError("source_absent")
            self.online.refresh(
                identifier,
                book.metadata.source,
                book.metadata_revision,
                cancelled=cancelled,
                check_generation=generation,
            )
        except Exception as error:
            if not isinstance(error, (SourceError, RevisionConflict, InterruptedError)):
                logging.getLogger(__name__).exception("Source check %s failed", identifier)
            code = (
                error.code
                if isinstance(error, SourceError)
                else "metadata_changed"
                if isinstance(error, RevisionConflict)
                else "interrupted"
                if isinstance(error, InterruptedError)
                else "source_check_failed"
            )
            if isinstance(error, SourceError) and error.code == "source_rate_limited":
                # The site is cooling down: wait, then continue without user action.
                state, retry_at = "queued", int(time.time()) + error.retry_after
            else:
                state, retry_at = ("paused" if code == "interrupted" else "needs_review"), 0
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE source_check SET state=?,error_code=?,reasons_json=?,retry_at=?,updated_at=? "
                    "WHERE book_id=? AND generation=? AND state='running'",
                    (
                        state,
                        code,
                        canonical_json([] if state == "queued" else [code]),
                        retry_at,
                        int(time.time()),
                        identifier,
                        generation,
                    ),
                )
        return True
