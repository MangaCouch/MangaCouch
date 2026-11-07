from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .archives import ImportCancelled, check_cancel
from .editions import Editions
from .errors import InvalidInput, NotFound
from .eze import eze_path
from .eze_cache import EzeCache
from .files import ContentChanged, RootUnavailable, file_stamp, sync_directory
from .ingest import FileLibrary, error_reason
from .models import canonical_json
from .sidecars import SidecarSync

MAX_DIRECTORY_ENTRIES = 100_000
ARCHIVE_SUFFIXES = {".zip", ".cbz", ".pdf"}


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
        self.stopping = False
        self._stop_checked: dict[str, tuple[float, bool]] = {}

    def latest(self) -> dict | None:
        with self.library.connection() as db:
            row = db.execute("SELECT id FROM scan_run ORDER BY rowid DESC LIMIT 1").fetchone()
        return self.get(row[0]) if row else None

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

    def get(self, identifier: str, *, after: int = 0, limit: int = 50) -> dict:
        if not 1 <= limit <= 200 or after < 0:
            raise InvalidInput("Invalid scan result bounds")
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
                "(kind='archive' OR state='error') ORDER BY id LIMIT ?",
                (identifier, after, limit),
            ).fetchall()
            result["items"] = [{**dict(row), "warnings": json.loads(row["warnings_json"])} for row in rows]
            result["after"] = rows[-1]["id"] if len(rows) == limit else None
        previous = Editions(self.library).previous_many(
            [item["book_id"] for item in result["items"] if item["book_id"]]
        )
        for item in result["items"]:
            del item["warnings_json"]
            item["previous_books"] = previous.get(item["book_id"], [])
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
        return self.get(identifier)

    def pause(self, identifier: str) -> dict:
        self._stop_checked.pop(identifier, None)
        with self.library.transaction() as db:
            if not db.execute("SELECT 1 FROM scan_run WHERE id=?", (identifier,)).fetchone():
                raise NotFound("Scan not found")
            db.execute("UPDATE scan_run SET state='paused' WHERE id=? AND state='active'", (identifier,))
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
                    entry.name.startswith(".mangacouch")
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
        if not book_id:
            # Inbox files remain user-owned until cleanup succeeds. They must stay independent
            # of the published archive even if cleanup, sidecar sync or the process fails.
            result = self.files.import_file(path, cancelled=lambda: self._stopped(identifier))
            state = "indexed" if result.created else "duplicate"
            book_id, warnings = result.book.id, list(result.warnings)
        EzeCache(self.files).sync_one(book_id)
        sync = SidecarSync(self.files).sync_one(book_id)
        with self.files.root.mutation():
            if file_stamp(path) != before or not self._available(book_id):
                raise ContentChanged("Import changed before source cleanup")
            keep = self._keep_source(identifier)
            if {sidecar: file_stamp(sidecar) for sidecar in sidecars if sidecar.exists()} != sidecar_stamps:
                raise ContentChanged("Source sidecar changed during import")
            if not keep and not warnings and sync.get("state") in {"synced", "unchanged"}:
                try:
                    path.unlink()
                    for sidecar in sidecar_stamps:
                        sidecar.unlink()
                    sync_directory(path.parent)
                except PermissionError:
                    warnings.append("source_cleanup_permission_denied")
            else:
                if not keep:
                    warnings.append("source_retained_for_review")
            with self.library.transaction() as db:
                if not set(warnings) - {"source_cleanup_permission_denied"} and sync.get("state") in {
                    "synced",
                    "unchanged",
                }:
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

    def step(self, identifier: str, *, limit: int = 1) -> dict:
        if not 1 <= limit <= 10:
            raise InvalidInput("Scan batch must contain 1 to 10 work items")
        self.summary(identifier)
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
                            db.execute(
                                "UPDATE scan_run SET state='done',error_code=NULL "
                                "WHERE id=? AND state='active'",
                                (identifier,),
                            )
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
                        self.files.root.check()
                        code, message = error_reason(error)
                        with self.library.transaction() as db:
                            db.execute(
                                "UPDATE scan_item SET state='error',error_code=?,error_message=? WHERE id=?",
                                (code, message, item["id"]),
                            )
        except BlockingIOError:
            pass
        except RootUnavailable:
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE scan_run SET state='paused',error_code='RootUnavailable' "
                    "WHERE id=? AND state='active'",
                    (identifier,),
                )
        summary = self.summary(identifier)
        if summary is None:
            raise NotFound("Scan not found")
        return summary
