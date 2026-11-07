from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Literal, get_args

from .database import Book
from .errors import InvalidInput, NotFound, RevisionConflict
from .files import LibraryRoot, RootUnavailable, file_stamp, sync_directory
from .ingest import FileLibrary
from .models import MAX_METADATA_BYTES, FieldName, Metadata, NativeSidecar, canonical_json, read_native

SYNC_STATE = """CASE
    WHEN last_error='external_conflict' THEN 'conflict'
    WHEN last_error IS NOT NULL THEN 'error'
    WHEN pending_revision>synced_revision THEN 'pending'
    ELSE 'synced' END"""

Choice = Literal["library", "file"]


def sidecar_path(root: LibraryRoot, relative_path: str) -> Path:
    return root.resolve(relative_path + ".mangacouch.json")


def _json(sidecar: NativeSidecar | None) -> dict | None:
    return sidecar.model_dump(mode="json", by_alias=True) if sidecar else None


def _differing(snapshot: dict) -> list[str]:
    library, file = snapshot["library"]["metadata"], snapshot["file"]["metadata"]
    return [field for field in get_args(FieldName) if file[field] != library[field]]


class SidecarSync:
    """Mirror library metadata to `<archive>.mangacouch.json`.

    Under the root mutation lock the file on disk is compared with the baseline
    recorded at the last write. A matching file is replaced atomically (temp,
    fsync, os.replace); a file edited elsewhere becomes a two-way conflict that
    the owner resolves by keeping the library version or taking the file.
    """

    def __init__(self, files: FileLibrary):
        self.files = files
        self.library = files.library
        self.root = files.root

    def path(self, book: Book) -> Path:
        return sidecar_path(self.root, book.content.relative_path)

    def _read(self, path: Path, sha256: str) -> tuple[NativeSidecar | None, str | None]:
        try:
            before = file_stamp(path)
        except FileNotFoundError:
            return None, None
        with path.open("rb") as stream:
            data = stream.read(MAX_METADATA_BYTES + 4097)
        if file_stamp(path) != before:
            raise RevisionConflict("Sidecar changed during reading")
        try:
            native = read_native(data, sha256)
        except ValueError:
            raise ValueError("Invalid sidecar schema, content binding or payload") from None
        return native, hashlib.sha256(data).hexdigest()

    def _state(self, book_id: str) -> sqlite3.Row:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM sidecar_sync WHERE book_id=?", (book_id,)).fetchone()
        if row is None:
            raise NotFound("Sidecar state not found")
        return row

    def _observe(self, book: Book) -> tuple[dict, NativeSidecar | None, str | None]:
        current, raw_hash = self._read(self.path(book), book.content.sha256)
        local = self.library.native_sidecar(book.id)
        token = hashlib.sha256(
            canonical_json(
                {
                    "revision": local.revision,
                    "path": book.content.relative_path,
                    "file": current.semantic_hash() if current else None,
                }
            ).encode()
        ).hexdigest()
        return {"library": _json(local), "file": _json(current), "token": token}, current, raw_hash

    def _conflict(self, book: Book) -> dict:
        snapshot, _, _ = self._observe(book)
        with self.library.transaction() as db:
            old = db.execute(
                "SELECT id,snapshot_json FROM sidecar_conflict WHERE book_id=? AND state='pending'",
                (book.id,),
            ).fetchone()
            if old and json.loads(old["snapshot_json"]) == snapshot:
                conflict_id = old["id"]
            else:
                db.execute(
                    "UPDATE sidecar_conflict SET state='superseded' WHERE book_id=? AND state='pending'",
                    (book.id,),
                )
                conflict_id = str(uuid.uuid4())
                db.execute(
                    "INSERT INTO sidecar_conflict(id,book_id,token,snapshot_json,state) "
                    "VALUES(?,?,?,?,'pending')",
                    (conflict_id, book.id, snapshot["token"], canonical_json(snapshot)),
                )
            db.execute("UPDATE sidecar_sync SET last_error='external_conflict' WHERE book_id=?", (book.id,))
        return {"state": "conflict", "conflict_id": conflict_id}

    def conflicts(self, *, limit: int = 100, book_id: str | None = None) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise InvalidInput("Invalid conflict page size")
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT * FROM sidecar_conflict WHERE state='pending' AND (? IS NULL OR book_id=?) "
                "ORDER BY rowid LIMIT ?",
                (book_id, book_id, limit),
            ).fetchall()
        results = []
        for row in rows:
            snapshot = json.loads(row["snapshot_json"])
            results.append(
                {"id": row["id"], "book_id": row["book_id"], "fields": _differing(snapshot), **snapshot}
            )
        return results

    def _publish(self, target: Path, data: bytes) -> None:
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".mangacouch-sidecar-")
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        sync_directory(target.parent)

    def _record(self, book_id: str, revision: int, written: NativeSidecar, raw_hash: str | None) -> None:
        with self.library.transaction() as db:
            db.execute(
                "UPDATE sidecar_sync SET synced_revision=?,baseline_hash=?,observed_hash=?,last_error=NULL "
                "WHERE book_id=?",
                (revision, written.semantic_hash(), raw_hash, book_id),
            )
            db.execute(
                "UPDATE sidecar_conflict SET state='superseded' WHERE book_id=? AND state='pending'",
                (book_id,),
            )

    def sync_one(self, book_id: str) -> dict:
        result = self._sync_one(book_id)
        if result["state"] in {"synced", "unchanged"}:
            with self.library.transaction() as db:
                db.execute(
                    "INSERT INTO maintenance_stamp VALUES('sidecar_sync',?) ON CONFLICT(name) DO "
                    "UPDATE SET updated_at=excluded.updated_at",
                    (int(time.time()),),
                )
        return result

    def _sync_one(self, book_id: str) -> dict:
        with self.root.mutation():
            book = self.library.get_book(book_id)
            try:
                state, _ = self.files.availability(book)
                if state != "available":
                    raise OSError(f"archive_{state}")
                target = self.path(book)
                current, raw_hash = self._read(target, book.content.sha256)
                desired = self.library.native_sidecar(book_id)
                if current is not None and current.metadata == desired.metadata:
                    self._record(book_id, desired.revision, current, raw_hash)
                    return {"state": "unchanged", "revision": desired.revision}
                if current is not None and current.semantic_hash() != self._state(book_id)["baseline_hash"]:
                    return self._conflict(book)
                self.root.check(writable=True)
                data = desired.canonical_bytes()
                self._publish(target, data)
                self._record(book_id, desired.revision, desired, hashlib.sha256(data).hexdigest())
            except RootUnavailable:
                raise
            except (ValueError, OSError, sqlite3.Error) as error:
                with self.library.transaction() as db:
                    db.execute(
                        "UPDATE sidecar_sync SET last_error=? WHERE book_id=?",
                        (type(error).__name__, book_id),
                    )
                return {"state": "error", "error": type(error).__name__}
        state = self._state(book_id)
        return {
            "state": "pending" if state["pending_revision"] > state["synced_revision"] else "synced",
            "revision": state["synced_revision"],
        }

    def status(self, *, limit: int = 50, after_id: str = "") -> dict:
        if not 1 <= limit <= 100 or len(after_id) > 64:
            raise InvalidInput("Invalid status page")
        with self.library.connection() as db:
            db.execute("BEGIN")
            stamp = db.execute(
                "SELECT updated_at FROM maintenance_stamp WHERE name='sidecar_sync'"
            ).fetchone()
            counts = dict.fromkeys(("pending", "error", "conflict", "synced"), 0)
            for row in db.execute(
                f"SELECT {SYNC_STATE} AS state,COUNT(*) AS count FROM sidecar_sync GROUP BY state"
            ):
                counts[row["state"]] = row["count"]
            rows = db.execute(
                f"SELECT b.id,b.content_json,b.metadata_json,s.pending_revision,s.synced_revision,"
                f"s.last_error,{SYNC_STATE} AS state FROM book b JOIN sidecar_sync s ON s.book_id=b.id "
                "WHERE b.id>? AND (s.pending_revision>s.synced_revision OR s.last_error IS NOT NULL) "
                "ORDER BY b.id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
        items = []
        for row in rows[:limit]:
            metadata, content = json.loads(row["metadata_json"]), json.loads(row["content_json"])
            items.append(
                {
                    "id": row["id"],
                    "metadata": {field: metadata[field] for field in ("title", "original_title")},
                    "content": {"original_filename": content["original_filename"]},
                    "state": row["state"],
                    "pending_revision": row["pending_revision"],
                    "synced_revision": row["synced_revision"],
                    "error": row["last_error"],
                }
            )
        return {
            "counts": counts,
            "last_synced_at": stamp[0] if stamp else None,
            "items": items,
            "after_id": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def batch(
        self,
        *,
        limit: int = 100,
        after_id: str = "",
        through_id: str | None = None,
        mode: Literal["all", "pending"] = "all",
    ) -> dict:
        if not 1 <= limit <= 1000 or mode not in {"all", "pending"} or len(after_id) > 64:
            raise InvalidInput("Invalid batch size")
        if through_id is not None and len(through_id) > 64:
            raise InvalidInput("Invalid batch boundary")
        self.root.check(writable=True)
        with self.library.connection() as db:
            if through_id is None:
                through_id = db.execute("SELECT COALESCE(MAX(id),'') FROM book").fetchone()[0]
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT book_id FROM sidecar_sync WHERE book_id>? AND book_id<=? AND "
                    "(?='all' OR pending_revision>synced_revision OR last_error IS NOT NULL) "
                    "ORDER BY book_id LIMIT ?",
                    (after_id, through_id, mode, limit + 1),
                )
            ]
        results = {}
        for book_id in ids[:limit]:
            try:
                results[book_id] = self.sync_one(book_id)
            except KeyError:
                results[book_id] = {"state": "removed"}
        return {
            "books": results,
            "after_id": ids[limit - 1] if len(ids) > limit else None,
            "through_id": through_id,
        }

    def resolve(
        self,
        conflict_id: str,
        *,
        choice: Choice = "library",
        fields: dict[str, Choice] | None = None,
    ) -> Book:
        """Keep the library version or take the file, for the whole record or per field."""
        with self.root.mutation():
            with self.library.connection() as db:
                conflict = db.execute(
                    "SELECT * FROM sidecar_conflict WHERE id=? AND state='pending'", (conflict_id,)
                ).fetchone()
            if conflict is None:
                raise RevisionConflict("Conflict is no longer pending")
            book = self.library.get_book(conflict["book_id"])
            snapshot = json.loads(conflict["snapshot_json"])
            observed, current, raw_hash = self._observe(book)
            if observed["token"] != conflict["token"] or current is None:
                raise RevisionConflict("Metadata or sidecar changed; refresh the conflict preview")
            if fields is not None:
                if set(fields) != set(_differing(snapshot)):
                    raise InvalidInput("Choose a version for every differing metadata field")
                values = json.loads(canonical_json(snapshot["library"]["metadata"]))
                for field, source in fields.items():
                    if source not in get_args(Choice):
                        raise InvalidInput("Invalid field choice")
                    values[field] = snapshot[source]["metadata"][field]
                    values["field_sources"][field] = "manual"
                values["protected_fields"] = sorted(set(values["protected_fields"]) | set(fields))
                metadata = Metadata.model_validate_json(canonical_json(values))
            elif choice == "file":
                metadata = current.metadata
            elif choice == "library":
                metadata = book.metadata
            else:
                raise InvalidInput("Invalid conflict choice")
            with self.library.transaction() as db:
                self.library.write_metadata(db, book.id, metadata, book.metadata_revision)
                # The file is now the agreed baseline; the next sync rewrites it from the library.
                db.execute(
                    "UPDATE sidecar_sync SET baseline_hash=?,observed_hash=?,synced_revision=0,"
                    "last_error=NULL WHERE book_id=?",
                    (current.semantic_hash(), raw_hash, book.id),
                )
                db.execute(
                    "UPDATE sidecar_conflict SET state='resolved',resolution_json=? WHERE id=?",
                    (
                        canonical_json(
                            {"choice": choice, "fields": fields, "metadata": metadata.model_dump(mode="json")}
                        ),
                        conflict_id,
                    ),
                )
            return self.library.get_book(book.id)
