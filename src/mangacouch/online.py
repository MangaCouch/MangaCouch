from __future__ import annotations

import json
import logging
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

VOLATILE_FIELDS = ("comments", "source_notes")
"""Scraped fields that live in source_note, never in a book's curated metadata."""


def _curated(metadata: Metadata) -> dict:
    values = metadata.model_dump(mode="json")
    for name in VOLATILE_FIELDS:
        values.pop(name, None)
    return values


def store_notes(db, book_id: str, observation: dict) -> None:
    """Save the source notes and comments of one observation without touching book metadata.

    A comment fetch that failed keeps the comments stored earlier for that source.
    """
    notes = dict(observation["notes"])
    url = Source.model_validate(notes["source"]).url
    comments, _ = bounded_comments(observation["comments"])
    if observation.get("comments_error"):
        previous = db.execute(
            "SELECT notes_json,comments_json FROM source_note WHERE book_id=? AND source_url=?",
            (book_id, url),
        ).fetchone()
        comments = json.loads(previous[1]) if previous else []
        if previous:
            old = json.loads(previous[0])
            notes.update(comments_pages=old["comments_pages"], comments_complete=old["comments_complete"])
    db.execute(
        "INSERT INTO source_note(book_id,source_url,notes_json,comments_json) VALUES(?,?,?,?) "
        "ON CONFLICT(book_id,source_url) DO UPDATE SET "
        "notes_json=excluded.notes_json,comments_json=excluded.comments_json",
        (book_id, url, canonical_json(notes), canonical_json(comments)),
    )


class OnlineMetadata:
    def __init__(self, files: FileLibrary, sources: Sources):
        self.files, self.library, self.sources = files, files.library, sources

    def refresh(
        self,
        book_id: str,
        source: Source,
        expected_revision: int,
        *,
        cancelled=None,
        check_generation: int | None = None,
        link_source: bool = False,
    ) -> dict:
        observation = self.sources.preferred(
            source, comments=self.sources.http.settings.comments, cancelled=cancelled
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

    def refresh_comments(self, book_id: str, source: Source) -> dict:
        """Fetch fresh comments for a linked source; curated metadata and review state stay as they are."""
        known = self.library.get_book(book_id).metadata
        linked = [
            known.source,
            *[Source.model_validate(note["source"]) for note in self.notes(book_id)["notes"]],
        ]
        if source not in linked:
            raise InvalidInput("Comments require a linked source")
        observation = self.sources.fetch(source, comments=True)
        if observation.get("comments_error"):
            raise SourceError(observation["comments_error"])
        with self.library.transaction() as db:
            store_notes(db, book_id, observation)
        return {**self.notes(book_id), "comments_error": None}

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
        with self.files.root.mutation():
            review = Review(self.files)
            verified_stamp = review._verification_stamp(book_id)
            with self.library.transaction() as db:
                if check_generation is not None:
                    check = db.execute(
                        "SELECT state,generation FROM source_check WHERE book_id=?", (book_id,)
                    ).fetchone()
                    if check is None or tuple(check) != ("running", check_generation):
                        raise InterruptedError
                book = self.library.get_book(book_id)
                if expected_revision is not None and book.metadata_revision != expected_revision:
                    raise RevisionConflict("Metadata changed during source refresh")
                expected_revision = book.metadata_revision
                values = book.metadata.model_dump(mode="json")
                candidate = _curated(remote)
                current_source = book.metadata.source
                same_work = (
                    current_source is None
                    or current_source == source
                    or (
                        current_source.site in {"ehentai", "exhentai"}
                        and source.site in {"ehentai", "exhentai"}
                        and current_source.gallery_id == source.gallery_id
                        and current_source.gallery_token == source.gallery_token
                    )
                )
                other_tags = (
                    [
                        tag
                        for (notes,) in db.execute(
                            "SELECT notes_json FROM source_note WHERE book_id=? AND source_url!=? "
                            "ORDER BY rowid",
                            (book_id, source.url),
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
                    if (
                        same_work
                        and values[field] in (None, "", [])
                        and field not in values["protected_fields"]
                    ):
                        values[field] = candidate[field]
                        values["field_sources"][field] = "online"
                if same_work or link_source:
                    store_notes(db, book_id, observation)
                row = db.execute(
                    "SELECT report_json,decisions_json FROM metadata_import WHERE book_id=?", (book_id,)
                ).fetchone()
                report, decisions = (json.loads(row[0]), json.loads(row[1])) if row else ({}, {})
                name = "online:" + source.site
                fresh = _curated(Metadata.model_validate_json(canonical_json(candidate)))
                if report.get("candidates", {}).get(name) != fresh:
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
                db.execute(
                    "INSERT INTO source_observation VALUES(?,?,?) ON CONFLICT(book_id,source_url) DO "
                    "UPDATE SET observation_json=excluded.observation_json",
                    (book_id, source.url, canonical_json({**kept, "content_sha256": book.content.sha256})),
                )
                self.library.write_metadata(
                    db, book_id, Metadata.model_validate_json(canonical_json(values)), expected_revision
                )
                if same_work or link_source:
                    queue_eze(
                        db,
                        book_id,
                        remote,
                        fetched_at=observation["fetched_at"],
                        gdata=observation.get("gdata"),
                    )
                review._event(
                    db,
                    book_id,
                    "source_refresh",
                    {
                        "source": source.model_dump(),
                        "fetched_at": observation["fetched_at"],
                        "comments_error": observation.get("comments_error"),
                    },
                )
                outcome = review._automatic(db, book_id, record_failure=True, verified_stamp=verified_stamp)
                db.execute(
                    "UPDATE source_check SET state=?,generation=generation+1,error_code=NULL,"
                    "reasons_json=?,retry_at=0,updated_at=? WHERE book_id=?",
                    (
                        "needs_review" if outcome["reasons"] else "done",
                        canonical_json(outcome["reasons"]),
                        int(time.time()),
                        book_id,
                    ),
                )
        try:
            EzeCache(self.files).sync_one(book_id)
        except (OSError, ValueError):
            logging.getLogger(__name__).warning("Source metadata saved; Eze sidecar sync remains pending")
        return {
            "comments_error": observation.get("comments_error"),
            "revision": self.library.get_book(book_id).metadata_revision,
        }
