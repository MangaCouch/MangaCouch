from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn

from .archives import (
    IMAGE_VERSION,
    THUMBNAIL_HEIGHT,
    THUMBNAIL_WIDTH,
    ImportCancelled,
    InvalidArchive,
    Limits,
    Page,
    ThumbnailFormat,
    check_cancel,
    close_pdf,
    inspect_archive,
    read_page,
)
from .database import Book, Library
from .errors import InvalidInput
from .eze import MAX_EZE_BYTES, eze_path, import_eze
from .files import (
    CHUNK_SIZE,
    LINK_UNSUPPORTED,
    STAGING,
    ContentChanged,
    FileLock,
    LibraryRoot,
    RootUnavailable,
    _rename_new,
    file_stamp,
    hash_file,
    live_stamp,
    publish_file,
    require_free_space,
    set_aside,
    sync_directory,
    sync_file,
    thread_cancelled,
)
from .image_cache import ImageCache
from .logs import short_id
from .metadata_import import MetadataImport
from .models import MAX_METADATA_BYTES, Content, Metadata, NativeSidecar, canonical_json, read_native

LOGGER = logging.getLogger(__name__)
LEFTOVER = re.compile(r"\.mc-[0-9a-f]{12}\.tmp|\.mangacouch-publish-.+")
"""Temporary files of publish_file and of sidecar writes (atomic_write)."""


def error_reason(error: BaseException) -> tuple[str, str]:
    """A stable reason code plus a bounded human message for a failed import or scan item."""
    code = getattr(error, "code", None)
    if isinstance(error, InvalidInput) and isinstance(code, str):
        reason = code
    elif isinstance(error, RootUnavailable):
        reason = "storage_unavailable"
    elif isinstance(error, ContentChanged):
        reason = "content_changed"
    elif isinstance(error, FileNotFoundError):
        reason = "source_missing"
    elif isinstance(error, InvalidArchive):
        reason = "invalid_archive"
    elif isinstance(error, ImportCancelled):
        reason = "cancelled"
    elif isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        reason = "insufficient_storage"
    elif isinstance(error, sqlite3.Error):
        reason = "database_error"
    elif isinstance(error, OSError):
        reason = "storage_error"
    else:
        reason = "import_failed"
    return reason, (str(error) or type(error).__name__)[:500]


def _path_kept(db: sqlite3.Connection, relative_path: str) -> bool:
    """Whether a book or a removal receipt (recycle bin or pending deletion) uses this file."""
    return bool(
        db.execute(
            "SELECT 1 FROM book WHERE json_extract(content_json,'$.relative_path')=?", (relative_path,)
        ).fetchone()
        or db.execute(
            "SELECT 1 FROM removal, json_each(removal.receipt_json,'$.items') AS item "
            "WHERE json_extract(item.value,'$.relative_path')=? "
            "AND json_extract(item.value,'$.file') IS NOT 'deleted' LIMIT 1",
            (relative_path,),
        ).fetchone()
    )


def resync_sidecars(db: sqlite3.Connection, book_id: str) -> None:
    """Mark a book's native and Eze sidecars as unwritten, e.g. after its archive moved or its
    record came back from the recycle bin; the next sync or maintenance pass writes them."""
    db.execute("UPDATE sidecar_sync SET synced_revision=0 WHERE book_id=?", (book_id,))
    db.execute("UPDATE eze_cache SET synced_hash=NULL WHERE book_id=?", (book_id,))


def _too_large() -> InvalidInput:
    return InvalidInput("Archive exceeds configured byte limit", code="archive_too_large")


@dataclass(frozen=True)
class ImportResult:
    book: Book
    created: bool
    job_id: str
    warnings: tuple[str, ...]


def _serialize_import(result: MetadataImport | None) -> str | None:
    if result is None:
        return None
    return canonical_json(
        {
            "metadata": result.metadata.model_dump(mode="json"),
            "candidates": {key: value.model_dump(mode="json") for key, value in result.candidates.items()},
            "conflicts": result.conflicts,
            "ignored_fields": result.ignored_fields,
            "warnings": result.warnings,
            "native": result.native.model_dump(mode="json", by_alias=True) if result.native else None,
            "native_raw_hash": result.native_raw_hash,
            "eze_payload": result.eze_payload,
        }
    )


def _deserialize_import(data: str | None) -> MetadataImport | None:
    if data is None:
        return None
    value = json.loads(data)
    return MetadataImport(
        metadata=Metadata.model_validate(value["metadata"]),
        candidates={key: Metadata.model_validate(item) for key, item in value["candidates"].items()},
        conflicts=tuple(value["conflicts"]),
        ignored_fields=tuple(value["ignored_fields"]),
        warnings=tuple(value["warnings"]),
        native=NativeSidecar.model_validate(value["native"]) if value.get("native") else None,
        native_raw_hash=value.get("native_raw_hash"),
        eze_payload=value.get("eze_payload"),
    )


