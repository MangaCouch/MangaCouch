"""The ingest pipeline.

CPU-bound work (hashing, page enumeration, cover thumbnailing, cover pHash) is isolated in the
module-level, picklable :func:`compute_ingest_payload` so it can fan out across a
``ProcessPoolExecutor`` (§3). All database / thumbnail-store / FTS writes are serialised in the
main thread by :class:`Ingestor`. The manga folder is the source of truth; this rebuilds the index.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Executor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import hashing, sidecars
from ..core.archives import detect_format, is_supported, open_archive
from ..core.thumbnails import COVER_PAGE, VARIANT_COVER, ThumbStore
from ..db.base import session_scope
from ..db.models import Archive, ArchiveTag, Tag
from ..search.index import SearchIndex

log = logging.getLogger("mangacouch.ingest")


@dataclass(slots=True)
class IngestPayload:
    """Everything computed from a file on disk, with no DB/state coupling (picklable)."""

    rel_path: str
    size: int
    mtime: float
    archive_id: str
    fingerprint: str | None
    perceptual_hash: str | None
    format: str
    page_count: int
    original_filename: str
    cover_webp: bytes | None
    error: str | None = None


def compute_ingest_payload(
    abs_path: str, manga_root: str, *, cover_size: int = 512, quality: int = 80
) -> IngestPayload:
    """Pure compute step: hash + enumerate + cover-thumbnail one archive. Safe in a process pool."""
    from ..core import imaging  # local import so the worker process loads pyvips lazily

    path = Path(abs_path)
    root = Path(manga_root)
    rel = path.relative_to(root).as_posix()
    st = path.stat()
    fmt = detect_format(path)

    try:
        archive_id = hashing.content_id(path)
        with open_archive(path) as reader:
            pages = reader.list_pages()
            page_count = len(pages)
            fp = (
                hashing.fingerprint_from_reader(reader)
                if fmt in ("zip", "cbz")
                else None
            )
            cover_webp: bytes | None = None
            phash: str | None = None
            if pages:
                from ..core.thumbnails import _encode_page_thumb

                cover_webp = _encode_page_thumb(reader, pages[0], cover_size, quality)
                try:
                    phash = imaging.dhash(cover_webp)
                except Exception:  # noqa: BLE001 — pHash is best-effort (P1)
                    phash = None
    except Exception as exc:  # noqa: BLE001 — record, don't crash the scan
        return IngestPayload(
            rel_path=rel,
            size=st.st_size,
            mtime=st.st_mtime,
            archive_id="",
            fingerprint=None,
            perceptual_hash=None,
            format=fmt,
            page_count=0,
            original_filename=path.name,
            cover_webp=None,
            error=f"{type(exc).__name__}: {exc}",
        )

    return IngestPayload(
        rel_path=rel,
        size=st.st_size,
        mtime=st.st_mtime,
        archive_id=archive_id,
        fingerprint=fp,
        perceptual_hash=phash,
        format=fmt,
        page_count=page_count,
        original_filename=path.name,
        cover_webp=cover_webp,
    )


class Ingestor:
    """Serialises DB / thumbnail / FTS writes for the organize flow."""

    def __init__(
        self,
        manga_root: Path,
        thumbs: ThumbStore,
        search: SearchIndex,
        *,
        cover_size: int = 512,
        quality: int = 80,
        executor: Executor | None = None,
    ) -> None:
        self.manga_root = manga_root
        self.thumbs = thumbs
        self.search = search
        self.cover_size = cover_size
        self.quality = quality
        self.executor = executor
        # Serialises index/remove across the callers that share this Ingestor (initial scan,
        # watcher, uploads, downloads, POST /library/scan) — the check-then-insert patterns in
        # _persist/_sync_tags are not safe to interleave.
        self._write_lock = threading.Lock()
        self._scan_lock = threading.Lock()

    # -- public API ---------------------------------------------------------------------------

    def index_file(self, path: Path) -> str | None:
        """Index (or re-index) one archive. Returns the archive id, or ``None`` if skipped."""
        if not is_supported(path) or not path.is_file():
            return None
        rel = path.relative_to(self.manga_root).as_posix()

        with self._write_lock:
            with session_scope() as session:
                existing = session.scalar(select(Archive).where(Archive.rel_path == rel))
                if existing is not None:
                    st = path.stat()
                    if existing.size == st.st_size and abs(existing.mtime - st.st_mtime) < 1e-6:
                        return existing.id  # unchanged → never re-read (R4)

            payload = self._compute(path)
            if payload.error or not payload.archive_id:
                log.warning("ingest failed for %s: %s", rel, payload.error)
                return None
            return self._persist(path, payload)

    def remove_path(self, rel_path: str) -> None:
        with self._write_lock:
            with session_scope() as session:
                arch = session.scalar(select(Archive).where(Archive.rel_path == rel_path))
                if arch is None:
                    return
                archive_id = arch.id
                session.delete(arch)
            self.thumbs.delete_archive(archive_id)
            self.search.delete(archive_id)

    def scan(self, paths: list[Path] | None = None) -> dict[str, int]:
        """Index every supported archive under the manga root (or a given subset).

        Only one full scan runs at a time; a second call while one is in flight is a no-op.
        """
        if not self._scan_lock.acquire(blocking=False):
            log.info("scan already running — skipping duplicate request")
            return {"indexed": 0, "skipped": 0, "failed": 0, "already_running": 1}
        try:
            full_scan = paths is None
            files = paths if paths is not None else self._discover()
            stats = {"indexed": 0, "skipped": 0, "failed": 0}
            for path in files:
                try:
                    result = self.index_file(path)
                    if result is None:
                        stats["skipped"] += 1
                    else:
                        stats["indexed"] += 1
                except Exception:
                    log.exception("error indexing %s", path)
                    stats["failed"] += 1
            if full_scan:
                # Files deleted/renamed while the server was down never produced a watcher
                # event — prune rows whose file is gone or they linger as ghost archives.
                stats["pruned"] = self._prune_missing({
                    p.relative_to(self.manga_root).as_posix() for p in files
                })
            return stats
        finally:
            self._scan_lock.release()

    def _prune_missing(self, discovered_rel_paths: set[str]) -> int:
        """Remove index rows for archives that no longer exist on disk (full scans only)."""
        pruned = 0
        with self._write_lock:
            with session_scope() as session:
                rows = session.execute(select(Archive.id, Archive.rel_path)).all()
            for archive_id, rel_path in rows:
                if rel_path in discovered_rel_paths:
                    continue
                if (self.manga_root / rel_path).is_file():
                    continue  # appeared after discovery — leave it alone
                with session_scope() as session:
                    arch = session.get(Archive, archive_id)
                    if arch is not None:
                        session.delete(arch)
                self.thumbs.delete_archive(archive_id)
                self.search.delete(archive_id)
                log.info("pruned missing archive %s (%s)", archive_id, rel_path)
                pruned += 1
        return pruned

    # -- internals ----------------------------------------------------------------------------

    def _discover(self) -> list[Path]:
        return [p for p in self.manga_root.rglob("*") if p.is_file() and is_supported(p)]

    def _compute(self, path: Path) -> IngestPayload:
        args = (str(path), str(self.manga_root))
        kwargs = {"cover_size": self.cover_size, "quality": self.quality}
        if self.executor is not None:
            return self.executor.submit(compute_ingest_payload, *args, **kwargs).result()
        return compute_ingest_payload(*args, **kwargs)

    def _persist(self, path: Path, payload: IngestPayload) -> str:
        mc = sidecars.read_mc(path)
        eze = sidecars.read_eze(path)

        # Metadata precedence: native sidecar → eze sidecar → filename.
        title = ""
        title_jpn = None
        summary = ""
        rating = None
        language = None
        category = None
        source_url = source_token = None
        source_gid = None
        uploader = None
        posted_dt: datetime | None = None
        tag_list: list[str] = []

        if eze is not None:
            title = eze.title or title
            title_jpn = eze.title_jpn or title_jpn
            category = eze.category or category
            uploader = eze.uploader or uploader
            rating = eze.rating if eze.rating is not None else rating
            source_gid = eze.gid or source_gid
            source_token = eze.token or source_token
            tag_list = list(eze.tags)
            if eze.posted:
                posted_dt = datetime.fromtimestamp(eze.posted, UTC)
        if mc is not None:
            title = mc.title or title
            title_jpn = mc.title_jpn or title_jpn
            summary = mc.summary or summary
            rating = mc.rating if mc.rating is not None else rating
            language = mc.language or language
            category = mc.category or category
            source_url = mc.source_url or source_url
            source_gid = mc.source_gid or source_gid
            source_token = mc.source_token or source_token
            uploader = mc.uploader or uploader
            if mc.tags:
                tag_list = list(mc.tags)
            if mc.posted:
                posted_dt = datetime.fromtimestamp(mc.posted, UTC)
        if not title:
            title = sidecars.normalise_display(Path(payload.original_filename).stem)

        with session_scope() as session:
            if self._reconcile_identity(session, payload):
                # Duplicate content whose original still exists — leave the existing row
                # (and its progress/favorites) pointing at the original file.
                return payload.archive_id
            arch = session.get(Archive, payload.archive_id)
            is_new = arch is None
            if arch is None:
                arch = Archive(id=payload.archive_id, added_at=datetime.now(UTC))
                session.add(arch)
            arch.fingerprint = payload.fingerprint
            arch.perceptual_hash = payload.perceptual_hash
            arch.rel_path = payload.rel_path
            arch.size = payload.size
            arch.mtime = payload.mtime
            arch.format = payload.format
            arch.page_count = payload.page_count
            arch.title = sidecars.normalise_display(title)
            arch.title_jpn = title_jpn
            arch.original_filename = payload.original_filename
            arch.summary = summary
            arch.rating = rating
            arch.language = language
            arch.category = category
            arch.source_url = source_url
            arch.source_gid = source_gid
            arch.source_token = source_token
            arch.uploader = uploader
            arch.posted_at = posted_dt
            arch.cover_status = "ready" if payload.cover_webp else "error"

            # date_added auto-tag (§5.2).
            if not any(t.startswith("date_added:") for t in tag_list):
                tag_list.append(f"date_added:{arch.added_at.date().isoformat()}")
            if source_url and not any(t.startswith("source:") for t in tag_list):
                tag_list.append(f"source:{source_url}")

            self._sync_tags(session, arch, tag_list)
            # Restore the sidecar's progress pointer on first import only — an existing row's
            # progress is live user state and must never be rolled back by a rescan.
            if is_new and mc is not None and mc.progress_page > 0:
                from ..db.models import Progress

                session.add(
                    Progress(
                        archive_id=payload.archive_id,
                        page=min(mc.progress_page, max(payload.page_count, 0)),
                    )
                )
            session.flush()
            final_tags = self._current_tags(session, arch.id)

        # Side stores (outside the DB transaction).
        if payload.cover_webp:
            self.thumbs.put(
                payload.archive_id, COVER_PAGE, VARIANT_COVER, "image/webp", payload.cover_webp
            )
        self.search.upsert(payload.archive_id, title, final_tags)

        # Refresh the native sidecar so the folder stays self-describing (§3.3).
        self._write_sidecars(path, payload, final_tags, title, title_jpn, summary, rating,
                             language, category, source_url, source_gid, source_token,
                             uploader, posted_dt, mc)
        return payload.archive_id

    def _reconcile_identity(self, session: Session, payload: IngestPayload) -> bool:
        """Handle moves and re-content: drop a stale row that now points at a missing/other file.

        Returns ``True`` when the payload is a *duplicate copy* (same content, original file
        still present) — the caller must then leave the existing row untouched, otherwise the
        copy would steal the row and deleting the copy would cascade away the original's
        progress/favorites/tags.
        """
        # Same path, different content (id changed): remove the old row so the new id can be inserted.
        stale = session.scalar(
            select(Archive).where(
                Archive.rel_path == payload.rel_path, Archive.id != payload.archive_id
            )
        )
        if stale is not None:
            old_id = stale.id
            session.delete(stale)
            session.flush()
            self.thumbs.delete_archive(old_id)
            self.search.delete(old_id)
        # Same content already known at another path: if that path is gone, treat this as a move.
        same = session.get(Archive, payload.archive_id)
        if same is not None and same.rel_path != payload.rel_path:
            old_abs = self.manga_root / same.rel_path
            if old_abs.exists():
                log.info("duplicate content: %s mirrors %s", payload.rel_path, same.rel_path)
                return True
        return False

    def _sync_tags(self, session: Session, arch: Archive, tags: list[str]) -> None:
        session.query(ArchiveTag).filter(ArchiveTag.archive_id == arch.id).delete()
        seen: set[tuple[str, str]] = set()
        for raw in tags:
            ns, value = sidecars._split_tag(raw)
            key = (ns, value)
            if not value or key in seen:
                continue
            seen.add(key)
            tag = session.scalar(
                select(Tag).where(Tag.namespace == ns, Tag.value == value)
            )
            if tag is None:
                tag = Tag(namespace=ns, value=value)
                session.add(tag)
                session.flush()
            session.add(ArchiveTag(archive_id=arch.id, tag_id=tag.id))

    def _current_tags(self, session: Session, archive_id: str) -> list[str]:
        rows = session.execute(
            select(Tag.namespace, Tag.value)
            .join(ArchiveTag, ArchiveTag.tag_id == Tag.id)
            .where(ArchiveTag.archive_id == archive_id)
        ).all()
        return [f"{ns}:{value}" if ns else value for ns, value in rows]

    def _write_sidecars(self, path, payload, tags, title, title_jpn, summary, rating, language,
                       category, source_url, source_gid, source_token, uploader, posted_dt, mc):
        posted = int(posted_dt.timestamp()) if posted_dt else (mc.posted if mc else None)
        mc_sidecar = sidecars.McSidecar(
            archive_id=payload.archive_id,
            fingerprint=payload.fingerprint,
            format=payload.format,
            page_count=payload.page_count,
            original_filename=payload.original_filename,
            title=title,
            title_jpn=title_jpn,
            summary=summary,
            rating=rating,
            language=language,
            category=category,
            source_url=source_url,
            source_gid=source_gid,
            source_token=source_token,
            uploader=uploader,
            posted=posted,
            tags=tags,
            progress_page=mc.progress_page if mc else 0,
            added_at=mc.added_at if mc else datetime.now(UTC).isoformat(),
            ingest=mc.ingest if mc else {"via": "scan"},
        )
        try:
            sidecars.write_mc(path, mc_sidecar)
        except OSError:
            log.warning("could not write native sidecar for %s", payload.rel_path)
