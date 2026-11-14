"""Record removal with a seven-day recycle bin.

Removing records stores every database row that references the removed books
in the receipt (tables found through PRAGMA foreign_key_list, so new tables are
covered automatically) and deletes the books in one transaction. Restore puts
those rows back. Explicit full backups remain in snapshots.py.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from typing import Literal

from .archives import close_pdf
from .cursors import decode_cursor, encode_cursor
from .database import Book, write_source_links, write_tags
from .errors import InvalidInput, NotFound, RevisionConflict
from .eze import eze_path
from .files import file_stamp, hash_file, sync_directory, thread_cancelled
from .ingest import FileLibrary, resync_sidecars
from .limits import ImportCancelled, check_cancel
from .maintenance import Retry
from .models import Content, Metadata, PersonalState, canonical_json
from .page_features import bands
from .preferences import display_title, preferred_title
from .sidecars import sidecar_path

RETENTION_SECONDS = 7 * 86400
# Rebuilt from the restored metadata by write_tags, so restoring them explicitly would duplicate rows.
DERIVED_TABLES = frozenset({"book_tag", "book_newer", "page_band", "duplicate_progress"})
# Restored feature ids must not collide with later imports; child rows follow their new ids.
REINDEXED_TABLES = frozenset({"page_feature"})
# Collection neighbours recorded on each side of a removed book, nearest first.
PLACEMENT_ANCHORS = 32


def _encode(row: dict) -> dict:
    """JSON-safe copy of a row; BLOB values (page features) are stored as base64."""
    return {
        key: {"base64": base64.b64encode(value).decode()} if isinstance(value, bytes) else value
        for key, value in row.items()
    }


def _decode(row: dict) -> dict:
    return {
        key: base64.b64decode(value["base64"]) if isinstance(value, dict) else value
        for key, value in row.items()
    }


def _foreign_keys(db: sqlite3.Connection) -> dict[str, list[dict]]:
    """Map every table to its single-column foreign keys."""
    tables = [
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    references: dict[str, list[dict]] = {}
    for table in tables:
        keys: dict[int, dict] = {}
        for row in db.execute(f"PRAGMA foreign_key_list({table})"):
            if row["id"] in keys:
                raise RuntimeError(f"Composite foreign key on {table} is not supported by removals")
            keys[row["id"]] = {
                "parent": row["table"],
                "column": row["from"],
                "target": row["to"],
                "on_delete": row["on_delete"],
            }
        references[table] = list(keys.values())
    return references


def _primary_key(db: sqlite3.Connection, table: str) -> tuple[list[str], bool]:
    """Return the primary key columns and whether the key is an INTEGER rowid alias."""
    columns = sorted(
        (row["pk"], row["name"], row["type"]) for row in db.execute(f"PRAGMA table_info({table})")
    )
    key = [(name, kind) for pk, name, kind in columns if pk]
    if not key:
        return ["rowid"], False
    return [name for name, _ in key], len(key) == 1 and key[0][1].upper() == "INTEGER"


def _capture(db: sqlite3.Connection, book_id: str) -> dict:
    """Collect the book row and every row that references it, parents before children."""
    references = _foreign_keys(db)
    captured: dict[str, list[dict]] = {
        "book": [dict(db.execute("SELECT * FROM book WHERE id=?", (book_id,)).fetchone())]
    }
    order = ["book"]
    links: list[dict] = []
    frontier = ["book"]
    while frontier:
        parent = frontier.pop(0)
        for child, keys in references.items():
            if child in DERIVED_TABLES:
                continue
            for key in keys:
                if key["parent"] != parent:
                    continue
                values = canonical_json([row[key["target"]] for row in captured[parent]])
                if key["on_delete"] == "SET NULL":
                    primary, _ = _primary_key(db, child)
                    rows = db.execute(
                        f"SELECT {','.join(primary)},{key['column']} AS value FROM {child} "
                        f"WHERE {key['column']} IN (SELECT value FROM json_each(?))",
                        (values,),
                    ).fetchall()
                    if rows:
                        links.append(
                            {
                                "table": child,
                                "column": key["column"],
                                "key": primary,
                                "rows": [[row[name] for name in primary] + [row["value"]] for row in rows],
                            }
                        )
                    continue
                if key["on_delete"] != "CASCADE":
                    raise RuntimeError(f"{child}.{key['column']} would block book removal")
                rows = db.execute(
                    f"SELECT * FROM {child} WHERE {key['column']} IN (SELECT value FROM json_each(?))",
                    (values,),
                ).fetchall()
                if not rows:
                    continue
                existing = captured.setdefault(child, [])
                seen = {canonical_json(_encode(row)) for row in existing}
                existing.extend(dict(row) for row in rows if canonical_json(_encode(dict(row))) not in seen)
                if child in order:
                    order.remove(child)
                order.append(child)
                frontier.append(child)
    return {
        "sha256": captured["book"][0]["sha256"],
        "tables": [[table, [_encode(row) for row in captured[table]]] for table in order],
        "links": links,
    }


class Removals:
    def __init__(self, files: FileLibrary):
        self.files = files
        self.library = files.library
        self.deletion_retry = Retry()

    @staticmethod
    def _identifier(identifier: str) -> str:
        try:
            if str(uuid.UUID(identifier)) == identifier:
                return identifier
        except ValueError:
            pass
        raise InvalidInput("Invalid removal identifier")

    # Preview ---------------------------------------------------------------

    def _preview(
        self, db: sqlite3.Connection, book_id: str, identifier: str, observations: dict | None = None
    ) -> dict:
        book = db.execute("SELECT * FROM book WHERE id=?", (book_id,)).fetchone()
        if book is None:
            raise NotFound("Book not found")
        content = Content.model_validate_json(book["content_json"])
        metadata = Metadata.model_validate_json(book["metadata_json"])
        related = {}
        for table in ("personal", "metadata_import", "sidecar_sync", "sidecar_conflict", "review_event"):
            related[table] = [
                dict(row)
                for row in db.execute(f"SELECT * FROM {table} WHERE book_id=? ORDER BY rowid", (book_id,))
            ]
        collections = [
            dict(row)
            for row in db.execute(
                "SELECT c.id,c.name,c.revision,i.position FROM collection c "
                "JOIN collection_item i ON i.collection_id=c.id WHERE i.book_id=? ORDER BY c.id",
                (book_id,),
            )
        ]
        blocked = []
        if db.execute("SELECT 1 FROM scan_run WHERE state='active' LIMIT 1").fetchone():
            blocked.append("scan_active")
        if db.execute(
            "SELECT 1 FROM import_job WHERE state='ready' "
            "AND json_extract(content_json,'$.sha256')=? LIMIT 1",
            (content.sha256,),
        ).fetchone():
            blocked.append("import_recovery_pending")
        presence, stamp = (observations or {}).get(book_id, (None, None))
        if presence is None:
            presence, stamp = self._observe(content.relative_path)
        manifest = db.execute(
            "SELECT file_stamp FROM archive_manifest WHERE book_id=?", (book_id,)
        ).fetchone()
        verified_stamp = json.loads(manifest[0]) if manifest and manifest[0] else None
        delete_blocked = []
        if presence == "unreadable":
            delete_blocked.append("archive_unreadable")
        elif presence == "present" and list(stamp or ()) != verified_stamp:
            delete_blocked.append("archive_changed")
        state = {
            "id": identifier,
            "root_id": self.files.root.root_id,
            "book": {key: value for key, value in dict(book).items() if key != "indexed_pages"},
            # Background sidecar writes change only the sync bookkeeping (synced revision, hashes,
            # last error); they must not void a preview the user is confirming. An edit still
            # does, through the book row and the pending revision.
            "related": {
                **related,
                "sidecar_sync": [
                    {"pending_revision": row["pending_revision"]} for row in related["sidecar_sync"]
                ],
            },
            "collections": [item["id"] for item in collections],
            "presence": presence,
            "stamp": stamp,
            "blocked": blocked,
            "delete_blocked": delete_blocked,
        }
        sync = related["sidecar_sync"][0]
        personal = PersonalState.model_validate_json(related["personal"][0]["state_json"])
        return {
            "id": identifier,
            "book_id": book_id,
            "token": hashlib.sha256(canonical_json(state).encode()).hexdigest(),
            "title": display_title(metadata, content.original_filename),
            "filename": content.original_filename,
            "relative_path": content.relative_path,
            "presence": presence,
            "file_stamp": stamp,
            "verified_stamp": verified_stamp,
            "personal": personal.model_dump(mode="json"),
            "collections": collections,
            "metadata_revision": book["metadata_revision"],
            "personal_revision": related["personal"][0]["revision"],
            "unsynced_metadata": sync["pending_revision"] > sync["synced_revision"],
            "pending_conflicts": sum(row["state"] == "pending" for row in related["sidecar_conflict"]),
            "blocked": blocked,
            "delete_blocked": delete_blocked,
        }

    def _observe(self, relative_path: str) -> tuple[str, tuple[int, int, int, int, int] | None]:
        try:
            return "present", file_stamp(self.files.root.resolve(relative_path))
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unreadable", None

    def _verified(self, item: dict) -> tuple[bool, tuple[int, int, int, int, int] | None]:
        path = self.files.root.resolve(item["relative_path"])
        try:
            stamp = file_stamp(path)
            valid = list(stamp) == item.get("file_stamp") or hash_file(path) == item["sha256"]
            return valid and file_stamp(path) == stamp, stamp
        except FileNotFoundError:
            return False, None

    def _reverify(self, book_ids: list[str]) -> None:
        """Hash archives whose stamp moved since their content was last verified.

        A copy that did not keep modification times, or a touched file, has the same content
        under a new stamp: recording that stamp keeps the preview from refusing to delete it.
        Different content keeps its old stamp and stays blocked. Hashing runs before the
        preview's read transaction and outside any write, cancelled by the request deadline."""
        with self.library.connection() as db:
            verified = dict(
                db.execute(
                    "SELECT book_id,file_stamp FROM archive_manifest "
                    "WHERE book_id IN (SELECT value FROM json_each(?))",
                    (json.dumps(book_ids),),
                ).fetchall()
            )
        for book in self.library.get_books([book_id for book_id in book_ids if verified.get(book_id)]):
            presence, stamp = self._observe(book.content.relative_path)
            if presence == "present" and canonical_json(stamp) != verified[book.id]:
                self.files.availability(book, manifest_stamp=verified[book.id])

    def preview(self, book_id: str) -> dict:
        self.files.root.check()
        self._reverify([book_id])
        with self.library.connection() as db:
            db.execute("BEGIN")
            return self._preview(db, book_id, str(uuid.uuid4()))

    @staticmethod
    def _ids(book_ids: list[str]) -> list[str]:
        if (
            not 1 <= len(book_ids) <= 500
            or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in book_ids)
            or len(set(book_ids)) != len(book_ids)
        ):
            raise InvalidInput("Select 1 to 500 distinct records")
        return sorted(book_ids)

    def _batch_preview(
        self, db: sqlite3.Connection, book_ids: list[str], identifier: str, observations: dict | None = None
    ) -> dict:
        items = [self._preview(db, book_id, identifier, observations) for book_id in book_ids]
        return {
            "id": identifier,
            "kind": "selected",
            "book_id": book_ids[0],
            "book_ids": book_ids,
            "token": hashlib.sha256(
                canonical_json(
                    {"kind": "selected", "items": [(item["book_id"], item["token"]) for item in items]}
                ).encode()
            ).hexdigest(),
            "title": "",
            "filename": "",
            "items": items,
            "blocked": sorted({code for item in items for code in item["blocked"]}),
            "delete_blocked": sorted({code for item in items for code in item["delete_blocked"]}),
        }

    def preview_selected(self, book_ids: list[str]) -> dict:
        book_ids = self._ids(book_ids)
        self.files.root.check()
        self._reverify(book_ids)
        with self.library.connection() as db:
            db.execute("BEGIN")
            return self._batch_preview(db, book_ids, str(uuid.uuid4()))

    # Missing-file maintenance ----------------------------------------------

    def check_page(self, *, after: str = "", through_id: str | None = None, limit: int = 100) -> dict:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or (through_id is not None and len(through_id) > 64)
        ):
            raise InvalidInput("Invalid maintenance page")
        (after_id,) = decode_cursor(after, str) if after else ("",)
        with self.library.connection() as db:
            through_id = (
                through_id
                if through_id is not None
                else db.execute("SELECT coalesce(max(id),'') FROM book").fetchone()[0]
            )
            rows = db.execute(
                "SELECT id FROM book WHERE id>? AND id<=? ORDER BY id LIMIT ?",
                (after_id, through_id, limit + 1),
            ).fetchall()
        # Probe every archive (an available one may be rehashed) and the root before taking the
        # operation lock; under it only the cheap stamps are compared again and the results written.
        observed = []
        started = time.monotonic()
        for book in self.library.get_books([row[0] for row in rows[:limit]]):
            try:
                path = self.files.root.resolve(book.content.relative_path)
                file_stamp(path)
                with path.open("rb") as stream:
                    stream.read(1)
                state, stamp = self.files.availability(book)
            except FileNotFoundError:
                state, stamp = "missing", None
            except OSError:
                state, stamp = "unreadable", None
            observed.append((book, state, stamp))
            if time.monotonic() - started >= 10:
                break
        self.files.root.check(writable=True)
        with self.files.root.mutation(), self.library.transaction() as db:
            for book, state, stamp in observed:
                if not self._still(book, state, stamp):
                    continue  # it changed meanwhile; the next check observes it again
                db.execute("UPDATE book SET availability=? WHERE id=?", (state, book.id))
                if state == "available" and book.availability != "available":
                    resync_sidecars(db, book.id)
                if stamp is not None:
                    db.execute("UPDATE archive_manifest SET file_stamp=? WHERE book_id=?", (stamp, book.id))
        return {
            "checked": len(observed),
            "through_id": through_id,
            "after": encode_cursor(observed[-1][0].id) if observed and len(rows) > len(observed) else None,
            "items": [
                {
                    "book_id": book.id,
                    "title": preferred_title(book),
                    "relative_path": book.content.relative_path,
                    "state": state,
                }
                for book, state, _ in observed
                if state != "available"
            ],
        }

    def _still(self, book: Book, state: str, stamp: str | None) -> bool:
        """Whether an archive is still as `check_page` observed it, judged by its stamp alone."""
        try:
            current = canonical_json(file_stamp(self.files.root.resolve(book.content.relative_path)))
        except FileNotFoundError:
            return state == "missing"
        except OSError:
            return state == "unreadable"
        return current == stamp if state == "available" else state != "missing"

    # Receipts --------------------------------------------------------------

    @staticmethod
    def _load(db: sqlite3.Connection, identifier: str) -> dict | None:
        row = db.execute(
            "SELECT id,token,receipt_json,created_at FROM removal WHERE id=?", (identifier,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row["id"], "token": row["token"], "created_at": row["created_at"]} | json.loads(
            row["receipt_json"]
        )

    @staticmethod
    def _save(db: sqlite3.Connection, receipt: dict) -> None:
        stored = {key: value for key, value in receipt.items() if key not in {"id", "token", "created_at"}}
        db.execute("UPDATE removal SET receipt_json=? WHERE id=?", (canonical_json(stored), receipt["id"]))

    @staticmethod
    def _output(receipt: dict) -> dict:
        items = receipt["items"]
        return {
            "id": receipt["id"],
            "book_id": receipt["book_ids"][0],
            "kind": receipt["kind"],
            "book_ids": receipt["book_ids"],
            "created_at": receipt["created_at"],
            "title": receipt["title"],
            "filename": receipt["filename"],
            "state": "removed",
            "delete_requested": receipt["delete_requested"],
            "delete_pending": receipt["delete_pending"],
            "deletion_stopped": receipt["deletion_stopped"],
            "files_kept": not any(item["file"] == "deleted" for item in items),
            "restored": any(item["restored"] for item in items)
            and all(item["restored"] or item["file"] == "deleted" for item in items),
            "items": [
                {key: item[key] for key in ("book_id", "title", "filename", "file", "restored")}
                for item in items
            ],
        }

    def _expired(self, receipt: dict) -> bool:
        return receipt["created_at"] < int(time.time()) - RETENTION_SECONDS

    def _restorable_items(self, receipt: dict) -> list[dict]:
        if receipt["delete_pending"] or self._expired(receipt):
            return []
        return [item for item in receipt["items"] if not item["restored"] and item["file"] != "deleted"]

    def _can_restore(self, receipt: dict) -> bool:
        # The restore action verifies files. Listing receipts must not stat thousands of NAS paths.
        return bool(self._restorable_items(receipt))

    def get(self, identifier: str) -> dict:
        identifier = self._identifier(identifier)
        with self.library.connection() as db:
            receipt = self._load(db, identifier)
        if receipt is None:
            raise NotFound("Removal receipt not found")
        return self._output(receipt)

    def list(self, *, limit: int = 50, after: str = "") -> dict:
        """Read-only history page, newest first; expired receipts are purged by the hourly maintenance."""
        if not 1 <= limit <= 200:
            raise InvalidInput("Invalid removal history page")
        (before,) = decode_cursor(after, int) if after else (None,)
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,id FROM removal WHERE (? IS NULL OR rowid<?) "
                "ORDER BY rowid DESC LIMIT ?",
                (before, before, limit),
            ).fetchall()
            receipts = [self._load(db, row["id"]) for row in rows]
        try:
            self.files.root.check()
            connected = True
        except (OSError, ValueError):
            connected = False
        return {
            "items": [
                self._output(receipt) | {"can_restore": connected and self._can_restore(receipt)}
                for receipt in receipts
                if receipt is not None
            ],
            "after": encode_cursor(rows[-1]["cursor"]) if len(rows) == limit else None,
        }

    def purge(self) -> int:
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM removal WHERE created_at<? "
                "AND coalesce(json_extract(receipt_json,'$.delete_pending'),0)=0",
                (int(time.time()) - RETENTION_SECONDS,),
            ).rowcount

    def pending_deletions(self) -> list[str]:
        with self.library.connection() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT id FROM removal WHERE json_extract(receipt_json,'$.delete_pending')=1"
                )
            ]

    def finish_pending(self) -> bool:
        def select(after: str, limit: int) -> list[str]:
            with self.library.connection() as db:
                return [
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM removal WHERE json_extract(receipt_json,'$.delete_pending')=1 "
                        "AND id>? ORDER BY id LIMIT ?",
                        (after, limit),
                    )
                ]

        settled = False
        for identifier in self.deletion_retry.take(select, 10):
            try:
                self.finish_deletion(identifier)
            except ImportCancelled:
                raise
            except (OSError, ValueError, sqlite3.Error):
                self.deletion_retry.failed(identifier)
            else:
                self.deletion_retry.succeeded(identifier)
                settled = True
        return settled

    # Removal ---------------------------------------------------------------

    def remove(
        self, identifier: str, book_id: str, token: str, *, confirmed: bool, delete_file: bool = False
    ) -> dict:
        return self._remove(
            identifier, "single", [book_id], token, confirmed=confirmed, delete_file=delete_file
        )

    def remove_selected(
        self, identifier: str, book_ids: list[str], token: str, *, confirmed: bool, delete_file: bool = False
    ) -> dict:
        return self._remove(
            identifier, "selected", self._ids(book_ids), token, confirmed=confirmed, delete_file=delete_file
        )

    def _remove(
        self,
        identifier: str,
        kind: Literal["single", "selected"],
        book_ids: list[str],
        token: str,
        *,
        confirmed: bool,
        delete_file: bool,
    ) -> dict:
        if confirmed is not True:
            raise InvalidInput("Explicit confirmation is required")
        identifier = self._identifier(identifier)

        def previous(db: sqlite3.Connection) -> bool:
            receipt = self._load(db, identifier)
            if receipt and (
                receipt["book_ids"] != book_ids
                or receipt["kind"] != kind
                or receipt["delete_requested"] != delete_file
                or receipt["token"] != token
            ):
                raise RevisionConflict("Removal receipt does not match this request")
            return receipt is not None

        with self.library.connection() as db:
            done = previous(db)
        if not done:
            with ExitStack() as locks:
                try:
                    locks.enter_context(
                        self.files.root.mutation(lock_name=".mangacouch-scan.lock", blocking=False)
                    )
                except BlockingIOError:
                    raise RevisionConflict(
                        "A scan batch is still running; pause it and refresh the preview",
                        code="removal_scan_active",
                    ) from None
                locks.enter_context(self.files.root.mutation())
                self._commit_removal(identifier, kind, book_ids, token, delete_file, previous)
        if delete_file:
            self.finish_deletion(identifier)
        return self.get(identifier)

    def _commit_removal(
        self,
        identifier: str,
        kind: Literal["single", "selected"],
        book_ids: list[str],
        token: str,
        delete_file: bool,
        previous: Callable[[sqlite3.Connection], bool],
    ) -> None:
        with self.library.connection() as db:
            # Another request with this identifier may have committed between the caller's receipt
            # check and the locks; its books are gone, so answer from the receipt, not "not found".
            if previous(db):
                return
            if kind == "selected":
                self._batch_preview(db, book_ids, identifier)
            else:
                self._preview(db, book_ids[0], identifier)
        self.files.root.check(writable=True)
        observations = {
            book.id: self._observe(book.content.relative_path) for book in self.library.get_books(book_ids)
        }
        with self.library.transaction(durable=True) as db:
            if previous(db):
                return
            preview = (
                self._batch_preview(db, book_ids, identifier, observations)
                if kind == "selected"
                else self._preview(db, book_ids[0], identifier, observations)
            )
            if preview["token"] != token:
                raise RevisionConflict("Book, files or related state changed; request a fresh preview")
            if preview["blocked"]:
                raise InvalidInput(
                    "Resolve blocked work before removing this record",
                    code="removal_scan_active"
                    if "scan_active" in preview["blocked"]
                    else "removal_import_pending",
                )
            if delete_file and preview["delete_blocked"]:
                raise InvalidInput(
                    "The file must be verified before it can be deleted",
                    code="removal_archive_changed"
                    if "archive_changed" in preview["delete_blocked"]
                    else "removal_archive_unreadable",
                )
            previews = preview["items"] if kind == "selected" else [preview]
            rows = {book_id: _capture(db, book_id) | {"placements": []} for book_id in book_ids}
            for collection_id in sorted({c["id"] for item in previews for c in item["collections"]}):
                members = self._members(db, collection_id)
                for index, book_id in enumerate(members):
                    if book_id in rows:
                        rows[book_id]["placements"].append(
                            {
                                "collection_id": collection_id,
                                "index": index,
                                "before": members[max(0, index - PLACEMENT_ANCHORS) : index][::-1],
                                "after": members[index + 1 : index + 1 + PLACEMENT_ANCHORS],
                            }
                        )
            receipt = {
                "kind": kind,
                "book_ids": book_ids,
                "title": preview["title"],
                "filename": preview["filename"],
                "delete_requested": delete_file,
                "delete_pending": delete_file,
                "deletion_stopped": None,
                "items": [
                    {
                        "book_id": item["book_id"],
                        "title": item["title"],
                        "filename": item["filename"],
                        "relative_path": item["relative_path"],
                        "sha256": rows[item["book_id"]]["sha256"],
                        "presence": item["presence"],
                        "file_stamp": item["verified_stamp"],
                        "file": "kept",
                        "restored": False,
                    }
                    for item in previews
                ],
            }
            db.executemany("DELETE FROM book WHERE id=?", ((book_id,) for book_id in book_ids))
            collections = sorted({c["id"] for item in previews for c in item["collections"]})
            for collection_id in collections:
                self._reorder(db, collection_id, self._members(db, collection_id))
            db.execute(
                "INSERT INTO removal(id,token,receipt_json,rows_json,created_at) VALUES(?,?,?,?,?)",
                (identifier, token, canonical_json(receipt), canonical_json(rows), int(time.time())),
            )
            self.files.root.check()
        for book_id in book_ids:
            self.files.availability_failures.pop(book_id, None)

    @staticmethod
    def _members(db: sqlite3.Connection, collection_id: str) -> list[str]:
        return [
            row[0]
            for row in db.execute(
                "SELECT book_id FROM collection_item WHERE collection_id=? ORDER BY position",
                (collection_id,),
            )
        ]

    @staticmethod
    def _reorder(db: sqlite3.Connection, collection_id: str, members: list[str]) -> None:
        db.execute("DELETE FROM collection_item WHERE collection_id=?", (collection_id,))
        db.executemany(
            "INSERT INTO collection_item(collection_id,book_id,position) VALUES(?,?,?)",
            ((collection_id, book_id, position) for position, book_id in enumerate(members)),
        )
        db.execute("UPDATE collection SET revision=revision+1 WHERE id=?", (collection_id,))

    # File deletion ---------------------------------------------------------

    def finish_deletion(self, identifier: str) -> None:
        """Hash outside the operation lock and recheck identity before each unlink."""
        with self.library.connection() as db:
            receipt = self._load(db, identifier)
        if receipt is None or not receipt["delete_pending"]:
            return
        pending = [item for item in receipt["items"] if item["file"] != "deleted"]
        verified = []
        for item in pending:
            check_cancel(thread_cancelled())
            valid, stamp = self._verified(item)
            if stamp is not None and not valid:
                self._stop_deletion(identifier, "file_changed")
                return
            verified.append((item, stamp))
        for item, stamp in verified:
            check_cancel(thread_cancelled())
            with self.files.root.mutation():
                with self.library.connection() as db:
                    current = self._load(db, identifier)
                    if current is None or not current["delete_pending"]:
                        return
                    if any(
                        stored["book_id"] == item["book_id"] and stored["file"] == "deleted"
                        for stored in current["items"]
                    ):
                        continue
                    registered = db.execute(
                        "SELECT 1 FROM book WHERE sha256=? OR json_extract(content_json,'$.relative_path')=?",
                        (item["sha256"], item["relative_path"]),
                    ).fetchone()
                if registered:
                    self._stop_deletion(identifier, "file_registered_again")
                    return
                archive = self.files.root.resolve(item["relative_path"])
                if self._observe(item["relative_path"])[1] != stamp:
                    self._stop_deletion(identifier, "file_changed")
                    return
                for path in (
                    eze_path(archive),
                    sidecar_path(self.files.root, item["relative_path"]),
                    archive,
                ):
                    close_pdf(path)
                    path.unlink(missing_ok=True)
                sync_directory(archive.parent)
                with self.library.transaction(durable=True) as db:
                    current = self._load(db, identifier)
                    if current is None:
                        return
                    for stored in current["items"]:
                        if stored["book_id"] == item["book_id"]:
                            stored["file"] = "deleted"
                    self._save(db, current)
        with self.library.transaction(durable=True) as db:
            current = self._load(db, identifier)
            if current is not None and current["delete_pending"]:
                current["delete_pending"] = False
                self._save(db, current)

    def cancel_deletion(self, identifier: str) -> dict:
        identifier = self._identifier(identifier)
        with self.files.root.mutation():
            self._stop_deletion(identifier, "cancelled")
            return self.get(identifier)

    def _stop_deletion(self, identifier: str, reason: str) -> None:
        with self.library.transaction(durable=True) as db:
            receipt = self._load(db, identifier)
            if receipt is None:
                raise NotFound("Removal receipt not found")
            if receipt["delete_pending"]:
                receipt.update(delete_pending=False, deletion_stopped=reason)
                self._save(db, receipt)

    # Restore ---------------------------------------------------------------

    def restore(self, identifier: str) -> dict:
        """Restore every remaining item whose original file is unchanged."""
        identifier = self._identifier(identifier)
        with self.library.connection() as db:
            receipt = self._load(db, identifier)
        if receipt is None or self._expired(receipt):
            raise NotFound("Removal receipt not found")
        if receipt["delete_pending"]:
            raise RevisionConflict(
                "Stop the pending file deletion before restoring", code="removal_pending_deletion"
            )
        candidates = self._restorable_items(receipt)
        if not candidates:
            if self._output(receipt)["restored"]:
                return self._output(receipt)
            raise RevisionConflict("Original files are unavailable", code="removal_files_unavailable")
        ready = []
        for item in candidates:
            check_cancel(thread_cancelled())
            try:
                valid, stamp = self._verified(item)
                if valid:
                    ready.append(item | {"file_stamp": stamp})
            except OSError:
                pass
        if not ready:
            raise RevisionConflict("Original files are missing or changed", code="removal_files_unavailable")
        with self.files.root.mutation():
            ready = [item for item in ready if self._observe(item["relative_path"])[1] == item["file_stamp"]]
            with self.library.transaction(durable=True) as db:
                current = self._load(db, identifier)
                if current is None:
                    raise NotFound("Removal receipt not found")
                rows = json.loads(
                    db.execute("SELECT rows_json FROM removal WHERE id=?", (identifier,)).fetchone()[0]
                )
                restored = []
                for item in ready:
                    if db.execute(
                        "SELECT 1 FROM book WHERE id=? OR sha256=?", (item["book_id"], item["sha256"])
                    ).fetchone():
                        continue
                    self._reinsert(db, rows[item["book_id"]])
                    db.execute("UPDATE book SET availability='available' WHERE id=?", (item["book_id"],))
                    db.execute(
                        "UPDATE archive_manifest SET file_stamp=? WHERE book_id=?",
                        (canonical_json(item["file_stamp"]), item["book_id"]),
                    )
                    # A deletion stopped halfway may have removed the sidecars already.
                    resync_sidecars(db, item["book_id"])
                    restored.append(item["book_id"])
                if not restored:
                    raise RevisionConflict(
                        "These books are already in the library again", code="removal_already_restored"
                    )
                self._place(db, restored, rows)
                for item in current["items"]:
                    if item["book_id"] in restored:
                        item["restored"] = True
                self._save(db, current)
        return self.get(identifier)

    def _place(self, db: sqlite3.Connection, restored: list[str], rows: dict) -> None:
        """Put restored books back into the static collections they were in, in their old order.

        A book returns after the nearest earlier neighbour still in the collection, else before
        the nearest later one, else at its old index (or the end when the collection has since
        become shorter). Books are placed in their old order, so a batch restore keeps its own
        order and books restored together anchor each other. Deleted collections are skipped.
        """
        placements: dict[str, list[dict]] = {}
        for book_id in restored:
            for placement in rows[book_id]["placements"]:
                placements.setdefault(placement["collection_id"], []).append(placement | {"book_id": book_id})
        for collection_id, items in sorted(placements.items()):
            if not db.execute("SELECT 1 FROM collection WHERE id=?", (collection_id,)).fetchone():
                continue
            members = self._members(db, collection_id)
            for item in sorted(items, key=lambda value: value["index"]):
                if item["book_id"] in members:
                    continue
                present = set(members)
                earlier = next((anchor for anchor in item["before"] if anchor in present), None)
                later = next((anchor for anchor in item["after"] if anchor in present), None)
                if earlier is not None:
                    index = members.index(earlier) + 1
                elif later is not None:
                    index = members.index(later)
                else:
                    index = min(item["index"], len(members))
                members.insert(index, item["book_id"])
            self._reorder(db, collection_id, members)

    @staticmethod
    def _reinsert(db: sqlite3.Connection, captured: dict) -> None:
        references = _foreign_keys(db)
        remapped: dict[str, dict[int, int]] = {}
        for table, rows in captured["tables"]:
            if table in DERIVED_TABLES or table == "collection_item":
                continue  # placed at the recorded positions afterwards
            primary, rowid_alias = _primary_key(db, table)
            for encoded in rows:
                row = _decode(encoded)
                if table == "book":
                    row["indexed_pages"] = 0  # rebuilt as captured page features are inserted
                    row["recent_order"] = 0  # rebuilt from captured reading activity
                for key in references[table]:
                    value = row[key["column"]]
                    if value is None:
                        continue
                    value = remapped.get(key["parent"], {}).get(value, value)
                    row[key["column"]] = value
                    if not db.execute(
                        f"SELECT 1 FROM {key['parent']} WHERE {key['target']}=?", (value,)
                    ).fetchone():
                        break  # its other parent (collection, credential, another book) is gone
                else:
                    if table == "source_check" and row["state"] == "running":
                        row["state"], row["generation"] = "queued", row["generation"] + 1
                    if table == "review_event":
                        details = json.loads(row["details_json"])
                        evidence = details.get("evidence_event_id")
                        if evidence in remapped.get("review_event", {}):
                            details["evidence_event_id"] = remapped["review_event"][evidence]
                            row["details_json"] = canonical_json(details)
                    exists = (
                        primary != ["rowid"]
                        and db.execute(
                            f"SELECT 1 FROM {table} WHERE {' AND '.join(f'{name}=?' for name in primary)}",
                            [row[name] for name in primary],
                        ).fetchone()
                    )
                    if exists and not rowid_alias:
                        continue  # shared row (a duplicate pair) already restored with the other book
                    old_id = row[primary[0]] if rowid_alias else None
                    if exists or (rowid_alias and table in REINDEXED_TABLES):
                        row.pop(primary[0])
                    columns = list(row)
                    cursor = db.execute(
                        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [row[name] for name in columns],
                    )
                    if table == "page_feature" and row["dhash"]:
                        db.executemany(
                            "INSERT INTO page_band VALUES(?,?,?)",
                            [
                                (segment, value, cursor.lastrowid)
                                for segment, value in bands(int(row["dhash"], 16))
                            ],
                        )
                    if table == "book":
                        write_tags(db, row["id"], Metadata.model_validate_json(row["metadata_json"]))
                        write_source_links(db, row["id"], Metadata.model_validate_json(row["metadata_json"]))
                    if old_id is not None and cursor.lastrowid is not None and cursor.lastrowid != old_id:
                        remapped.setdefault(table, {})[old_id] = cursor.lastrowid
        for link in captured["links"]:
            condition = " AND ".join(f"{name}=?" for name in link["key"])
            column = link["column"]
            for values in link["rows"]:
                db.execute(
                    f"UPDATE {link['table']} SET {column}=? WHERE {condition} AND {column} IS NULL",
                    [values[-1], *values[:-1]],
                )
