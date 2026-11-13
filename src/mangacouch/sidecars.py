from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import stat
import time
import uuid
from pathlib import Path
from typing import Literal, get_args

from .cursors import decode_cursor, encode_cursor
from .database import Book, busy
from .errors import InvalidInput, NotFound, RevisionConflict
from .eze_cache import EzeCache
from .files import LibraryRoot, RootUnavailable, atomic_write, file_stamp
from .ingest import FileLibrary
from .limits import ImportCancelled
from .logs import short_id
from .maintenance import Retry
from .models import MAX_METADATA_BYTES, FieldName, Metadata, NativeSidecar, canonical_json, read_native

LOGGER = logging.getLogger(__name__)
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
    if snapshot["file"] is None:
        return []
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
        self.retry = Retry()
        self.conflict_after = ""
        self.conflict_next_at = 0.0

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
        snapshot, current, raw_hash = self._observe(book)
        if current is None or current.metadata == book.metadata:
            if current is not None:
                self._record(book.id, self.library.native_sidecar(book.id).revision, current, raw_hash)
                return {"state": "unchanged"}
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE sidecar_sync SET synced_revision=0,last_error=NULL WHERE book_id=?", (book.id,)
                )
                db.execute(
                    "UPDATE sidecar_conflict SET state='superseded' WHERE book_id=? AND state='pending'",
                    (book.id,),
                )
            return {"state": "pending"}
        with self.library.connection() as db:
            old = db.execute(
                "SELECT id,snapshot_json FROM sidecar_conflict WHERE book_id=? AND state='pending'",
                (book.id,),
            ).fetchone()
        if old and json.loads(old["snapshot_json"]) == snapshot:
            self._error(book.id, "external_conflict")
            return {"state": "conflict", "conflict_id": old["id"]}
        with self.library.transaction() as db:
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
        return self.conflict_page(limit=limit, book_id=book_id)["items"]

    def conflict_page(self, *, limit: int = 100, after: str = "", book_id: str | None = None) -> dict:
        """Pending conflicts, oldest first, with an opaque keyset cursor."""
        if not 1 <= limit <= 1000:
            raise InvalidInput("Invalid conflict page size")
        (after_row,) = decode_cursor(after, int) if after else (0,)
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT rowid AS row,* FROM sidecar_conflict WHERE state='pending' "
                "AND (? IS NULL OR book_id=?) AND rowid>? ORDER BY rowid LIMIT ?",
                (book_id, book_id, after_row, limit + 1),
            ).fetchall()
        results = []
        for row in rows[:limit]:
            snapshot = json.loads(row["snapshot_json"])
            results.append(
                {"id": row["id"], "book_id": row["book_id"], "fields": _differing(snapshot), **snapshot}
            )
        return {
            "items": results,
            "after": encode_cursor(rows[limit - 1]["row"]) if len(rows) > limit else None,
        }

    @staticmethod
    def _publish(target: Path, data: bytes) -> None:
        """Replace the sidecar atomically. Like the archive beside it, a new sidecar follows the
        umask; a rewrite keeps the permissions the existing file was given."""
        try:
            existing = target.lstat()
        except FileNotFoundError:
            existing = None
        keep = existing is not None and stat.S_ISREG(existing.st_mode)
        atomic_write(target, data, mode=stat.S_IMODE(existing.st_mode) if keep and existing else None)

    def purge(self) -> int:
        """Drop superseded conflicts: a newer conflict or a successful write replaced them, and only
        pending conflicts are shown; resolved ones stay as the record of a choice."""
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM sidecar_conflict WHERE state='superseded' OR "
                "(state='resolved' AND coalesce(json_extract(resolution_json,'$.resolved_at'),0)"
                "<unixepoch()-2592000)"
            ).rowcount

    def _record(self, book_id: str, revision: int, written: NativeSidecar, raw_hash: str | None) -> None:
        previous = self._state(book_id)
        if (
            previous["synced_revision"] == revision
            and previous["baseline_hash"] == written.semantic_hash()
            and previous["observed_hash"] == raw_hash
            and previous["last_error"] is None
        ):
            return
        with self.library.transaction() as db:
            db.execute(
                "INSERT INTO maintenance_stamp VALUES('sidecar_sync',?) ON CONFLICT(name) DO "
                "UPDATE SET updated_at=excluded.updated_at",
                (int(time.time()),),
            )
            db.execute(
                "UPDATE sidecar_sync SET synced_revision=?,baseline_hash=?,observed_hash=?,last_error=NULL "
                "WHERE book_id=?",
                (revision, written.semantic_hash(), raw_hash, book_id),
            )
            db.execute(
                "UPDATE sidecar_conflict SET state='superseded' WHERE book_id=? AND state='pending'",
                (book_id,),
            )

    def _error(self, book_id: str, code: str) -> dict:
        with self.library.transaction() as db:
            db.execute("UPDATE sidecar_sync SET last_error=? WHERE book_id=?", (code, book_id))
        return {"state": "error", "error": code}

    def sync_one(self, book_id: str, *, replace_invalid: bool = False, blocking: bool = True) -> dict:
        """Write one book's sidecar. `replace_invalid` overwrites a damaged or foreign file that
        would otherwise fail every sync; a readable file edited elsewhere stays a conflict."""
        result = self._sync_one(book_id, replace_invalid=replace_invalid, blocking=blocking)
        return result

    def _sync_one(
        self,
        book_id: str,
        *,
        replace_invalid: bool = False,
        blocking: bool = True,
        writable_checked: bool = False,
    ) -> dict:
        book = self.library.get_book(book_id)
        state, stamp = self.files.availability(book)
        with self.root.mutation(blocking=blocking):
            book = self.library.get_book(book_id)
            try:
                if (
                    state == "available"
                    and canonical_json(file_stamp(self.root.resolve(book.content.relative_path))) != stamp
                ):
                    raise RevisionConflict("Archive changed during sidecar sync")
                if state != "available":
                    return self._error(book_id, f"archive_{state}")
                target = self.path(book)
                try:
                    current, raw_hash = self._read(target, book.content.sha256)
                except RevisionConflict:
                    raise
                except ValueError:
                    if not replace_invalid:
                        raise
                    current, raw_hash = None, None
                desired = self.library.native_sidecar(book_id)
                if current is not None and current.metadata == desired.metadata:
                    self._record(book_id, desired.revision, current, raw_hash)
                    return {"state": "unchanged", "revision": desired.revision}
                if current is not None:
                    baseline = self._state(book_id)
                    if (
                        current.semantic_hash() != baseline["baseline_hash"]
                        and raw_hash != baseline["observed_hash"]
                        and raw_hash != baseline["intent_hash"]
                    ):
                        return self._conflict(book)
                if not writable_checked:
                    self.root.check(writable=True)
                data = desired.canonical_bytes()
                with self.library.transaction() as db:
                    db.execute(
                        "UPDATE sidecar_sync SET intent_hash=? WHERE book_id=?",
                        (hashlib.sha256(data).hexdigest(), book_id),
                    )
                self._publish(target, data)
                self._record(book_id, desired.revision, desired, hashlib.sha256(data).hexdigest())
            except (RootUnavailable, ImportCancelled):
                raise
            except sqlite3.Error as error:
                if busy(error):
                    raise
                return self._error(book_id, "database_error")
            except OSError:
                return self._error(book_id, "storage_error")
            except RevisionConflict:
                raise
            except ValueError:
                return self._error(book_id, "sidecar_invalid")
        state = self._state(book_id)
        return {
            "state": "pending" if state["pending_revision"] > state["synced_revision"] else "synced",
            "revision": state["synced_revision"],
        }

    def publish(self, book_id: str) -> dict:
        """The one step after any committed metadata change: write the book's sidecars now.

        It never raises for file or storage problems, because the change itself is saved; a write
        that fails stays pending and the maintenance worker retries it (`sync_pending`). `state`
        is synced, unchanged, conflict (edited elsewhere; the owner chooses), pending (saved, the
        file is still to be written; `error` says why) or removed.
        """
        try:
            with self.library.connection() as db:
                eze = db.execute(
                    "SELECT 1 FROM eze_cache WHERE book_id=? AND synced_hash IS NULL", (book_id,)
                ).fetchone()
            if eze:
                EzeCache(self.files).sync_one(book_id)
        except (OSError, ValueError, KeyError, sqlite3.Error):
            pass  # left pending for the maintenance step
        try:
            result = self.sync_one(book_id)
        except KeyError:
            return {"state": "removed"}
        except RootUnavailable:
            result = {"state": "error", "error": "root_unavailable"}
        except RevisionConflict:
            result = {"state": "error", "error": "sidecar_changed"}
        except sqlite3.Error:
            result = {"state": "error", "error": "database_error"}
        except (OSError, ValueError):
            result = {"state": "error", "error": "storage_error"}
        if result["state"] == "error":
            LOGGER.warning("Metadata of %s saved; sidecar pending: %s", short_id(book_id), result["error"])
            return {"state": "pending", "error": result["error"]}
        return result

    def sync_pending(self, limit: int = 10) -> int:
        """Maintenance step: write native sidecars left pending or failed, a few at a time.

        Conflicts wait for the owner. A book whose write keeps failing backs off (see Retry), so
        it neither blocks the others nor keeps the worker busy. Returns the books settled.
        """

        def select(after: str, count: int) -> list[str]:
            with self.library.connection() as db:
                return [
                    row[0]
                    for row in db.execute(
                        "SELECT s.book_id FROM sidecar_sync s JOIN book b ON b.id=s.book_id "
                        "WHERE (s.pending_revision>s.synced_revision OR s.last_error IS NOT NULL) "
                        "AND s.last_error IS NOT 'external_conflict' AND b.availability='available' "
                        "AND s.book_id>? ORDER BY s.book_id LIMIT ?",
                        (after, count),
                    )
                ]

        conflicts = []
        if time.monotonic() >= self.conflict_next_at:
            with self.library.connection() as db:
                conflicts = db.execute(
                    "SELECT book_id FROM sidecar_sync WHERE last_error='external_conflict' AND book_id>? "
                    "ORDER BY book_id LIMIT ?",
                    (self.conflict_after, limit),
                ).fetchall()
            self.conflict_next_at = time.monotonic() + 30
            if not conflicts:
                self.conflict_after = ""
        for row in conflicts:
            self.conflict_after = row[0]
            try:
                self._sync_one(row[0])
            except (RootUnavailable, ImportCancelled):
                raise
            except RevisionConflict:
                continue
            except (ValueError, OSError):
                self._error(row[0], "sidecar_invalid")
        settled = 0
        selected = self.retry.take(select, limit)
        if selected:
            self.root.check(writable=True)
        for book_id in selected:
            try:
                result = self._sync_one(book_id, writable_checked=True)
            except (RootUnavailable, ImportCancelled):
                raise
            except KeyError:
                continue
            except sqlite3.Error as error:
                if busy(error):
                    break
                result = {"state": "error"}
            except (OSError, ValueError):
                result = {"state": "error"}
            if result["state"] == "error":
                self.retry.failed(book_id)
            else:
                # "pending": edited again while writing; a later pass takes the newer revision.
                self.retry.succeeded(book_id)
                settled += result["state"] not in {"pending", "conflict"}
        return settled

    def status(self, *, limit: int = 50, after: str = "") -> dict:
        if not 1 <= limit <= 100:
            raise InvalidInput("Invalid status page")
        (after_id,) = decode_cursor(after, str) if after else ("",)
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
            "after": encode_cursor(rows[limit - 1]["id"]) if len(rows) > limit else None,
        }

    def batch(
        self,
        *,
        limit: int = 100,
        after: str = "",
        through_id: str | None = None,
        mode: Literal["all", "pending"] = "all",
    ) -> dict:
        if not 1 <= limit <= 1000 or mode not in {"all", "pending"}:
            raise InvalidInput("Invalid batch size")
        (after_id,) = decode_cursor(after, str) if after else ("",)
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
        results = []
        for book_id in ids[:limit]:
            try:
                results.append({"book_id": book_id, **self._sync_one(book_id, writable_checked=True)})
            except KeyError:
                results.append({"book_id": book_id, "state": "removed"})
        return {
            "items": results,
            "after": encode_cursor(ids[limit - 1]) if len(ids) > limit else None,
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
                self._conflict(book)
                error = RevisionConflict("Metadata or sidecar changed; refresh the conflict preview")
                error.conflict = next(iter(self.conflicts(book_id=book.id)), None)
                raise error
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
                        canonical_json({"resolved_at": int(time.time())}),
                        conflict_id,
                    ),
                )
            return self.library.get_book(book.id)
