from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import UTC, datetime
from typing import get_args

from .database import Book
from .errors import InvalidInput, NotFound, RevisionConflict
from .files import ContentChanged, file_stamp
from .ingest import FileLibrary, image_bytes
from .models import Content, FieldName, Metadata, Source, canonical_json, normalized

LOCAL_RULE = "local-confirmation/1"
EVENT_RETENTION = 90 * 86400
"""Seconds that routine review events (source refreshes, failed automatic checks) are kept."""


def _evidence(content: Content, metadata: dict) -> dict:
    fields = {field: metadata[field] for field in get_args(FieldName)}
    return {
        "rule": LOCAL_RULE,
        "content_sha256": content.sha256,
        "metadata_sha256": hashlib.sha256(canonical_json(fields).encode()).hexdigest(),
        "page_count": content.page_count,
        "byte_size": content.byte_size,
    }


def _equivalent(left: object, right: object) -> bool:
    if isinstance(left, str) and isinstance(right, str):
        return normalized(left.strip()) == normalized(right.strip())
    return left == right


def settle_source_check(db: sqlite3.Connection, book_id: str) -> None:
    """Close the book's source check once the book is confirmed.

    A check left at needs_review only reported review reasons (an error code means the check
    itself failed); confirming the book, by hand or automatically, resolves those reasons."""
    db.execute(
        "UPDATE source_check SET state='done',generation=generation+1,reasons_json='[]',retry_at=0,"
        "updated_at=unixepoch() WHERE book_id=? AND state='needs_review' AND error_code IS NULL",
        (book_id,),
    )


