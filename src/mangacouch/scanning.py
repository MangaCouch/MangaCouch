from __future__ import annotations

import errno
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import overload

from .archives import ImportCancelled, check_cancel
from .cursors import decode_cursor, encode_cursor
from .database import busy
from .editions import Editions
from .errors import InvalidInput, NotFound
from .eze import eze_path
from .eze_cache import EzeCache
from .files import ContentChanged, RootUnavailable, file_stamp, sync_directory
from .ingest import FileLibrary, error_reason
from .logs import short_id
from .models import canonical_json
from .sidecars import SidecarSync

LOGGER = logging.getLogger(__name__)
MAX_DIRECTORY_ENTRIES = 100_000
SCAN_RETENTION = 30 * 86400
"""Finished scans (and their per-file results) older than this are deleted; the latest stays."""
ARCHIVE_SUFFIXES = {".zip", ".cbz", ".pdf"}
INFORMATIONAL = {"changed_file_set_aside"}
"""Import warnings that need no review: the source is still cleaned up."""


class DirectoryEntryLimit(InvalidInput):
    def __init__(self, message: str = "Directory exceeds scan entry limit"):
        super().__init__(message, code="directory_too_large")


def sidecar_paths(path: Path) -> list[Path]:
    """Metadata files that travel with an archive in the import folder."""
    return [eze_path(path), path.with_suffix(".eze.json"), path.with_name(path.name + ".mangacouch.json")]


def _is_sidecar(directory: Path, name: str) -> bool:
    """True for a metadata file named after an archive next to it; such files are not 'ignored'."""
    for suffix in (".mangacouch.json", ".eze.json"):
        if name.endswith(suffix):
            stem = name.removesuffix(suffix)
            candidates = [stem + extension for extension in sorted(ARCHIVE_SUFFIXES)]
            if Path(stem).suffix.lower() in ARCHIVE_SUFFIXES:
                candidates.append(stem)
            return any((directory / candidate).is_file() for candidate in candidates)
    return False


