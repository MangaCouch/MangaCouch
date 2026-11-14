from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import get_args

from .database import RevisionConflict
from .errors import InvalidInput, NotFound
from .eze_cache import EzeCache, queue_eze
from .ingest import FileLibrary
from .models import FieldName, Metadata, Source, canonical_json
from .review import Review
from .source_adapters import Sources, bounded_comments
from .source_http import SourceError

RATE_LIMITED = "source_rate_limited"
DEFER_SECONDS = 60
"""Wait for deferred source work when the rate limit gave none."""
FAILED_TASK = 2**62
"""A failed manual metadata task keeps its URL until an explicit retry or replacement."""


def _curated(metadata: Metadata) -> dict:
    return metadata.model_dump(mode="json")


def candidate_evidence(candidate: dict | None) -> dict | None:
    if candidate is None:
        return None
    return {
        **{key: value for key, value in candidate.items() if key not in {"source_rating", "field_sources"}},
        "field_sources": {
            key: value for key, value in candidate.get("field_sources", {}).items() if key != "source_rating"
        },
    }


def store_notes(db, book_id: str, observation: dict) -> None:
    """Save the source notes and comments of one observation without touching book metadata.

    Only a complete comment fetch replaces the stored comments, so an empty result clears them
    only when the source really has none. Comments that were not requested or could not be fetched
    keep the stored ones; a partial (budget-bounded) fetch updates the comments it saw and keeps
    the stored ones it could not see, within the shared budget.
    """
    notes = dict(observation["notes"])
    url = Source.model_validate(notes["source"]).key
    state = observation["comments_state"]
    comments, _ = bounded_comments(observation["comments"])
    if state != "complete":
        previous = db.execute(
            "SELECT notes_json,comments_json FROM source_note WHERE book_id=? AND source_url=?",
            (book_id, url),
        ).fetchone()
        kept = json.loads(previous[1]) if previous else []
        if state == "partial":
            seen = {comment["comment_id"] for comment in comments}
            comments, _ = bounded_comments(
                [*comments, *(comment for comment in kept if comment["comment_id"] not in seen)]
            )
        else:
            comments = kept
            if previous:
                old = json.loads(previous[0])
                notes.update(comments_pages=old["comments_pages"], comments_complete=old["comments_complete"])
                # A deferred comment fetch that failed (SourceChecks._settle) stays shown until one works.
                if "comments_failure" in old:
                    notes["comments_failure"] = old["comments_failure"]
    db.execute(
        "INSERT INTO source_note(book_id,source_url,notes_json,comments_json) VALUES(?,?,?,?) "
        "ON CONFLICT(book_id,source_url) DO UPDATE SET "
        "notes_json=excluded.notes_json,comments_json=excluded.comments_json",
        (book_id, url, canonical_json(notes), canonical_json(comments)),
    )


def defer(db, book_id: str, source_url: str, kind: str, *, manual: bool, retry_after: int) -> int:
    """Queue source work a rate limit put off (source_task); returns when it runs (epoch seconds).

    A task already queued for the same book, source and kind takes the new time; asking by hand
    keeps it out of the background share for good.
    """
    retry_at = int(time.time()) + (retry_after or DEFER_SECONDS)
    db.execute(
        "INSERT INTO source_task(book_id,source_url,kind,manual,retry_at,created_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(book_id,source_url,kind) DO UPDATE SET manual=max(manual,excluded.manual),"
        "retry_at=excluded.retry_at,created_at=excluded.created_at",
        (book_id, source_url, kind, int(manual), retry_at, int(time.time())),
    )
    return retry_at