class Review:
    def __init__(self, files: FileLibrary):
        self.files = files
        self.library = files.library

    def _preview(self, db: sqlite3.Connection, book_id: str, *, available: bool) -> dict:
        """Review state of one book. `available` is the archive availability, resolved by the
        caller before any write transaction so no file I/O happens while holding the lock."""
        row = db.execute("SELECT * FROM book WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise NotFound("Book not found")
        content = Content.model_validate_json(row["content_json"])
        metadata = Metadata.model_validate_json(row["metadata_json"])
        imported = db.execute("SELECT * FROM metadata_import WHERE book_id=?", (book_id,)).fetchone()
        report = json.loads(imported["report_json"]) if imported else {}
        decisions = json.loads(imported["decisions_json"]) if imported else {}
        values = metadata.model_dump(mode="json")
        candidates = {}
        for name, candidate in report.get("candidates", {}).items():
            validated = Metadata.model_validate_json(canonical_json(candidate)).model_dump(mode="json")
            differences = [
                field
                for field in get_args(FieldName)
                if (not name.startswith(("eze:", "online:")) or field in validated["field_sources"])
                and not (name.startswith("online:") and field == "source_rating")
                and not _equivalent(values[field], validated[field])
            ]
            candidates[name] = {"metadata": validated, "fields": differences, "decision": decisions.get(name)}
        conflicts = [
            item[0]
            for item in db.execute(
                "SELECT id FROM sidecar_conflict WHERE book_id=? AND state='pending' ORDER BY id", (book_id,)
            )
        ]
        event = db.execute("SELECT max(id) FROM review_event WHERE book_id=?", (book_id,)).fetchone()[0]
        confirmed = db.execute(
            "SELECT action,metadata_revision,details_json FROM review_event "
            "WHERE book_id=? AND action IN ('manual_confirm','auto_confirm') ORDER BY id DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        token = hashlib.sha256(
            canonical_json(
                {
                    "book_id": book_id,
                    "content_sha256": content.sha256,
                    "revision": row["metadata_revision"],
                    "report": report,
                    "decisions": decisions,
                    "event": event,
                    "sidecar_conflicts": conflicts,
                }
            ).encode()
        ).hexdigest()
        issues = []
        if not (metadata.title or "").strip() and not (metadata.original_title or "").strip():
            issues.append("title_missing")
        if not metadata.language or normalized(metadata.language.strip()) in {"unknown", "n/a", ""}:
            issues.append("language_unknown")
        online = None
        resampled = False
        if metadata.source:
            observed = db.execute(
                "SELECT observation_json FROM source_observation WHERE book_id=? AND source_url=?",
                (book_id, metadata.source.key),
            ).fetchone()
            if observed:
                candidate_name = "online:" + metadata.source.site
                candidate = candidates.get(candidate_name)
                observation = json.loads(observed[0])
                # A resample archive re-encodes the images: the gallery's declared size describes the
                # original files and says nothing about these bytes.
                resampled = (
                    observation.get("content_sha256") == content.sha256
                    and observation.get("archive_variant") == "resample"
                )
                if (
                    observation.get("content_sha256") == content.sha256
                    and candidate
                    and (
                        candidate["decision"] != "skipped"
                        or Source.model_validate(candidate["metadata"]["source"]).key != metadata.source.key
                    )
                ):
                    online = observation
        if online is None:
            issues.append("source_unverified" if metadata.source else "source_absent")
        if metadata.declared_page_count is not None and metadata.declared_page_count != content.page_count:
            issues.append("declared_page_count_mismatch")
        compared_byte_size = content.byte_size
        byte_size_basis = "archive"
        if resampled:
            compared_byte_size, byte_size_basis = None, "resample"
        elif (
            metadata.declared_byte_size is not None
            and online is not None
            and metadata.source is not None
            and content.format == "cbz"
        ):
            byte_size_basis = "images"
            compared_byte_size = image_bytes(db, book_id) if available else None
            if compared_byte_size is None:
                issues.append("archive_unavailable")
        if (
            metadata.declared_byte_size is not None
            and compared_byte_size is not None
            and metadata.declared_byte_size != compared_byte_size
        ):
            issues.append("declared_byte_size_mismatch")
        if any(candidate["fields"] and candidate["decision"] is None for candidate in candidates.values()):
            issues.append("candidates_unresolved")
        if conflicts:
            issues.append("sidecar_conflicts")
        evidence = _evidence(content, values)
        proof = db.execute(
            "SELECT id,details_json FROM review_event WHERE book_id=? AND action='manual_confirm' "
            "AND json_extract(details_json,'$.evidence')=json(?) ORDER BY id DESC LIMIT 1",
            (book_id, canonical_json(evidence)),
        ).fetchone()
        reasons = [issue for issue in issues if issue not in {"source_absent", "source_unverified"}]
        if online is None and (
            proof is None
            or not set(issues) <= set(json.loads(proof["details_json"]).get("acknowledged_issues", []))
        ):
            reasons.append("local_confirmation_missing")
        manifest = db.execute(
            "SELECT pages_json FROM archive_manifest WHERE book_id=?", (book_id,)
        ).fetchone()
        if manifest is None or len(json.loads(manifest[0])) != content.page_count:
            reasons.append("page_manifest_missing")
        return {
            "book_id": book_id,
            "revision": row["metadata_revision"],
            "token": token,
            "state": row["review_state"],
            "metadata": values,
            "candidates": candidates,
            "issues": issues,
            "automatic": {
                "rule": "online-source/1" if online else LOCAL_RULE,
                "eligible": not reasons,
                "reasons": reasons,
                "evidence_event_id": proof["id"] if proof else None,
            },
            "confirmation": (
                {**json.loads(confirmed["details_json"]), "action": confirmed["action"]}
                if confirmed
                and confirmed["metadata_revision"] == row["metadata_revision"]
                and row["review_state"] == "confirmed"
                else None
            ),
            "ignored_fields": report.get("ignored_fields", []),
            "warnings": report.get("warnings", []),
            "import_sources": report.get("sources", list(candidates)),
            "actual_page_count": content.page_count,
            "actual_byte_size": content.byte_size,
            "compared_byte_size": compared_byte_size,
            "byte_size_basis": byte_size_basis,
        }

    def purge(self) -> int:
        """Drop source refresh and failed automatic check events older than EVENT_RETENTION.

        Confirmations, edits and candidate decisions stay, as does each book's latest event: the
        review token, the confirmation shown and the local evidence are read from them.
        """
        cutoff = int(time.time()) - EVENT_RETENTION
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM review_event WHERE id IN (SELECT e.id FROM review_event e WHERE "
                "(e.action='source_refresh' AND json_extract(e.details_json,'$.fetched_at')<?) OR "
                "(e.action='auto_check' AND json_extract(e.details_json,'$.checked_at')<?)) "
                "AND id NOT IN (SELECT max(id) FROM review_event GROUP BY book_id)",
                (cutoff, datetime.fromtimestamp(cutoff, UTC).isoformat()),
            ).rowcount

    def preview(self, book_id: str) -> dict:
        available = self.verification_stamp(book_id) is not None
        with self.library.connection() as db:
            db.execute("BEGIN")
            return self._preview(db, book_id, available=available)

    def record_event(self, db: sqlite3.Connection, book_id: str, action: str, details: dict) -> None:
        revision = db.execute("SELECT metadata_revision FROM book WHERE id=?", (book_id,)).fetchone()[0]
        db.execute(
            "INSERT INTO review_event(book_id,metadata_revision,action,details_json) VALUES(?,?,?,?)",
            (book_id, revision, action, canonical_json(details)),
        )

    def edit(self, book_id: str, metadata: Metadata, *, expected_revision: int) -> Book:
        verified_stamp = self.verification_stamp(book_id)
        with self.files.root.mutation(), self.library.transaction() as db:
            previous = self._preview(db, book_id, available=verified_stamp is not None)["metadata"]
            values = metadata.model_dump(mode="json")
            changed = [field for field in get_args(FieldName) if values[field] != previous[field]]
            values["field_sources"] = previous["field_sources"]
            values["protected_fields"] = sorted(set(previous["protected_fields"]) | set(changed))
            for field in changed:
                values["field_sources"][field] = "manual"
            validated = Metadata.model_validate_json(canonical_json(values))
            self.library.write_metadata(db, book_id, validated, expected_revision)
            if changed:
                self.record_event(db, book_id, "manual_edit", {"fields": changed})
                self.auto_check(db, book_id, record_failure=False, verified_stamp=verified_stamp)
        return self.library.get_book(book_id)

    def _checked(self, db: sqlite3.Connection, book_id: str, token: str, *, available: bool) -> dict:
        preview = self._preview(db, book_id, available=available)
        if preview["token"] != token:
            raise RevisionConflict("Review changed; refresh the preview")
        return preview

    def candidate(self, book_id: str, name: str, token: str, choices: dict[str, str] | None) -> Book:
        verified_stamp = self.verification_stamp(book_id)
        with self.files.root.mutation(), self.library.transaction() as db:
            preview = self._checked(db, book_id, token, available=verified_stamp is not None)
            if name not in preview["candidates"]:
                raise InvalidInput("Unknown candidate for this book")
            candidate = preview["candidates"][name]
            if choices is not None:
                if set(choices) != set(candidate["fields"]) or not set(choices.values()) <= {
                    "current",
                    "candidate",
                }:
                    raise InvalidInput("Choose current or candidate for every differing field")
                values = preview["metadata"]
                for field, choice in choices.items():
                    if choice == "candidate":
                        values[field] = candidate["metadata"][field]
                    values["field_sources"][field] = "manual"
                values["protected_fields"] = sorted(set(values["protected_fields"]) | set(choices))
                self.library.write_metadata(
                    db, book_id, Metadata.model_validate_json(canonical_json(values)), preview["revision"]
                )
            row = db.execute(
                "SELECT decisions_json FROM metadata_import WHERE book_id=?", (book_id,)
            ).fetchone()
            decisions = json.loads(row[0])
            decisions[name] = "skipped" if choices is None else "selected"
            db.execute(
                "UPDATE metadata_import SET decisions_json=? WHERE book_id=?",
                (canonical_json(decisions), book_id),
            )
            self.record_event(db, book_id, "candidate", {"name": name, "choices": choices})
            self.auto_check(db, book_id, record_failure=False, verified_stamp=verified_stamp)
        return self.library.get_book(book_id)

    def verification_stamp(self, book_id: str) -> str | None:
        """The archive's file stamp when it is present with the registered content, else None.

        Stamp-based: the archive is only rehashed when its stamp differs from the manifest.
        Call it before opening a write transaction (it may refresh the manifest stamp)."""
        book = self.library.get_book(book_id)
        try:
            state, stamp = self.files.availability(book)
        except OSError:
            return None
        return stamp if state == "available" else None

    def _current_stamp(self, book: Book) -> str | None:
        try:
            return canonical_json(file_stamp(self.files.root.resolve(book.content.relative_path)))
        except OSError:
            return None

    def auto_check(
        self,
        db: sqlite3.Connection,
        book_id: str,
        *,
        record_failure: bool,
        verified_stamp=None,
        recheck_candidates: bool = False,
    ) -> dict:
        preview = self._preview(db, book_id, available=verified_stamp is not None)
        automatic = preview["automatic"]
        if preview["state"] == "confirmed":
            if not recheck_candidates or "candidates_unresolved" not in automatic["reasons"]:
                return {"outcome": "already_confirmed", "reasons": []}
            db.execute("UPDATE book SET review_state='needs_review' WHERE id=?", (book_id,))
        reasons = list(automatic["reasons"])
        if not reasons:
            book = self.library.get_book(book_id, db)
            if verified_stamp is None or self._current_stamp(book) != verified_stamp:
                reasons.append("archive_unavailable")
        details = {
            "rule": automatic["rule"],
            "checked_at": datetime.now(UTC).isoformat(),
            "evidence_event_id": automatic["evidence_event_id"],
            "reasons": reasons,
            "candidates": {name: candidate["decision"] for name, candidate in preview["candidates"].items()},
            "acknowledged_issues": preview["issues"] if not reasons else [],
        }
        if not reasons:
            db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (book_id,))
            settle_source_check(db, book_id)
            self.record_event(db, book_id, "auto_confirm", details)
        elif record_failure:
            self.record_event(db, book_id, "auto_check", details)
        return {"outcome": "needs_review" if reasons else "confirmed", "reasons": reasons}

    def automatic(self, book_id: str, token: str) -> dict:
        verified_stamp = self.verification_stamp(book_id)
        with self.files.root.mutation(), self.library.transaction() as db:
            self._checked(db, book_id, token, available=verified_stamp is not None)
            return self.auto_check(db, book_id, record_failure=True, verified_stamp=verified_stamp)

    def complete(self, book_id: str, token: str, acknowledged_issues: list[str]) -> Book:
        # Verify the archive (perhaps rehashing it) before taking the lock; inside it only the
        # file stamp is compared.
        verified_stamp = self.verification_stamp(book_id)
        if verified_stamp is None:
            raise ContentChanged("Cannot confirm review while the archive is unavailable")
        with self.files.root.mutation(), self.library.transaction() as db:
            book = self.library.get_book(book_id, db)
            if self._current_stamp(book) != verified_stamp:
                raise ContentChanged("Archive changed while the review was confirmed")
            preview = self._checked(db, book_id, token, available=True)
            if {"candidates_unresolved", "sidecar_conflicts"} & set(preview["issues"]):
                raise InvalidInput(
                    "Select or skip pending candidates and resolve sidecar conflicts first",
                    code="review_blocked",
                )
            if set(acknowledged_issues) != set(preview["issues"]):
                raise InvalidInput(
                    "Explicitly acknowledge each missing or unverified item",
                    code="acknowledgement_required",
                )
            db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (book_id,))
            settle_source_check(db, book_id)
            self.record_event(
                db,
                book_id,
                "manual_confirm",
                {
                    "acknowledged_issues": acknowledged_issues,
                    "evidence": _evidence(book.content, preview["metadata"]),
                    "checked_at": datetime.now(UTC).isoformat(),
                },
            )
        return self.library.get_book(book_id)