class Scanner:
    def __init__(self, files: FileLibrary):
        self.files, self.library = files, files.library
        self.editions = Editions(self.library)
        self.eze_cache = EzeCache(files)
        self.sync = SidecarSync(files)
        self.stopping = False
        self._stop_checked: dict[str, tuple[float, bool]] = {}

    def latest(self, *, state: str = "", limit: int = 50) -> dict | None:
        with self.library.connection() as db:
            row = db.execute("SELECT id FROM scan_run ORDER BY rowid DESC LIMIT 1").fetchone()
        return self.get(row[0], state=state, limit=limit) if row else None

    def purge(self) -> int:
        """Hourly: drop old finished scan runs; import receipts stay, they skip unchanged files."""
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM scan_run WHERE state='done' AND created_at<? "
                "AND rowid<(SELECT max(rowid) FROM scan_run)",
                (int(time.time()) - SCAN_RETENTION,),
            ).rowcount

    @overload
    def summary(self, identifier: str) -> dict: ...

    @overload
    def summary(self, identifier: None = None) -> dict | None: ...

    def summary(self, identifier: str | None = None) -> dict | None:
        """Light progress view of the latest scan, or of `identifier` (KeyError when unknown)."""
        with self.library.connection() as db:
            run = db.execute(
                "SELECT id,state,error_code FROM scan_run WHERE (? IS NULL OR id=?) "
                "ORDER BY rowid DESC LIMIT 1",
                (identifier, identifier),
            ).fetchone()
            if run is None:
                if identifier is not None:
                    raise NotFound("Scan not found")
                return None
            total, processed = db.execute(
                "SELECT count(*),coalesce(sum(state!='pending'),0) FROM scan_item "
                "WHERE scan_id=? AND kind='archive'",
                (run["id"],),
            ).fetchone()
            return {**dict(run), "archives": total, "processed": processed}

    def get(self, identifier: str, *, after: str = "", limit: int = 50, state: str = "") -> dict:
        if state not in {"", "pending", "expanded", "indexed", "unchanged", "duplicate", "error"}:
            raise InvalidInput("Invalid scan item state")
        if not 1 <= limit <= 200:
            raise InvalidInput("Invalid scan result bounds")
        (after_id,) = decode_cursor(after, int) if after else (0,)
        with self.library.connection() as db:
            db.execute("BEGIN")
            run = db.execute("SELECT * FROM scan_run WHERE id=?", (identifier,)).fetchone()
            if run is None:
                raise NotFound("Scan not found")
            result = dict(run)
            policy = db.execute(
                "SELECT keep_source FROM scan_policy WHERE scan_id=?", (identifier,)
            ).fetchone()
            result["keep_source"] = bool(policy[0]) if policy else True
            result["counts"] = {
                state: 0 for state in ("pending", "expanded", "indexed", "unchanged", "duplicate", "error")
            }
            result["counts"].update(
                {
                    row[0]: row[1]
                    for row in db.execute(
                        "SELECT state,count(*) FROM scan_item WHERE scan_id=? GROUP BY state", (identifier,)
                    )
                }
            )
            result["with_metadata"] = db.execute(
                "SELECT count(*) FROM scan_item s JOIN metadata_import m ON m.book_id=s.book_id "
                "WHERE s.scan_id=? AND s.state='indexed'",
                (identifier,),
            ).fetchone()[0]
            result["without_metadata"] = result["counts"]["indexed"] - result["with_metadata"]
            result["archives"] = db.execute(
                "SELECT count(*) FROM scan_item WHERE scan_id=? AND kind='archive'", (identifier,)
            ).fetchone()[0]
            result["processed"] = db.execute(
                "SELECT count(*) FROM scan_item WHERE scan_id=? AND kind='archive' AND state!='pending'",
                (identifier,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM scan_item WHERE scan_id=? AND id>? AND "
                "(kind='archive' OR state='error') AND (?='' OR state=?) ORDER BY id LIMIT ?",
                (identifier, after_id, state, state, limit),
            ).fetchall()
            result["items"] = [{**dict(row), "warnings": json.loads(row["warnings_json"])} for row in rows]
            result["after"] = encode_cursor(rows[-1]["id"]) if len(rows) == limit else None
        for item in result["items"]:
            del item["warnings_json"]
        return result

    def item(self, identifier: str, item_id: int) -> dict:
        with self.library.connection() as db:
            row = db.execute(
                "SELECT * FROM scan_item WHERE scan_id=? AND id=?", (identifier, item_id)
            ).fetchone()
        if row is None:
            raise NotFound("Scan item not found")
        result = dict(row)
        result["previous_books"] = (
            self.editions.previous_many([row["book_id"]]).get(row["book_id"], []) if row["book_id"] else []
        )
        return result

    def start(self, *, keep_source: bool = False) -> dict:
        self.files.root.check(writable=True)
        self.files.root.resolve("import").mkdir(exist_ok=True)
        with self.library.transaction() as db:
            old = db.execute("SELECT id FROM scan_run WHERE state!='done'").fetchone()
            identifier = old[0] if old else str(uuid.uuid4())
            if old:
                # Starting again continues the unfinished run with the policy asked for now.
                self._stop_checked.pop(identifier, None)
                db.execute(
                    "UPDATE scan_policy SET keep_source=? WHERE scan_id=?", (int(keep_source), identifier)
                )
                db.execute(
                    "UPDATE scan_run SET state='active',error_code=NULL WHERE id=? AND state='paused'",
                    (identifier,),
                )
            else:
                db.execute(
                    "INSERT INTO scan_run(id,state,created_at) VALUES(?,'active',?)",
                    (identifier, int(time.time())),
                )
                db.execute("INSERT INTO scan_policy VALUES(?,?)", (identifier, int(keep_source)))
                db.execute(
                    "INSERT INTO scan_item(scan_id,path,kind) VALUES(?,'import','directory')", (identifier,)
                )
        LOGGER.info(
            "Import folder scan %s %s (keep source files: %s)",
            short_id(identifier),
            "resumed" if old else "started",
            "yes" if keep_source else "no",
        )
        return self.get(identifier)

    def pause(self, identifier: str) -> dict:
        self._stop_checked.pop(identifier, None)
        with self.library.transaction() as db:
            if not db.execute("SELECT 1 FROM scan_run WHERE id=?", (identifier,)).fetchone():
                raise NotFound("Scan not found")
            db.execute(
                "UPDATE scan_run SET state='paused',error_code=NULL,retry_at=0 WHERE id=? AND state!='done'",
                (identifier,),
            )
        return self.get(identifier)

    def resume(self, identifier: str) -> dict:
        self._stop_checked.pop(identifier, None)
        self.files.root.check(writable=True)
        with self.library.transaction() as db:
            if not db.execute("SELECT 1 FROM scan_run WHERE id=?", (identifier,)).fetchone():
                raise NotFound("Scan not found")
            db.execute(
                "UPDATE scan_run SET state='active',error_code=NULL WHERE id=? AND state='paused'",
                (identifier,),
            )
        return self.get(identifier)

    def _stopped(self, identifier: str) -> bool:
        if self.stopping:
            return True
        checked, stopped = self._stop_checked.get(identifier, (0.0, False))
        now = time.monotonic()
        if now - checked < 0.1:
            return stopped
        with self.library.connection() as db:
            stopped = (
                db.execute("SELECT state FROM scan_run WHERE id=?", (identifier,)).fetchone()[0] != "active"
            )
        self._stop_checked[identifier] = (now, stopped)
        return stopped

    def _discover(self, identifier: str, item: sqlite3.Row) -> None:
        directory = self.files.root.resolve(item["path"])
        found, ignored = [], 0
        checked_at = 0.0
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries, 1):
                if count > MAX_DIRECTORY_ENTRIES:
                    raise DirectoryEntryLimit()
                if time.monotonic() - checked_at >= 0.1:
                    check_cancel(lambda: self._stopped(identifier))
                    checked_at = time.monotonic()
                if (
                    entry.name.startswith(".")
                    or entry.is_symlink()
                    or Path(entry.path).is_junction()
                    or any(0xD800 <= ord(c) <= 0xDFFF for c in entry.name)
                ):
                    ignored += 1
                    continue
                relative = (Path(item["path"]) / entry.name).as_posix()
                if entry.is_dir(follow_symlinks=False):
                    found.append((relative, "directory"))
                elif (
                    entry.is_file(follow_symlinks=False)
                    and Path(entry.name).suffix.lower() in ARCHIVE_SUFFIXES
                ):
                    found.append((relative, "archive"))
                elif not (entry.is_file(follow_symlinks=False) and _is_sidecar(directory, entry.name)):
                    ignored += 1
        self.files.root.check()
        with self.library.transaction() as db:
            if db.execute("SELECT state FROM scan_run WHERE id=?", (identifier,)).fetchone()[0] != "active":
                raise ImportCancelled("Scan paused")
            db.executemany(
                "INSERT OR IGNORE INTO scan_item(scan_id,path,kind) VALUES(?,?,?)",
                ((identifier, path, kind) for path, kind in sorted(found)),
            )
            db.execute("UPDATE scan_run SET ignored=ignored+? WHERE id=?", (ignored, identifier))
            db.execute("UPDATE scan_item SET state='expanded' WHERE id=?", (item["id"],))

    def _keep_source(self, identifier: str) -> bool:
        with self.library.connection() as db:
            return bool(
                db.execute("SELECT keep_source FROM scan_policy WHERE scan_id=?", (identifier,)).fetchone()[0]
            )

    def _available(self, book_id: str) -> bool:
        """Stamp-based check that the library file still holds the registered content."""
        state, _ = self.files.availability(self.library.get_book(book_id))
        return state == "available"

    def _import(self, identifier: str, item: sqlite3.Row, path: Path) -> None:
        before = file_stamp(path)
        sidecars = sidecar_paths(path)
        sidecar_stamps = {sidecar: file_stamp(sidecar) for sidecar in sidecars if sidecar.exists()}
        receipt_stamp = canonical_json(
            {
                "archive": before,
                "sidecars": {sidecar.name: stamp for sidecar, stamp in sidecar_stamps.items()},
            }
        )
        with self.library.connection() as db:
            old = db.execute("SELECT * FROM import_receipt WHERE source_path=?", (item["path"],)).fetchone()
        state, book_id, warnings = "unchanged", "", []
        if old and old["stamp_json"] == receipt_stamp and self._available(old["book_id"]):
            book_id = old["book_id"]
            if self._keep_source(identifier):
                with self.library.transaction() as db:
                    db.execute(
                        "UPDATE scan_item SET state='unchanged',book_id=? WHERE id=?", (book_id, item["id"])
                    )
                return
        if not book_id:
            # Inbox files remain user-owned until cleanup succeeds. They must stay independent
            # of the published archive even if cleanup, sidecar sync or the process fails.
            result = self.files.import_file(
                path,
                cancelled=lambda: self._stopped(identifier),
                durable=not self._keep_source(identifier),
                job_id=str(uuid.uuid5(uuid.UUID(identifier), item["path"])),
            )
            state = "indexed" if result.created else "duplicate"
            book_id, warnings = result.book.id, list(result.warnings)
        try:
            self.eze_cache.sync_one(book_id)
        except (OSError, ValueError, sqlite3.Error):
            LOGGER.warning("Eze sidecar remains pending for %s", short_id(book_id))
        sync = self.sync.sync_one(book_id)
        with self.files.root.mutation():
            if file_stamp(path) != before or not self._available(book_id):
                raise ContentChanged("Import changed before source cleanup")
            keep = self._keep_source(identifier)
            if {sidecar: file_stamp(sidecar) for sidecar in sidecars if sidecar.exists()} != sidecar_stamps:
                raise ContentChanged("Source sidecar changed during import")
            synced = sync.get("state") in {"synced", "unchanged"}
            cleanup = not keep and not set(warnings) - INFORMATIONAL and synced
            if not keep and not cleanup:
                warnings.append("source_retained_for_review")
            with self.library.transaction(durable=cleanup) as db:
                if synced and not set(warnings) - INFORMATIONAL:
                    db.execute(
                        "INSERT INTO import_receipt VALUES(?,?,?) ON CONFLICT(source_path) DO UPDATE SET "
                        "stamp_json=excluded.stamp_json,book_id=excluded.book_id",
                        (item["path"], receipt_stamp, book_id),
                    )
                else:
                    db.execute("DELETE FROM import_receipt WHERE source_path=?", (item["path"],))
                db.execute(
                    "UPDATE scan_item SET state=?,book_id=?,warnings_json=? WHERE id=?",
                    (state, book_id, canonical_json(warnings), item["id"]),
                )
            if cleanup:
                try:
                    path.unlink()
                    for sidecar in sidecar_stamps:
                        sidecar.unlink()
                    sync_directory(path.parent)
                except OSError:
                    warnings.append("source_cleanup_failed")
                    with self.library.transaction() as db:
                        db.execute(
                            "UPDATE scan_item SET warnings_json=? WHERE id=?",
                            (canonical_json(warnings), item["id"]),
                        )

    def step(self, identifier: str, *, limit: int = 1) -> dict:
        if not 1 <= limit <= 10:
            raise InvalidInput("Scan batch must contain 1 to 10 work items")
        self.summary(identifier)
        self.resume_storage_wait(identifier)
        try:
            with self.files.root.mutation(lock_name=".mangacouch-scan.lock", blocking=False):
                for _ in range(limit):
                    if self._stopped(identifier):
                        break
                    with self.library.connection() as db:
                        item = db.execute(
                            "SELECT * FROM scan_item WHERE scan_id=? AND state='pending' ORDER BY id LIMIT 1",
                            (identifier,),
                        ).fetchone()
                    if item is None:
                        with self.library.transaction() as db:
                            finished = db.execute(
                                "UPDATE scan_run SET state='done',error_code=NULL "
                                "WHERE id=? AND state='active'",
                                (identifier,),
                            ).rowcount
                        if finished:
                            LOGGER.info("Import folder scan %s finished", short_id(identifier))
                        break
                    try:
                        if item["kind"] == "directory":
                            self._discover(identifier, item)
                        else:
                            self._import(identifier, item, self.files.root.resolve(item["path"]))
                    except ImportCancelled:
                        break
                    except RootUnavailable:
                        raise
                    except Exception as error:
                        if busy(error):
                            break
                        if isinstance(error, OSError) and error.errno in {
                            errno.ENOSPC,
                            errno.EDQUOT,
                        }:
                            code = "insufficient_storage"
                            with self.library.transaction() as db:
                                db.execute(
                                    "UPDATE scan_run SET state='paused',error_code=?,"
                                    "retry_at=unixepoch()+300 "
                                    "WHERE id=?",
                                    (code, identifier),
                                )
                            break
                        self.files.root.check()
                        code, message = error_reason(error)
                        LOGGER.warning("Import folder scan: %r failed: %s (%s)", item["path"], code, message)
                        with self.library.transaction() as db:
                            db.execute(
                                "UPDATE scan_item SET state='error',error_code=?,error_message=? WHERE id=?",
                                (code, message, item["id"]),
                            )
        except BlockingIOError:
            LOGGER.debug("Import folder scan %s: another process holds the scan lock", short_id(identifier))
        except RootUnavailable:
            LOGGER.warning("Import folder scan %s paused: library root unavailable", short_id(identifier))
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE scan_run SET state='paused',error_code='root_unavailable',"
                    "retry_at=unixepoch()+300 "
                    "WHERE id=? AND state='active'",
                    (identifier,),
                )
        summary = self.summary(identifier)
        return summary

    def resume_storage_wait(self, identifier: str) -> bool:
        with self.library.connection() as db:
            ready = db.execute(
                "SELECT 1 FROM scan_run WHERE id=? AND state='paused' "
                "AND error_code IN ('insufficient_storage','root_unavailable') AND retry_at<=unixepoch()",
                (identifier,),
            ).fetchone()
        if not ready:
            return False
        self._stop_checked.pop(identifier, None)
        with self.library.transaction() as db:
            db.execute(
                "UPDATE scan_run SET state='active',error_code=NULL,retry_at=0 WHERE id=? "
                "AND state='paused' AND error_code IN ('insufficient_storage','root_unavailable')",
                (identifier,),
            )
        return True