class OnlineMetadata:
    def __init__(self, files: FileLibrary, sources: Sources):
        self.files, self.library, self.sources = files, files.library, sources
        self.review = Review(files)
        self.eze_cache = EzeCache(files)

    def refresh(
        self,
        book_id: str,
        source: Source,
        expected_revision: int,
        *,
        cancelled=None,
        check_generation: int | None = None,
        link_source: bool = False,
        comments: bool | None = None,
    ) -> dict:
        """Fetch `source` and apply it; `comments` defaults to the comments setting."""
        if self.library.get_book(book_id).metadata_revision != expected_revision:
            raise RevisionConflict("Metadata changed; reload and retry")
        observation = self.sources.preferred(
            source,
            comments=self.sources.http.settings.comments if comments is None else comments,
            cancelled=cancelled,
        )
        if cancelled and cancelled():
            raise InterruptedError
        return self.apply(
            book_id,
            observation,
            expected_revision,
            check_generation=check_generation,
            link_source=link_source,
        )

    def notes(self, book_id: str) -> dict:
        """Source notes and scraped comments of a book, oldest source first."""
        with self.library.connection() as db:
            if db.execute("SELECT 1 FROM book WHERE id=?", (book_id,)).fetchone() is None:
                raise NotFound("Book not found")
            rows = db.execute(
                "SELECT notes_json,comments_json FROM source_note WHERE book_id=? ORDER BY rowid", (book_id,)
            ).fetchall()
        return {
            "notes": [json.loads(row[0]) for row in rows],
            "comments": [comment for row in rows for comment in json.loads(row[1])],
        }

    def refresh_comments(self, book_id: str, source: Source, *, cancelled=None) -> dict:
        """Fetch fresh comments for a linked source; curated metadata and review state stay as they are."""
        known = self.library.get_book(book_id).metadata
        linked = [
            known.source,
            *[Source.model_validate(note["source"]) for note in self.notes(book_id)["notes"]],
        ]
        if source not in linked:
            raise InvalidInput("Comments require a linked source")
        observation = self.sources.fetch(source, comments=True, cancelled=cancelled)
        if observation.get("comments_error"):
            raise SourceError(observation["comments_error"], observation.get("comments_retry_after", 0))
        with self.library.transaction() as db:
            store_notes(db, book_id, observation)
            self._comments_owed(db, book_id, source.url, observation)
        return {**self.notes(book_id), "comments_error": None}

    def defer(self, book_id: str, source: Source, kind: str, retry_after: int) -> int:
        """Queue a refresh (`kind` 'metadata') or a comment fetch asked for by hand that a rate
        limit stopped; it runs in the background later. Returns when (epoch seconds)."""
        with self.library.transaction() as db:
            if db.execute("SELECT 1 FROM book WHERE id=?", (book_id,)).fetchone() is None:
                raise NotFound("Book not found")
            return defer(db, book_id, source.url, kind, manual=True, retry_after=retry_after)

    @staticmethod
    def _comments_owed(db, book_id: str, url: str, observation: dict) -> None:
        """Comments a rate limit kept from `observation` are fetched again later; fetched ones
        settle what was owed for that source."""
        if observation.get("comments_error") == RATE_LIMITED:
            defer(
                db,
                book_id,
                url,
                "comments",
                manual=False,
                retry_after=observation.get("comments_retry_after", 0),
            )
        elif observation.get("comments_state") in {"complete", "partial"}:
            db.execute(
                "DELETE FROM source_task WHERE book_id=? AND source_url=? AND kind='comments'", (book_id, url)
            )

    def apply(
        self,
        book_id: str,
        observation: dict,
        expected_revision: int | None,
        *,
        check_generation: int | None = None,
        link_source: bool = False,
    ) -> dict:
        remote = Metadata.model_validate(observation["metadata"])
        source = remote.source
        if source is None:
            raise ValueError("Source observation requires an identity")
        review = self.review
        verified_stamp = review.verification_stamp(book_id)
        with self.files.root.mutation(), self.library.transaction() as db:
            if check_generation is not None:
                check = db.execute(
                    "SELECT state,generation FROM source_check WHERE book_id=?", (book_id,)
                ).fetchone()
                if check is None or tuple(check) != ("running", check_generation):
                    raise InterruptedError
            book = self.library.get_book(book_id, db)
            if expected_revision is not None and book.metadata_revision != expected_revision:
                raise RevisionConflict("Metadata changed during source refresh")
            expected_revision = book.metadata_revision
            values = book.metadata.model_dump(mode="json")
            current_source = book.metadata.source
            if current_source is not None and current_source.key == source.key:
                source = current_source
                remote = remote.model_copy(update={"source": source})
                observation = {
                    **observation,
                    "metadata": {**observation["metadata"], "source": source.model_dump(mode="json")},
                    "notes": {**observation["notes"], "source": source.model_dump(mode="json")},
                    "comments": [
                        {**comment, "source": source.model_dump(mode="json")}
                        for comment in observation["comments"]
                    ],
                }
            candidate = _curated(remote)
            same_work = (
                current_source is None
                or current_source == source
                or (
                    current_source.gallery_id == source.gallery_id
                    and current_source.gallery_token == source.gallery_token
                )
            )
            other_tags = (
                [
                    tag
                    for (notes,) in db.execute(
                        "SELECT notes_json FROM source_note WHERE book_id=? AND source_url!=? ORDER BY rowid",
                        (book_id, source.key),
                    )
                    for tag in json.loads(notes)["tags"]
                ]
                if same_work
                else []
            )
            candidate["tags"] = [*other_tags, *candidate["tags"]]
            candidate["field_sources"] = {
                field: "online"
                for field in get_args(FieldName)
                if candidate[field] not in (None, "", []) or field in {"tags", "newer_versions"}
            }
            for field in candidate["field_sources"]:
                if same_work and values[field] in (None, "", []) and field not in values["protected_fields"]:
                    values[field] = candidate[field]
                    values["field_sources"][field] = "online"
            if same_work or link_source:
                store_notes(db, book_id, observation)
                self._comments_owed(db, book_id, source.url, observation)
            row = db.execute(
                "SELECT report_json,decisions_json FROM metadata_import WHERE book_id=?", (book_id,)
            ).fetchone()
            report, decisions = (json.loads(row[0]), json.loads(row[1])) if row else ({}, {})
            name = "online:" + source.site
            fresh = _curated(Metadata.model_validate_json(canonical_json(candidate)))
            candidate_changed = candidate_evidence(
                report.get("candidates", {}).get(name)
            ) != candidate_evidence(fresh)
            if candidate_changed:
                # A decision only covers the candidate it was made for.
                decisions.pop(name, None)
            report.setdefault("candidates", {})[name] = fresh
            report["sources"] = list(dict.fromkeys([*report.get("sources", []), name]))
            db.execute(
                "INSERT INTO metadata_import(book_id,report_json,decisions_json) VALUES(?,?,?) ON "
                "CONFLICT(book_id) DO UPDATE SET "
                "report_json=excluded.report_json,decisions_json=excluded.decisions_json",
                (book_id, canonical_json(report), canonical_json(decisions)),
            )
            kept = {key: value for key, value in observation.items() if key != "comments"}
            if kept.get("archive_variant") in (None, "unknown"):
                # Which archive (original/resample) a download fetched describes the content, not
                # the gallery page: a later refresh of the same bytes (EH or ExHentai) keeps it.
                previous = db.execute(
                    "SELECT json_extract(observation_json,'$.archive_variant') FROM source_observation "
                    "WHERE book_id=? AND json_extract(observation_json,'$.content_sha256')=? "
                    "AND json_extract(observation_json,'$.archive_variant') IN ('original','resample') "
                    "LIMIT 1",
                    (book_id, book.content.sha256),
                ).fetchone()
                if previous:
                    kept["archive_variant"] = previous[0]
            db.execute(
                "INSERT INTO source_observation VALUES(?,?,?) ON CONFLICT(book_id,source_url) DO "
                "UPDATE SET observation_json=excluded.observation_json",
                (book_id, source.key, canonical_json({**kept, "content_sha256": book.content.sha256})),
            )
            self.library.write_metadata(
                db, book_id, Metadata.model_validate_json(canonical_json(values)), expected_revision
            )
            if values.get("source") and Source.model_validate(values["source"]).key == source.key:
                queue_eze(
                    db,
                    book_id,
                    remote,
                    fetched_at=observation["fetched_at"],
                    gdata=observation.get("gdata"),
                )
            review.record_event(
                db,
                book_id,
                "source_refresh",
                {
                    "source": source.model_dump(),
                    "fetched_at": observation["fetched_at"],
                    "comments_error": observation.get("comments_error"),
                },
            )
            outcome = review.auto_check(
                db,
                book_id,
                record_failure=True,
                verified_stamp=verified_stamp,
                recheck_candidates=candidate_changed,
            )
            # Any refresh counts as a check, so the periodic refresh covers the book from now on.
            if link_source:
                db.execute(
                    "DELETE FROM source_task WHERE book_id=? AND kind='metadata' AND retry_at=?",
                    (book_id, FAILED_TASK),
                )
            db.execute(
                "INSERT INTO source_check(book_id,state,reasons_json,updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET state=excluded.state,generation=generation+1,"
                "error_code=NULL,reasons_json=excluded.reasons_json,retry_at=0,"
                "updated_at=excluded.updated_at",
                (
                    book_id,
                    "needs_review" if outcome["reasons"] else "done",
                    canonical_json(outcome["reasons"]),
                    int(time.time()),
                ),
            )
        try:
            self.eze_cache.sync_one(book_id)
        except (OSError, ValueError, sqlite3.Error):
            logging.getLogger(__name__).warning("Source metadata saved; Eze sidecar sync remains pending")
        return {
            "comments_error": observation.get("comments_error"),
            "revision": self.library.get_book(book_id).metadata_revision,
        }