class FileLibrary:
    def __init__(self, library: Library, root: Path, limits: Limits | None = None):
        self.availability_locks = [threading.RLock() for _ in range(64)]
        self.availability_failures: dict[str, tuple[str, str | None]] = {}
        self.unavailable_after = ""
        self.manifests: OrderedDict[tuple[str, str], list] = OrderedDict()
        self.manifest_lock = threading.Lock()
        self.library = library
        self.root = LibraryRoot(library, root)
        self.limits = limits or Limits()
        self.page_cache = ImageCache(
            library.path.parent / f".mangacouch-cache-{library.path.name}",
            name="pages",
            disk_bytes=512 * 1024**2,
            memory_bytes=64 * 1024**2,
            memory_entries=16,
            item_bytes=64 * 1024**2,
            pending_bytes=64 * 1024**2,
            batch_size=1,
        )

    def _stage_path(self, job_id: str) -> Path:
        if str(uuid.UUID(job_id)) != job_id:
            raise ValueError("Invalid import job identifier")
        return self.root.resolve(f"{STAGING}/{job_id}.part")

    def _cleanup_stage(self, job_id: str) -> None:
        try:
            self.root.check()
        except RootUnavailable:
            return
        path = self._stage_path(job_id)
        path.unlink(missing_ok=True)
        if path.parent.exists():
            sync_directory(path.parent)

    def _link(
        self, source: Path, stage: Path, cancelled: Callable[[], bool] | None, known_sha256: str | None = None
    ) -> str | None:
        """Hard-link a consumed source into staging (same device); None when linking is impossible."""
        before = live_stamp(source)
        if self.limits.max_archive_bytes is not None and before[2] > self.limits.max_archive_bytes:
            raise _too_large()
        try:
            os.link(source, stage, follow_symlinks=False)
        except OSError as error:
            if error.errno in LINK_UNSUPPORTED:
                return None
            raise
        digest = known_sha256 or hash_file(stage, cancelled)
        if live_stamp(source) != before or live_stamp(stage) != before:
            raise ContentChanged("Source changed during import; retry with a stable file")
        sync_file(stage)
        sync_directory(stage.parent)
        return digest

    def _copy(self, source: Path, stage: Path, cancelled: Callable[[], bool] | None) -> str:
        before = live_stamp(source)
        if self.limits.max_archive_bytes is not None and before[2] > self.limits.max_archive_bytes:
            raise _too_large()
        require_free_space(self.root.path, before[2])
        digest = hashlib.sha256()
        size = 0
        with source.open("rb") as incoming, stage.open("xb") as outgoing:
            while chunk := incoming.read(CHUNK_SIZE):
                check_cancel(cancelled)
                size += len(chunk)
                if self.limits.max_archive_bytes is not None and size > self.limits.max_archive_bytes:
                    raise _too_large()
                outgoing.write(chunk)
                digest.update(chunk)
            outgoing.flush()
            os.fsync(outgoing.fileno())
        if live_stamp(source) != before or size != before[2]:
            raise ContentChanged("Source changed during import; retry with a stable file")
        sync_directory(stage.parent)
        return digest.hexdigest()

    def _local_eze(self, source: Path) -> tuple[MetadataImport | None, list[str]]:
        path = eze_path(source)
        alternate = source.with_suffix(".eze.json")
        if path.exists() and alternate.exists():
            return None, ["multiple_eze_sidecars"]
        if not path.exists():
            path = alternate
            if path.exists():
                # Probe the few possible archive names instead of listing the directory; dedupe by
                # inode so case-insensitive filesystems do not count one file twice.
                siblings = set()
                for suffix in (".cbz", ".zip", ".pdf", ".CBZ", ".ZIP", ".PDF"):
                    with suppress(OSError):
                        info = source.with_suffix(suffix).stat()
                        siblings.add((info.st_dev, info.st_ino))
                if len(siblings) > 1:
                    return None, ["ambiguous_eze_sidecar"]
        if not path.exists() and not path.is_symlink():
            return None, []
        try:
            before = live_stamp(path)
            with path.open("rb") as stream:
                result = import_eze(stream.read(MAX_EZE_BYTES + 1))
            if live_stamp(path) != before:
                return None, ["eze_changed_during_read"]
            return result, list(result.warnings)
        except (ValueError, OSError, RecursionError):
            return None, ["eze_unreadable_or_invalid"]

    def _local_metadata(self, source: Path, sha256: str) -> tuple[MetadataImport | None, list[str]]:
        eze, warnings = self._local_eze(source)
        path = source.with_name(source.name + ".mangacouch.json")
        native, raw_hash = None, None
        try:
            before = live_stamp(path)
        except FileNotFoundError:
            before = None
        except (ValueError, OSError):
            before = None
            warnings.append("native_unreadable_or_invalid")
        if before is not None:
            try:
                with path.open("rb") as stream:
                    data = stream.read(MAX_METADATA_BYTES + 4097)
                if live_stamp(path) != before:
                    warnings.append("native_changed_during_read")
                else:
                    native = read_native(data, sha256)
                    raw_hash = hashlib.sha256(data).hexdigest()
            except (ValueError, OSError):
                warnings.append("native_unreadable_or_invalid")
        if native is None and eze is None and not warnings:
            return None, []
        return MetadataImport(
            metadata=native.metadata if native else eze.metadata if eze else Metadata(),
            candidates=eze.candidates if eze else {},
            conflicts=eze.conflicts if eze else (),
            ignored_fields=eze.ignored_fields if eze else (),
            warnings=tuple(warnings),
            native=native,
            native_raw_hash=raw_hash,
            eze_payload=eze.eze_payload if eze else None,
        ), warnings

    def import_file(
        self,
        source: Path,
        *,
        index_existing: bool = False,
        cancelled: Callable[[], bool] | None = None,
        original_filename: str | None = None,
        consume: bool = False,
        known_sha256: str | None = None,
        job_id: str | None = None,
        durable: bool = False,
    ) -> ImportResult:
        """Validate and publish one archive.

        consume=True declares that the caller owns the source and will delete it afterwards (an
        upload or download temporary file). On the same device it is hard-linked into staging
        instead of copied. known_sha256 lets such a caller skip re-hashing a linked source it just
        hashed; a copied source is still verified against it.

        Hashing, copying and validation run without the library lock, which other edits need;
        the job's own lock tells `recover` in another process that the import is still running.
        Publication takes the library lock and verifies the staged file again.
        """
        if original_filename is not None and (
            index_existing or not original_filename or len(original_filename) > 4096
        ):
            raise InvalidInput(
                "An original filename is only supported for copied imports and must be bounded"
            )
        cancelled = cancelled or thread_cancelled()
        source = source.absolute()
        extension = source.suffix.lower()
        if extension not in {".zip", ".cbz", ".pdf"}:
            raise InvalidInput("Expected ZIP, CBZ or PDF", code="unsupported_format")
        archive_format = "pdf" if extension == ".pdf" else "cbz"
        self.root.check(writable=True)
        check_cancel(cancelled)
        if index_existing:
            relative = source.relative_to(self.root.path).as_posix()
            if any(part.startswith(".mangacouch") for part in Path(relative).parts):
                raise InvalidInput("Internal library files cannot be indexed")
            source = self.root.resolve(relative)
        elif source.is_relative_to(self.root.path) and not source.is_relative_to(self.root.path / "import"):
            raise InvalidInput("Use index_existing for files already inside this library")
        before = live_stamp(source)
        if self.limits.max_archive_bytes is not None and before[2] > self.limits.max_archive_bytes:
            raise _too_large()
        staging = self.root.resolve(STAGING)
        staging.mkdir(mode=0o700, exist_ok=True)
        sync_directory(self.root.path)
        job_id = job_id or str(uuid.uuid4())
        stage = self._stage_path(job_id)
        lock_path = stage.with_suffix(".lock")
        try:
            with FileLock(lock_path, cancelled=cancelled):
                with self.root.mutation(cancelled=cancelled):
                    with self.library.connection() as db:
                        previous = db.execute("SELECT * FROM import_job WHERE id=?", (job_id,)).fetchone()
                    if previous is not None:
                        if previous["state"] == "done":
                            return ImportResult(
                                self.library.get_book(previous["book_id"]),
                                bool(previous["created"]),
                                job_id,
                                (),
                            )
                        self._abandon_import(job_id)
                        with self.library.transaction() as db:
                            db.execute("DELETE FROM import_job WHERE id=?", (job_id,))
                return self._import(
                    job_id,
                    source,
                    before,
                    archive_format,
                    index_existing,
                    original_filename,
                    consume,
                    known_sha256,
                    cancelled,
                    durable,
                )
        finally:
            with suppress(OSError):
                lock_path.unlink(missing_ok=True)

    def _import(
        self,
        job_id: str,
        source: Path,
        before: tuple[int, int, int, int],
        archive_format: Literal["cbz", "pdf"],
        index_existing: bool,
        original_filename: str | None,
        consume: bool,
        known_sha256: str | None,
        cancelled: Callable[[], bool] | None,
        durable: bool,
    ) -> ImportResult:
        with self.library.transaction() as db:
            db.execute(
                "INSERT INTO import_job(id,state,kind,durable) VALUES(?,'copying',?,?)",
                (job_id, "index" if index_existing else "copy", int(durable)),
            )
        try:
            stage = self._stage_path(job_id)
            if index_existing:
                digest = hash_file(source, cancelled)
            else:
                digest = (
                    self._link(source, stage, cancelled, known_sha256) if consume else None
                ) or self._copy(source, stage, cancelled)
                if known_sha256 is not None and digest != known_sha256:
                    raise ContentChanged("Source does not match the expected content hash")
            validated = source if index_existing else stage
            stamp_before = live_stamp(validated)
            pages = inspect_archive(validated, archive_format, self.limits, cancelled)
            if live_stamp(validated) != stamp_before or live_stamp(source) != before:
                raise ContentChanged("Source changed during archive validation")
            metadata, warnings = self._local_metadata(source, digest)
            content = Content(
                sha256=digest,
                format=archive_format,
                relative_path="pending",
                original_filename=source.name if original_filename is None else original_filename,
                byte_size=before[2],
                page_count=len(pages),
            )
            content.relative_path = (
                source.relative_to(self.root.path).as_posix() if index_existing else content.managed_path
            )
            check_cancel(cancelled)
            self.root.check(writable=True)
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE import_job SET state='ready',content_json=?,report_json=?,pages_json=?,"
                    "warnings_json=? WHERE id=?",
                    (
                        content.model_dump_json(),
                        _serialize_import(metadata),
                        canonical_json([page.model_dump() for page in pages]),
                        canonical_json(warnings),
                        job_id,
                    ),
                )
            with self.root.mutation(cancelled=cancelled):
                return self._publish(job_id, verified=stamp_before)
        except Exception as error:
            # A caller-visible failure must never publish later in maintenance. If the root or
            # database is unavailable, startup/maintenance repeats this cleanup, never publication.
            with suppress(OSError, ValueError, sqlite3.Error), self.root.mutation():
                self._abandon_import(job_id, reason=error_reason(error)[0])
            raise

    def abandon_import(self, job_id: str) -> str | None:
        """Cancel unpublished work; a committed book wins over cancellation."""
        self._stage_path(job_id)  # Validate the identifier before touching a record.
        with self.library.connection() as db:
            job = db.execute("SELECT state,book_id FROM import_job WHERE id=?", (job_id,)).fetchone()
        if job is None:
            return None
        if job["state"] == "done":
            return job["book_id"]
        stage = self._stage_path(job_id)
        with FileLock(stage.with_suffix(".lock"), cancelled=thread_cancelled()), self.root.mutation():
            return self._abandon_import(job_id)

    def _abandon_import(self, job_id: str, *, reason: str = "cancelled") -> str | None:
        with self.library.transaction() as db:
            job = db.execute("SELECT * FROM import_job WHERE id=?", (job_id,)).fetchone()
            if job is not None and job["state"] == "done":
                return job["book_id"]
            if job is not None and job["kind"] == "copy" and job["content_json"] and job["publication_json"]:
                # Only a file this import published itself is removed, recognised by the identity
                # recorded before its name appeared. Equal content proves nothing: the same bytes
                # may be a file the recycle bin keeps or one placed there by hand.
                content = Content.model_validate_json(job["content_json"])
                target = self.root.resolve(content.relative_path)
                claim = json.loads(job["publication_json"])
                owned = (
                    claim[1] != 0  # a filesystem without file ids cannot tell files apart
                    and target.is_file()
                    and not target.is_symlink()
                    and list(live_stamp(target)[:2]) == claim
                )
                if owned and not _path_kept(db, content.relative_path):
                    close_pdf(target)
                    target.unlink()
                    sync_directory(target.parent)
            db.execute(
                "UPDATE import_job SET state='failed',error_code=?,"
                "report_json=NULL,pages_json=NULL,content_json=NULL,publication_json=NULL "
                "WHERE id=?",
                (reason, job_id),
            )
        self._cleanup_stage(job_id)
        return None

    def _verified_stage(
        self, job_id: str, sha256: str, verified: tuple[int, int, int, int] | None
    ) -> tuple[int, int, int, int]:
        """The stamp of the staged file, hashed again unless it still has the `verified` stamp."""
        stage = self._stage_path(job_id)
        if verified is None or live_stamp(stage) != verified:
            if hash_file(stage) != sha256:
                raise ContentChanged("Staged content no longer matches the import record")
            verified = live_stamp(stage)
        return verified

    def _publish(self, job_id: str, *, verified: tuple[int, int, int, int] | None = None) -> ImportResult:
        """Publish a ready job; the caller holds the library lock. `verified` is the stamp of the
        staged (or indexed) file that the caller hashed and validated; when the file still has
        it, it is not hashed again."""
        self.root.check(writable=True)
        with self.library.connection() as db:
            job = db.execute("SELECT * FROM import_job WHERE id=?", (job_id,)).fetchone()
            if job is None or job["state"] != "ready":
                raise InvalidInput("Import is not ready for publication")
            content = Content.model_validate_json(job["content_json"])
            existing = db.execute(
                "SELECT id,content_json FROM book WHERE sha256=?", (content.sha256,)
            ).fetchone()
        warnings = json.loads(job["warnings_json"])
        target = self.root.resolve(content.relative_path)
        target_verified = False
        if existing is not None:
            old = Content.model_validate_json(existing["content_json"])
            old_path = self.root.resolve(old.relative_path)
            try:
                with self.library.connection() as db:
                    manifest = db.execute(
                        "SELECT file_stamp FROM archive_manifest WHERE book_id=?", (existing["id"],)
                    ).fetchone()
                old_digest = (
                    content.sha256
                    if manifest and canonical_json(file_stamp(old_path)) == manifest[0]
                    else hash_file(old_path)
                )
            except FileNotFoundError:
                old_digest = None
            if old_digest is not None and old_digest != content.sha256:
                if job["kind"] != "copy":
                    raise ContentChanged(
                        "The existing library file has changed; resolve it before indexing a duplicate"
                    )
                # Importing the original again repairs the book; the edited file is kept beside it.
                verified = self._verified_stage(job_id, content.sha256, verified)
                close_pdf(old_path)
                aside = set_aside(old_path)
                warnings.append("changed_file_set_aside")
                LOGGER.warning(
                    "Book %s had changed on disk; kept the changed file as %r and restored the original",
                    short_id(existing["id"]),
                    aside.name,
                )
                content.relative_path = old.relative_path
                target = old_path
            elif old_digest == content.sha256:
                if job["kind"] == "index" and target != old_path:
                    warnings.append("duplicate_library_file_retained")
                content.relative_path = old.relative_path
                target = old_path
                target_verified = True
        if (
            existing is None
            and job["kind"] == "copy"
            and target.exists()
            and hash_file(target) != content.sha256
        ):
            with self.library.connection() as db:
                if db.execute(
                    "SELECT 1 FROM book WHERE json_extract(content_json,'$.relative_path')=?",
                    (content.relative_path,),
                ).fetchone():
                    raise ContentChanged("Target is referenced by another book")
            self._verified_stage(job_id, content.sha256, verified)
            close_pdf(target)
            set_aside(target)
            warnings.append("changed_file_set_aside")
        if job["kind"] == "copy" and not target.exists():
            stage = self._stage_path(job_id)
            verified = self._verified_stage(job_id, content.sha256, verified)
            target.parent.mkdir(exist_ok=True)
            sync_directory(self.root.path)
            self.root.resolve(content.relative_path)
            self.root.check(writable=True)

            def claimed(path: Path) -> None:
                with self.library.transaction() as db:
                    db.execute(
                        "UPDATE import_job SET publication_json=? WHERE id=?",
                        (canonical_json(live_stamp(path)[:2]), job_id),
                    )

            claimed(stage)  # A rename or link gives the target the staged file's identity.
            with suppress(FileExistsError):
                try:
                    _rename_new(stage, target, claimed=claimed)
                except OSError as error:
                    if error.errno not in LINK_UNSUPPORTED:
                        raise
                    publish_file(stage, target, claimed=claimed)
            sync_directory(target.parent)
        if (
            not target_verified
            and (verified is None or live_stamp(target) != verified)
            and hash_file(target) != content.sha256
        ):
            raise ContentChanged("Target does not match verified content; no file was overwritten")
        self.root.check(writable=True)
        result = self._commit_import(
            job_id, content, job["pages_json"], job["report_json"], warnings, durable=bool(job["durable"])
        )
        try:
            self._cleanup_stage(job_id)
        except OSError:
            LOGGER.warning("Import %s committed; staging cleanup pending", job_id, exc_info=True)
            result = replace(result, warnings=(*result.warnings, "staging_cleanup_pending"))
        return result

    def _commit_import(
        self,
        job_id: str,
        content: Content,
        pages_json: str,
        report_json: str | None,
        warnings: list[str],
        *,
        durable: bool = False,
    ) -> ImportResult:
        source_import = _deserialize_import(report_json)
        metadata = source_import.metadata if source_import else Metadata()
        target_stamp = canonical_json(file_stamp(self.root.resolve(content.relative_path)))
        with self.library.transaction(durable=durable) as db:
            book_id, created = self.library.register_record(db, content, metadata, source_import)
            if not created:
                old = Content.model_validate_json(
                    db.execute("SELECT content_json FROM book WHERE id=?", (book_id,)).fetchone()[0]
                )
                if old.relative_path != content.relative_path:
                    old.relative_path = content.relative_path
                    db.execute("UPDATE book SET content_json=? WHERE id=?", (old.model_dump_json(), book_id))
                    # Nothing has been written next to the new location yet.
                    resync_sidecars(db, book_id)
                if source_import is not None:
                    warnings.append("existing_book_metadata_preserved")
            elif source_import is not None and source_import.native is not None:
                kind = db.execute("SELECT kind FROM import_job WHERE id=?", (job_id,)).fetchone()[0]
                if kind == "index":
                    native = source_import.native
                    db.execute(
                        "UPDATE sidecar_sync SET baseline_hash=?,observed_hash=? WHERE book_id=?",
                        (
                            native.semantic_hash(),
                            source_import.native_raw_hash,
                            book_id,
                        ),
                    )
            db.execute("UPDATE book SET availability='available' WHERE id=?", (book_id,))
            previous = db.execute(
                "SELECT pages_json FROM archive_manifest WHERE book_id=?", (book_id,)
            ).fetchone()
            if previous is not None:
                old_pages, new_pages = json.loads(previous[0]), json.loads(pages_json)
                changed = [
                    index
                    for index, page in enumerate(new_pages)
                    if index >= len(old_pages)
                    or old_pages[index]["name"] != page["name"]
                    or old_pages[index]["orientation"] != page["orientation"]
                ]
                if changed:
                    selected = canonical_json(changed)
                    db.execute(
                        "DELETE FROM page_feature WHERE book_id=? AND page IN "
                        "(SELECT value FROM json_each(?))",
                        (book_id, selected),
                    )
                    db.execute(
                        "DELETE FROM duplicate_edge WHERE "
                        "(left_id=? AND left_page IN (SELECT value FROM json_each(?))) OR "
                        "(right_id=? AND right_page IN (SELECT value FROM json_each(?)))",
                        (book_id, selected, book_id, selected),
                    )
            db.execute(
                "INSERT INTO archive_manifest(book_id,pages_json,file_stamp) VALUES(?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET pages_json=excluded.pages_json,"
                "file_stamp=excluded.file_stamp",
                (book_id, pages_json, target_stamp),
            )
            # Finished jobs keep only their small identity row; the manifest and import report
            # already live in archive_manifest and metadata_import.
            db.execute(
                "UPDATE import_job SET state='done',created=?,book_id=?,warnings_json=?,error_code=NULL,"
                "report_json=NULL,pages_json=NULL WHERE id=?",
                (int(created), book_id, canonical_json(warnings), job_id),
            )
            if created and source_import is not None and source_import.eze_payload is not None:
                db.execute(
                    "INSERT INTO eze_cache(book_id,payload_json) VALUES(?,?)",
                    (book_id, canonical_json(source_import.eze_payload)),
                )
            book = self.library.get_book(book_id, db)
        LOGGER.info(
            "%s %s as book %s (%d pages)%s",
            "Imported" if created else "Already in library:",
            repr(book.content.original_filename),
            short_id(book_id),
            book.content.page_count,
            f"; warnings: {', '.join(warnings)}" if warnings else "",
        )
        return ImportResult(book, created, job_id, tuple(warnings))

    def purge_imports(self) -> int:
        with self.root.mutation():
            with self.library.connection() as db:
                copying = db.execute("SELECT id FROM import_job WHERE state='copying'").fetchall()
            for row in copying:
                if not self._running(self._stage_path(row[0]).with_suffix(".lock")):
                    with self.library.transaction() as db:
                        db.execute(
                            "UPDATE import_job SET state='failed',error_code='interrupted' "
                            "WHERE id=? AND state='copying'",
                            (row[0],),
                        )
            self._clear_staging()
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM import_job WHERE state IN ('done','failed') "
                "AND created_at < unixepoch() - 604800"
            ).rowcount

    def recover(self) -> dict[str, str]:
        """Fail interrupted imports and remove unpublished files; never publish in the background."""
        results = {}
        with self.library.connection() as db:
            jobs = db.execute(
                "SELECT id FROM import_job WHERE state IN ('copying','ready') "
                "OR (state='failed' AND content_json IS NOT NULL)"
            ).fetchall()
        for (job_id,) in jobs:
            stage = self._stage_path(job_id)
            try:
                with FileLock(stage.with_suffix(".lock"), blocking=False), self.root.mutation():
                    self._abandon_import(job_id)
                results[job_id] = "interrupted; retry source import"
            except BlockingIOError:
                continue
        with self.root.mutation():
            self._clear_staging()
            self._clear_temporaries()
        return results

    def recover_ready(self, settle: int = 600) -> dict[str, str]:
        """Clean up inactive interrupted imports after the caller has had time to finish."""
        with self.library.connection() as db:
            if not db.execute(
                "SELECT 1 FROM import_job WHERE (state IN ('copying','ready') "
                "OR (state='failed' AND content_json IS NOT NULL)) "
                "AND created_at<unixepoch()-? LIMIT 1",
                (settle,),
            ).fetchone():
                return {}
        return self.recover()

    def _clear_temporaries(self) -> None:
        """Delete half-written sidecars and publication copies a crash left next to archives.

        Their writers hold the library lock, which the caller holds now, so none is in use."""
        with self.library.connection() as db:
            directories = {
                PurePosixPath(row[0]).parent.as_posix()
                for row in db.execute("SELECT json_extract(content_json,'$.relative_path') FROM book")
            }
        for directory in sorted(directories):
            with suppress(OSError, ValueError):
                path = self.root.path if directory == "." else self.root.resolve(directory)
                leftovers = [
                    entry
                    for entry in path.iterdir()
                    if LEFTOVER.fullmatch(entry.name) and not entry.is_symlink() and entry.is_file()
                ]
                for entry in leftovers:
                    entry.unlink(missing_ok=True)
                if leftovers:
                    sync_directory(path)

    def _clear_staging(self) -> None:
        staging = self.root.path / STAGING
        if not staging.is_dir():
            return
        with self.library.connection() as db:
            ready = {row[0] for row in db.execute("SELECT id FROM import_job WHERE state='ready'")}
        removed = False
        for entry in staging.iterdir():
            if entry.suffix == ".lock":
                self._running(entry)  # removes a lock that no import holds
            elif (
                entry.suffix == ".part"
                and entry.stem not in ready
                and not self._running(entry.with_suffix(".lock"))
            ):
                entry.unlink(missing_ok=True)
                removed = True
        if removed:
            sync_directory(staging)

    @staticmethod
    def _running(lock_path: Path) -> bool:
        """Whether an import, in this or another process, holds the job lock at `lock_path`."""
        if not lock_path.exists():
            return False
        try:
            FileLock(lock_path, blocking=False).close()
        except BlockingIOError:
            return True
        lock_path.unlink(missing_ok=True)
        return False

    def availability(self, book: Book, *, manifest_stamp: str | None = None) -> tuple[str, str | None]:
        with self.availability_locks[hash(book.id) % len(self.availability_locks)]:
            return self._availability(book, manifest_stamp)

    def _availability(self, book: Book, manifest_stamp: str | None = None) -> tuple[str, str | None]:
        try:
            path = self.root.resolve(book.content.relative_path)
            stamp = canonical_json(file_stamp(path))
            if manifest_stamp is None:
                with self.library.connection() as db:
                    row = db.execute(
                        "SELECT file_stamp FROM archive_manifest WHERE book_id=?", (book.id,)
                    ).fetchone()
                manifest_stamp = row[0] if row else None
            manifest = {"file_stamp": manifest_stamp} if manifest_stamp is not None else None
            if (manifest is None or manifest["file_stamp"] != stamp) and hash_file(
                path
            ) != book.content.sha256:
                return "changed", None
            if manifest is not None and manifest["file_stamp"] != stamp:
                # Best effort: a busy writer only means the next call rehashes once more.
                with suppress(sqlite3.OperationalError), self.library.transaction() as db:
                    db.execute(
                        "UPDATE archive_manifest SET file_stamp=? WHERE book_id=? AND file_stamp=?",
                        (stamp, book.id, manifest["file_stamp"]),
                    )
            self.availability_failures.pop(book.id, None)
            return "available", stamp
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unreadable", None

    def observe_unavailable(self, book: Book, state: str) -> None:
        self.root.check()
        with self.library.connection() as db:
            row = db.execute(
                "SELECT b.availability,m.file_stamp FROM book b LEFT JOIN archive_manifest m "
                "ON m.book_id=b.id WHERE b.id=?",
                (book.id,),
            ).fetchone()
        if state == "available" or row is None or row[0] != "available":
            self.availability_failures.pop(book.id, None)
            return
        stamp = row[1]
        previous = self.availability_failures.get(book.id)
        self.availability_failures[book.id] = (state, stamp)
        if previous != (state, stamp):
            return
        with self.library.transaction() as db:
            db.execute(
                "UPDATE book SET availability=? WHERE id=? AND availability='available' "
                "AND (SELECT file_stamp FROM archive_manifest WHERE book_id=book.id) IS ?",
                (state, book.id, stamp),
            )
        self.availability_failures.pop(book.id, None)

    def recheck_unavailable(self, limit: int = 200) -> int:
        self.root.check()
        with self.library.connection() as db:
            pending = list(self.availability_failures)
            for start in range(0, len(pending), 200):
                batch = pending[start : start + 200]
                existing = {
                    row[0]
                    for row in db.execute(
                        "SELECT id FROM book WHERE availability='available' "
                        "AND id IN (SELECT value FROM json_each(?))",
                        (json.dumps(batch),),
                    )
                }
                for identifier in batch:
                    if identifier not in existing:
                        self.availability_failures.pop(identifier, None)
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM book WHERE availability!='available' AND id>? ORDER BY id LIMIT ?",
                    (self.unavailable_after, limit),
                )
            ]
            if not ids:
                self.unavailable_after = ""
                return 0
            books = self.library.get_books(ids, db)
        restored = 0
        for book in books:
            state, stamp = self.availability(book)
            self.unavailable_after = book.id
            if state != "available":
                continue
            with self.root.mutation():
                if canonical_json(file_stamp(self.root.resolve(book.content.relative_path))) != stamp:
                    continue
                with self.library.transaction() as db:
                    db.execute("UPDATE book SET availability='available' WHERE id=?", (book.id,))
                    resync_sidecars(db, book.id)
                self.availability_failures.pop(book.id, None)
                restored += 1
        return restored

    def refresh_availability(self) -> dict[str, str]:
        results: dict[str, str] = {}
        after = ""
        while True:
            with self.library.connection() as db:
                ids = [
                    row[0]
                    for row in db.execute("SELECT id FROM book WHERE id>? ORDER BY id LIMIT 500", (after,))
                ]
                books = self.library.get_books(ids, db)
            if not books:
                return results
            observed = [(book, *self.availability(book)) for book in books]
            with self.root.mutation():
                self.root.check(writable=True)
                with self.library.transaction() as db:
                    for book, state, stamp in observed:
                        if (
                            stamp is not None
                            and canonical_json(file_stamp(self.root.resolve(book.content.relative_path)))
                            != stamp
                        ):
                            continue
                        db.execute("UPDATE book SET availability=? WHERE id=?", (state, book.id))
                        if state == "available" and book.availability != "available":
                            resync_sidecars(db, book.id)
                        results[book.id] = state
            after = ids[-1]

    @staticmethod
    def _page_book(book: Book, index: int) -> None:
        if type(index) is not int or not 0 <= index < book.content.page_count:
            raise InvalidInput("Page index outside this book", code="page_out_of_range")

    def _page_context(
        self, book_id: str, index: int, book: Book | None = None
    ) -> tuple[Book, Page, Path, str]:
        self.root.check()
        with self.library.connection() as db:
            book = book or self.library.get_book(book_id, db)
            self._page_book(book, index)
            row = db.execute("SELECT file_stamp FROM archive_manifest WHERE book_id=?", (book_id,)).fetchone()
            pages = None
            if row is not None:
                key = (book_id, row[0])
                with self.manifest_lock:
                    pages = self.manifests.get(key)
                    if pages is None:
                        encoded = db.execute(
                            "SELECT pages_json FROM archive_manifest WHERE book_id=?", (book_id,)
                        ).fetchone()[0]
                        pages = json.loads(encoded)
                        if len(encoded) <= 8 * 1024**2:
                            self.manifests[key] = pages
                            while len(self.manifests) > 4:
                                self.manifests.popitem(last=False)
                    elif key in self.manifests:
                        self.manifests.move_to_end(key)
        state, stamp = self.availability(book, manifest_stamp=row[0] if row else None)
        if state != "available" or stamp is None:
            self._unavailable(book, state)
        if pages is None or index >= len(pages):
            raise InvalidInput("Archive has not been validated by the file importer", code="manifest_missing")
        page = Page.model_validate(pages[index])
        path = self.root.resolve(book.content.relative_path)
        self._check_page_stamp(path, stamp)
        return book, page, path, stamp

    def _check_page_stamp(self, path: Path, stamp: str) -> None:
        self.root.check()
        if canonical_json(file_stamp(path)) != stamp:
            raise ContentChanged("Archive changed while reading a page")

    def _image_key(self, sha256: str, index: int, kind: str) -> str:
        return hashlib.sha256(
            canonical_json(
                {
                    "sha256": sha256,
                    "page": index,
                    "kind": kind,
                    "size": [THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT],
                    "version": IMAGE_VERSION,
                    # Only the limits that shape a rendered image; the others merely reject input,
                    # so changing say --max-bytes keeps every cached thumbnail.
                    "render": [self.limits.max_dimension, self.limits.max_render_pixels],
                }
            ).encode()
        ).hexdigest()

    def page_bytes(self, book_id: str, index: int, *, book: Book | None = None) -> bytes:
        book, page, path, stamp = self._page_context(book_id, index, book)

        def render() -> bytes:
            self._check_page_stamp(path, stamp)
            data = read_page(path, book.content.format, page, self.limits)
            self._check_page_stamp(path, stamp)
            return data

        data = (
            self.page_cache.get_or_create(
                self._image_key(book.content.sha256, index, "page:webp:lossless"), render
            )
            if book.content.format == "pdf"
            else render()
        )
        self._check_page_stamp(path, stamp)
        return data

    def cover_key(self, book: Book, index: int = 0, *, format: ThumbnailFormat = "webp") -> str:
        """The thumbnail cache key of one page of a book, as cover_bytes stores it."""
        return self._image_key(
            book.content.sha256, index, f"thumbnail:{format}:q80:effort3:{book.thumbnail_revision}"
        )

    def cached_cover(
        self, book: Book, cache: ImageCache, index: int = 0, *, format: ThumbnailFormat = "webp"
    ) -> bytes | None:
        self._page_book(book, index)
        cached = cache.get(self.cover_key(book, index, format=format))
        if cached is not None:
            self.root.check()
            state, _ = self.availability(book)
            if state != "available":
                self._unavailable(book, state)
        return cached

    def _unavailable(self, book: Book, state: str) -> NoReturn:
        """Record what reading a book found instead of its archive, then refuse the read."""
        self.observe_unavailable(book, state)
        raise ContentChanged(f"Book is {state}; personal state is retained", observed=True)

    def cover_bytes(
        self,
        book_id: str,
        cache: ImageCache,
        index: int = 0,
        *,
        format: ThumbnailFormat = "webp",
        book: Book | None = None,
    ) -> bytes:
        book = book or self.library.get_book(book_id)
        self._page_book(book, index)
        key = self.cover_key(book, index, format=format)
        cached = self.cached_cover(book, cache, index, format=format)
        if cached is not None:
            return cached
        book, page, path, stamp = self._page_context(book_id, index, book)

        def render() -> bytes:
            self._check_page_stamp(path, stamp)
            result = read_page(path, book.content.format, page, self.limits, thumbnail=format)
            self._check_page_stamp(path, stamp)
            return result

        data = cache.get_or_create(key, render, cover=index == 0)
        self._check_page_stamp(path, stamp)
        return data


def image_bytes(db: sqlite3.Connection, book_id: str) -> int | None:
    """Sum of page image sizes for a ZIP/CBZ book (None for PDFs or without a manifest)."""
    row = db.execute(
        "SELECT sum(json_extract(p.value,'$.byte_size')) FROM archive_manifest m "
        "JOIN book b ON b.id=m.book_id, json_each(m.pages_json) p "
        "WHERE m.book_id=? AND json_extract(b.content_json,'$.format') ='cbz'",
        (book_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])
