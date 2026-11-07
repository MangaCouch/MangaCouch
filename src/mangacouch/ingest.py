from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path

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
    inspect_archive,
    read_page,
)
from .database import Book, Library
from .errors import InvalidInput
from .eze import MAX_EZE_BYTES, eze_path, import_eze
from .files import (
    CHUNK_SIZE,
    STAGING,
    ContentChanged,
    LibraryRoot,
    RootUnavailable,
    file_stamp,
    hash_file,
    publish_file,
    require_free_space,
    sync_directory,
)
from .image_cache import ImageCache
from .metadata_import import MetadataImport
from .models import MAX_METADATA_BYTES, Content, Metadata, NativeSidecar, canonical_json, read_native
from .source_checks import enqueue_check

TRANSIENT_ERRNOS = {
    errno.ENOSPC,
    errno.EDQUOT,
    errno.EIO,
    errno.EAGAIN,
    errno.EBUSY,
    errno.ETIMEDOUT,
    getattr(errno, "ESTALE", errno.EIO),
}


def transient_import_error(error: BaseException) -> bool:
    """Errors that a later retry of the same staged import can overcome."""
    if isinstance(error, (RootUnavailable, sqlite3.Error)):
        return True
    return (
        isinstance(error, OSError)
        and not isinstance(error, (ContentChanged, FileNotFoundError))
        and error.errno in TRANSIENT_ERRNOS
    )


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
        before = file_stamp(source)
        if self.limits.max_archive_bytes is not None and before[2] > self.limits.max_archive_bytes:
            raise _too_large()
        try:
            os.link(source, stage, follow_symlinks=False)
        except OSError as error:
            if error.errno in {
                errno.EXDEV,
                errno.EPERM,
                errno.EACCES,
                errno.ENOSYS,
                errno.ENOTSUP,
                errno.EMLINK,
            }:
                return None
            raise
        digest = known_sha256 or hash_file(stage, cancelled)
        if file_stamp(source) != before or file_stamp(stage) != before:
            raise ContentChanged("Source changed during import; retry with a stable file")
        sync_directory(stage.parent)
        return digest

    def _copy(self, source: Path, stage: Path, cancelled: Callable[[], bool] | None) -> str:
        before = file_stamp(source)
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
        if file_stamp(source) != before or size != before[2]:
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
            before = file_stamp(path)
            with path.open("rb") as stream:
                result = import_eze(stream.read(MAX_EZE_BYTES + 1))
            if file_stamp(path) != before:
                return None, ["eze_changed_during_read"]
            return result, list(result.warnings)
        except (ValueError, OSError, RecursionError):
            return None, ["eze_unreadable_or_invalid"]

    def _local_metadata(self, source: Path, sha256: str) -> tuple[MetadataImport | None, list[str]]:
        eze, warnings = self._local_eze(source)
        path = source.with_name(source.name + ".mangacouch.json")
        native, raw_hash = None, None
        try:
            before = file_stamp(path)
        except FileNotFoundError:
            before = None
        except (ValueError, OSError):
            before = None
            warnings.append("native_unreadable_or_invalid")
        if before is not None:
            try:
                with path.open("rb") as stream:
                    data = stream.read(MAX_METADATA_BYTES + 4097)
                if file_stamp(path) != before:
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
        move: bool = False,
        known_sha256: str | None = None,
    ) -> ImportResult:
        """Validate and publish one archive.

        consume=True (alias move=True) declares that the caller owns the source and will
        delete it afterwards (an upload or download temporary file). On the same device it is
        hard-linked into staging instead of copied.
        known_sha256 lets such a caller skip re-hashing a linked source it just hashed;
        a copied source is still verified against it.
        """
        consume = consume or move
        if original_filename is not None and (
            index_existing or not original_filename or len(original_filename) > 4096
        ):
            raise InvalidInput(
                "An original filename is only supported for copied imports and must be bounded"
            )
        source = source.absolute()
        extension = source.suffix.lower()
        if extension not in {".zip", ".cbz", ".pdf"}:
            raise InvalidInput("Expected ZIP, CBZ or PDF", code="unsupported_format")
        archive_format = "pdf" if extension == ".pdf" else "cbz"
        with self.root.mutation():
            check_cancel(cancelled)
            if index_existing:
                relative = source.relative_to(self.root.path).as_posix()
                if any(part.startswith(".mangacouch") for part in Path(relative).parts):
                    raise InvalidInput("Internal library files cannot be indexed")
                source = self.root.resolve(relative)
            elif source.is_relative_to(self.root.path) and not source.is_relative_to(
                self.root.path / "import"
            ):
                raise InvalidInput("Use index_existing for files already inside this library")
            before = file_stamp(source)
            if self.limits.max_archive_bytes is not None and before[2] > self.limits.max_archive_bytes:
                raise _too_large()
            staging = self.root.resolve(STAGING)
            staging.mkdir(mode=0o700, exist_ok=True)
            sync_directory(self.root.path)
            job_id = str(uuid.uuid4())
            with self.library.transaction() as db:
                db.execute(
                    "INSERT INTO import_job(id,state,kind) VALUES(?,'copying',?)",
                    (job_id, "index" if index_existing else "copy"),
                )
            ready = False
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
                stamp_before = file_stamp(validated)
                pages = inspect_archive(validated, archive_format, self.limits, cancelled)
                if file_stamp(validated) != stamp_before or file_stamp(source) != before:
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
                ready = True
                return self._publish(job_id, verified=stamp_before)
            except Exception as error:
                keep = ready and transient_import_error(error)
                with suppress(sqlite3.Error), self.library.transaction() as db:
                    db.execute(
                        "UPDATE import_job SET state=CASE WHEN state='done' THEN state ELSE ? END,"
                        "error_code=CASE WHEN state='done' THEN error_code ELSE ? END,"
                        "report_json=CASE WHEN ? THEN report_json END,"
                        "pages_json=CASE WHEN ? THEN pages_json END WHERE id=?",
                        ("ready" if keep else "failed", error_reason(error)[0], keep, keep, job_id),
                    )
                if not keep:
                    with suppress(OSError):
                        self._cleanup_stage(job_id)
                raise

    def _publish(self, job_id: str, *, verified: tuple[int, int, int, int] | None = None) -> ImportResult:
        """Publish a ready job. `verified` is the stamp of the staged (or indexed) file that the
        caller hashed and validated under the current mutation lock; when it still matches, the
        file is not hashed again."""
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
                old_digest = hash_file(old_path)
            except FileNotFoundError:
                old_digest = None
            if old_digest is not None and old_digest != content.sha256:
                raise ContentChanged(
                    "The existing library file has changed; resolve it before importing a duplicate"
                )
            if old_digest == content.sha256:
                if job["kind"] == "index" and target != old_path:
                    warnings.append("duplicate_library_file_retained")
                content.relative_path = old.relative_path
                target = old_path
                target_verified = True
        if job["kind"] == "copy" and not target.exists():
            stage = self._stage_path(job_id)
            if verified is None or file_stamp(stage) != verified:
                if hash_file(stage) != content.sha256:
                    raise ContentChanged("Staged content no longer matches the import record")
                verified = file_stamp(stage)
            target.parent.mkdir(exist_ok=True)
            sync_directory(self.root.path)
            self.root.resolve(content.relative_path)
            self.root.check(writable=True)
            with suppress(FileExistsError):
                publish_file(stage, target)
            sync_directory(target.parent)
        if (
            not target_verified
            and (verified is None or file_stamp(target) != verified)
            and hash_file(target) != content.sha256
        ):
            raise ContentChanged("Target does not match verified content; no file was overwritten")
        self.root.check(writable=True)
        result = self._commit_import(job_id, content, job["pages_json"], job["report_json"], warnings)
        self._cleanup_stage(job_id)
        return result

    def _commit_import(
        self,
        job_id: str,
        content: Content,
        pages_json: str,
        report_json: str | None,
        warnings: list[str],
    ) -> ImportResult:
        source_import = _deserialize_import(report_json)
        metadata = source_import.metadata if source_import else Metadata()
        target_stamp = canonical_json(file_stamp(self.root.resolve(content.relative_path)))
        with self.library.transaction() as db:
            book_id, created = self.library.register_record(db, content, metadata, source_import)
            if not created:
                old = Content.model_validate_json(
                    db.execute("SELECT content_json FROM book WHERE id=?", (book_id,)).fetchone()[0]
                )
                old.relative_path = content.relative_path
                db.execute("UPDATE book SET content_json=? WHERE id=?", (old.model_dump_json(), book_id))
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
            db.execute(
                "INSERT INTO archive_manifest(book_id,pages_json,file_stamp) VALUES(?,?,?) "
                "ON CONFLICT(book_id) DO UPDATE SET pages_json=excluded.pages_json,"
                "file_stamp=excluded.file_stamp",
                (book_id, pages_json, target_stamp),
            )
            # Finished jobs keep only their small identity row; the manifest and import report
            # already live in archive_manifest and metadata_import.
            db.execute(
                "UPDATE import_job SET state='done',book_id=?,warnings_json=?,error_code=NULL,"
                "report_json=NULL,pages_json=NULL WHERE id=?",
                (book_id, canonical_json(warnings), job_id),
            )
            enqueue_check(db, book_id)
            if created and source_import is not None and source_import.eze_payload is not None:
                db.execute(
                    "INSERT INTO eze_cache(book_id,payload_json) VALUES(?,?)",
                    (book_id, canonical_json(source_import.eze_payload)),
                )
        return ImportResult(self.library.get_book(book_id), created, job_id, tuple(warnings))

    def purge_imports(self) -> int:
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM import_job WHERE state IN ('done','failed') "
                "AND created_at < unixepoch() - 604800"
            ).rowcount

    def recover(self) -> dict[str, str]:
        """Finish interrupted imports: publish ready jobs, fail copying ones, clear stale stages."""
        results = {}
        with self.root.mutation():
            with self.library.connection() as db:
                jobs = db.execute(
                    "SELECT id,state FROM import_job WHERE state IN ('copying','ready') ORDER BY rowid"
                ).fetchall()
            for job in jobs:
                job_id = job["id"]
                if job["state"] == "copying":
                    with self.library.transaction() as db:
                        db.execute(
                            "UPDATE import_job SET state='failed',error_code='interrupted' WHERE id=?",
                            (job_id,),
                        )
                    results[job_id] = "interrupted; retry source import"
                    continue
                try:
                    self._publish(job_id)
                    results[job_id] = "recovered"
                except (OSError, ValueError, sqlite3.Error) as error:
                    keep = transient_import_error(error)
                    reason = error_reason(error)[0]
                    with self.library.transaction() as db:
                        db.execute(
                            "UPDATE import_job SET state=?,error_code=? WHERE id=? AND state='ready'",
                            ("ready" if keep else "failed", reason, job_id),
                        )
                    results[job_id] = reason
            self._clear_staging()
        return results

    def _clear_staging(self) -> None:
        staging = self.root.path / STAGING
        if not staging.is_dir():
            return
        with self.library.connection() as db:
            ready = {row[0] for row in db.execute("SELECT id FROM import_job WHERE state='ready'")}
        removed = False
        for entry in staging.iterdir():
            if entry.name.endswith(".part") and entry.name.removesuffix(".part") not in ready:
                entry.unlink(missing_ok=True)
                removed = True
        if removed:
            sync_directory(staging)

    def availability(self, book: Book) -> tuple[str, str | None]:
        try:
            path = self.root.resolve(book.content.relative_path)
            stamp = canonical_json(file_stamp(path))
            with self.library.connection() as db:
                manifest = db.execute(
                    "SELECT file_stamp FROM archive_manifest WHERE book_id=?", (book.id,)
                ).fetchone()
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
            return "available", stamp
        except FileNotFoundError:
            return "missing", None
        except OSError:
            return "unreadable", None

    def refresh_availability(self) -> dict[str, str]:
        with self.root.mutation():
            with self.library.connection() as db:
                ids = [row[0] for row in db.execute("SELECT id FROM book")]
            observed = {book_id: self.availability(self.library.get_book(book_id)) for book_id in ids}
            self.root.check(writable=True)
            with self.library.transaction() as db:
                for book_id, (state, stamp) in observed.items():
                    db.execute("UPDATE book SET availability=? WHERE id=?", (state, book_id))
                    if stamp is not None:
                        db.execute(
                            "UPDATE archive_manifest SET file_stamp=? WHERE book_id=?", (stamp, book_id)
                        )
        return {book_id: state for book_id, (state, _) in observed.items()}

    @staticmethod
    def _page_book(book: Book, index: int) -> None:
        if type(index) is not int or not 0 <= index < book.content.page_count:
            raise InvalidInput("Page index outside this book", code="page_out_of_range")

    def _page_context(self, book_id: str, index: int) -> tuple[Book, Page, Path, str]:
        self.root.check()
        book = self.library.get_book(book_id)
        self._page_book(book, index)
        state, stamp = self.availability(book)
        if state != "available" or stamp is None:
            raise ContentChanged(f"Book is {state}; personal state is retained")
        with self.library.connection() as db:
            row = db.execute(
                "SELECT json_extract(pages_json,?) FROM archive_manifest WHERE book_id=?",
                (f"$[{index}]", book_id),
            ).fetchone()
        if row is None or row[0] is None:
            raise InvalidInput("Archive has not been validated by the file importer", code="manifest_missing")
        page = Page.model_validate_json(row[0])
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
                    "limits": asdict(self.limits),
                }
            ).encode()
        ).hexdigest()

    def page_bytes(self, book_id: str, index: int) -> bytes:
        book, page, path, stamp = self._page_context(book_id, index)

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

    def image_byte_size(self, book_id: str) -> int:
        """Total uncompressed size of the page images, from the manifest recorded at import."""
        with self.library.connection() as db:
            total = image_bytes(db, book_id)
        if total is None:
            raise InvalidInput("Image byte totals require a validated ZIP archive")
        return total

    def cover_bytes(
        self, book_id: str, cache: ImageCache, index: int = 0, *, format: ThumbnailFormat = "webp"
    ) -> bytes:
        book = self.library.get_book(book_id)
        self._page_book(book, index)
        key = self._image_key(book.content.sha256, index, f"thumbnail:{format}:q80:effort3")
        cached = cache.get(key)
        if cached is not None:
            # A cached cover still requires the registered archive to be present and unchanged.
            self.root.check()
            state, _ = self.availability(book)
            if state != "available":
                raise ContentChanged(f"Book is {state}; personal state is retained")
            return cached
        book, page, path, stamp = self._page_context(book_id, index)

        def render() -> bytes:
            self._check_page_stamp(path, stamp)
            result = read_page(path, book.content.format, page, self.limits, thumbnail=format)
            self._check_page_stamp(path, stamp)
            return result

        data = cache.get_or_create(key, render)
        self._check_page_stamp(path, stamp)
        return data


def image_bytes(db: sqlite3.Connection, book_id: str) -> int | None:
    """Sum of page image sizes for a ZIP/CBZ book (None for PDFs or without a manifest)."""
    row = db.execute(
        "SELECT sum(json_extract(p.value,'$.byte_size')) FROM archive_manifest m "
        "JOIN book b ON b.id=m.book_id, json_each(m.pages_json) p "
        "WHERE m.book_id=? AND json_extract(b.content_json,'$.format') IN ('zip','cbz')",
        (book_id,),
    ).fetchone()
    return None if row is None or row[0] is None else int(row[0])
