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

from .database import write_tags
from .errors import InvalidInput, NotFound, RevisionConflict
from .eze import eze_path
from .files import file_stamp, hash_file, sync_directory
from .ingest import FileLibrary
from .models import Content, Metadata, PersonalState, canonical_json
from .sidecars import sidecar_path

RETENTION_SECONDS = 7 * 86400
# Rebuilt by the book insert trigger, so restoring them explicitly would duplicate rows.
DERIVED_TABLES = frozenset({"book_tag"})


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

    @staticmethod
    def _identifier(identifier: str) -> str:
        try:
            if str(uuid.UUID(identifier)) == identifier:
                return identifier
        except ValueError:
            pass
        raise InvalidInput("Invalid removal identifier")

    # Preview ---------------------------------------------------------------

    def _preview(self, db: sqlite3.Connection, book_id: str, identifier: str) -> dict:
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
        stamp = None
        try:
            stamp = file_stamp(self.files.root.resolve(content.relative_path))
            presence = "present"
        except FileNotFoundError:
            presence = "missing"
        except OSError:
            presence = "unreadable"
            blocked.append("archive_unreadable")
        state = {
            "id": identifier,
            "root_id": self.files.root.root_id,
            "book": {key: value for key, value in dict(book).items() if key != "indexed_pages"},
            "related": related,
            "collections": collections,
            "presence": presence,
            "stamp": stamp,
            "blocked": blocked,
        }
        sync = related["sidecar_sync"][0]
        personal = PersonalState.model_validate_json(related["personal"][0]["state_json"])
        return {
            "id": identifier,
            "book_id": book_id,
            "token": hashlib.sha256(canonical_json(state).encode()).hexdigest(),
            "title": metadata.title or metadata.original_title or content.original_filename,
            "filename": content.original_filename,
            "relative_path": content.relative_path,
            "presence": presence,
            "personal": personal.model_dump(mode="json"),
            "collections": collections,
            "metadata_revision": book["metadata_revision"],
            "personal_revision": related["personal"][0]["revision"],
            "unsynced_metadata": sync["pending_revision"] > sync["synced_revision"],
            "pending_conflicts": sum(row["state"] == "pending" for row in related["sidecar_conflict"]),
            "blocked": blocked,
        }

    def preview(self, book_id: str) -> dict:
        with self.files.root.mutation(), self.library.connection() as db:
            db.execute("BEGIN")
            return self._preview(db, book_id, str(uuid.uuid4()))

    @staticmethod
    def _ids(book_ids: list[str]) -> list[str]:
        if (
            not 1 <= len(book_ids) <= 100
            or any(not isinstance(value, str) or not 1 <= len(value) <= 64 for value in book_ids)
            or len(set(book_ids)) != len(book_ids)
        ):
            raise InvalidInput("Select 1 to 100 distinct records")
        return sorted(book_ids)

    def _batch_preview(self, db: sqlite3.Connection, book_ids: list[str], identifier: str) -> dict:
        items = [self._preview(db, book_id, identifier) for book_id in book_ids]
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
        }

    def preview_selected(self, book_ids: list[str]) -> dict:
        book_ids = self._ids(book_ids)
        with self.files.root.mutation(), self.library.connection() as db:
            db.execute("BEGIN")
            return self._batch_preview(db, book_ids, str(uuid.uuid4()))

    # Missing-file maintenance ----------------------------------------------

    def check_page(self, *, after_id: str = "", through_id: str | None = None, limit: int = 100) -> dict:
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or len(after_id) > 64
            or (through_id is not None and len(through_id) > 64)
        ):
            raise InvalidInput("Invalid maintenance page")
        with self.files.root.mutation():
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
            observed = []
            for row in rows[:limit]:
                book = self.library.get_book(row[0])
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
            with self.library.transaction() as db:
                for book, state, stamp in observed:
                    db.execute("UPDATE book SET availability=? WHERE id=?", (state, book.id))
                    if stamp is not None:
                        db.execute(
                            "UPDATE archive_manifest SET file_stamp=? WHERE book_id=?", (stamp, book.id)
                        )
                self.files.root.check(writable=True)
            return {
                "checked": len(observed),
                "through_id": through_id,
                "after_id": observed[-1][0].id if len(rows) > limit else None,
                "items": [
                    {
                        "book_id": book.id,
                        "title": book.metadata.title
                        or book.metadata.original_title
                        or book.content.original_filename,
                        "relative_path": book.content.relative_path,
                        "state": state,
                    }
                    for book, state, _ in observed
                    if state != "available"
                ],
            }

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
        items = self._restorable_items(receipt)
        try:
            self.files.root.check()
            return any(self.files.root.resolve(item["relative_path"]).is_file() for item in items)
        except (OSError, ValueError):
            return False

    def get(self, identifier: str) -> dict:
        identifier = self._identifier(identifier)
        with self.library.connection() as db:
            receipt = self._load(db, identifier)
        if receipt is None:
            raise NotFound("Removal receipt not found")
        return self._output(receipt)

    def list(self, *, limit: int = 50, before: int | None = None) -> dict:
        """Read-only history page; expired receipts are purged by the hourly maintenance."""
        if not 1 <= limit <= 200 or (before is not None and before < 1):
            raise InvalidInput("Invalid removal history page")
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT rowid AS cursor,id FROM removal WHERE (? IS NULL OR rowid<?) "
                "ORDER BY rowid DESC LIMIT ?",
                (before, before, limit),
            ).fetchall()
            receipts = [self._load(db, row["id"]) for row in rows]
        return {
            "items": [
                self._output(receipt) | {"can_restore": self._can_restore(receipt)}
                for receipt in receipts
                if receipt is not None
            ],
            "before": rows[-1]["cursor"] if len(rows) == limit else None,
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
                        "A scan batch is still running; pause it and refresh the preview"
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
        with self.library.transaction(durable=True) as db:
            if previous(db):
                return
            preview = (
                self._batch_preview(db, book_ids, identifier)
                if kind == "selected"
                else self._preview(db, book_ids[0], identifier)
            )
            if preview["token"] != token:
                raise RevisionConflict("Book, files or related state changed; request a fresh preview")
            if preview["blocked"]:
                raise InvalidInput("Resolve blocked work before removing this record")
            self.files.root.check(writable=True)
            previews = preview["items"] if kind == "selected" else [preview]
            rows = {book_id: _capture(db, book_id) for book_id in book_ids}
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
            self.files.root.check(writable=True)

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
        """Delete the removed books' files and sidecars, checking every item before unlinking any."""
        with self.files.root.mutation():
            with self.library.connection() as db:
                receipt = self._load(db, identifier)
                if receipt is None or not receipt["delete_pending"]:
                    return
                pending = [item for item in receipt["items"] if item["file"] != "deleted"]
                for item in pending:
                    if db.execute(
                        "SELECT 1 FROM book WHERE sha256=? OR json_extract(content_json,'$.relative_path')=?",
                        (item["sha256"], item["relative_path"]),
                    ).fetchone():
                        self._stop_deletion(identifier, "file_registered_again")
                        return
                    archive = self.files.root.resolve(item["relative_path"])
                    if archive.exists() and hash_file(archive) != item["sha256"]:
                        self._stop_deletion(identifier, "file_changed")
                        return
            for item in pending:
                archive = self.files.root.resolve(item["relative_path"])
                # Sidecars first: if the archive cannot be unlinked the item is still truthfully "kept".
                for path in (
                    eze_path(archive),
                    sidecar_path(self.files.root, item["relative_path"]),
                    archive,
                ):
                    path.unlink(missing_ok=True)
                sync_directory(archive.parent)
                with self.library.transaction(durable=True) as db:
                    current = self._load(db, identifier)
                    if current is None or not current["delete_pending"]:
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

    def restore(self, identifier: str, book_ids: list[str] | None = None) -> dict:
        """Restore every remaining item (or the selected ones) whose original file is unchanged."""
        identifier = self._identifier(identifier)
        with self.files.root.mutation():
            with self.library.connection() as db:
                receipt = self._load(db, identifier)
            if receipt is None or self._expired(receipt):
                raise NotFound("Removal receipt not found")
            known = {item["book_id"] for item in receipt["items"]}
            if book_ids is not None and not set(book_ids) <= known:
                raise InvalidInput("Selected books are not part of this removal")
            if receipt["delete_pending"]:
                raise RevisionConflict("Stop the pending file deletion before restoring")
            candidates = [
                item
                for item in self._restorable_items(receipt)
                if book_ids is None or item["book_id"] in book_ids
            ]
            if not candidates:
                if self._output(receipt)["restored"]:
                    return self._output(receipt)
                raise RevisionConflict("Original files are unavailable")
            ready = []
            for item in candidates:
                try:
                    if hash_file(self.files.root.resolve(item["relative_path"])) == item["sha256"]:
                        ready.append(item)
                except OSError:
                    pass
            if not ready:
                raise RevisionConflict("Original files are missing or changed")
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
                    restored.append(item["book_id"])
                if not restored:
                    raise RevisionConflict("These books are already in the library again")
                placements = sorted(
                    (row["position"], row["collection_id"], row["book_id"])
                    for book_id in restored
                    for table, table_rows in rows[book_id]["tables"]
                    if table == "collection_item"
                    for row in table_rows
                )
                for position, collection_id, book_id in placements:
                    if not db.execute("SELECT 1 FROM collection WHERE id=?", (collection_id,)).fetchone():
                        continue
                    members = self._members(db, collection_id)
                    # The recorded position, or the end when the collection has since become shorter.
                    members.insert(min(position, len(members)), book_id)
                    self._reorder(db, collection_id, members)
                for item in current["items"]:
                    if item["book_id"] in restored:
                        item["restored"] = True
                self._save(db, current)
        return self.get(identifier)

    @staticmethod
    def _reinsert(db: sqlite3.Connection, captured: dict) -> None:
        references = _foreign_keys(db)
        remapped: dict[str, dict[int, int]] = {}
        for table, rows in captured["tables"]:
            if table == "collection_item":
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
                    if exists:
                        row.pop(primary[0])
                    columns = list(row)
                    cursor = db.execute(
                        f"INSERT INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                        [row[name] for name in columns],
                    )
                    if table == "book":
                        write_tags(db, row["id"], Metadata.model_validate_json(row["metadata_json"]))
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
