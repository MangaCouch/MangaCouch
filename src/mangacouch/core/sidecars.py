"""Sidecars (§3.3) — the manga folder is self-describing and portable.

The archive file is stored **unmodified** (writing into it would change its content hash, R4). All
added metadata lives in two external JSON sidecars next to the archive:

- ``<name>.json`` — **Eze format**, the community-standard e-hentai sidecar (gid/token, title,
  title_jpn, category, uploader, posted, filecount, filesize, rating, namespaced tags). Interop.
- ``<name>.mc.json`` — **MangaCouch-native** (our archive id, both hashes, source, tags, rating,
  a progress pointer, and ingest provenance).

Re-importing into a fresh database reconstructs everything from these. No ComicInfo.xml, no
in-archive writes (R4) — interop is deferred.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SIDECAR_VERSION = 1


def eze_sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_suffix(".json")


def mc_sidecar_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.stem + ".mc.json")


def normalise_display(name: str) -> str:
    """NFC-normalise a display string in exactly one place (§3.4)."""
    return unicodedata.normalize("NFC", name)


def _split_tag(tag: str) -> tuple[str, str]:
    ns, _, value = tag.partition(":")
    return (ns.strip(), value.strip()) if value else ("", ns.strip())


def _join_tag(namespace: str, value: str) -> str:
    return f"{namespace}:{value}" if namespace else value


@dataclass(slots=True)
class GalleryMetadata:
    """The normalised metadata both sidecars project from."""

    title: str = ""
    title_jpn: str | None = None
    category: str | None = None
    uploader: str | None = None
    posted: int | None = None  # unix seconds
    rating: float | None = None
    filecount: int | None = None
    filesize: int | None = None
    gid: int | None = None
    token: str | None = None
    site: str | None = None  # "e-hentai" | "exhentai"
    source_url: str | None = None
    tags: list[str] = field(default_factory=list)  # "namespace:value" form

    # ---- Eze interop ----
    def to_eze(self) -> dict:
        tags_by_ns: dict[str, list[str]] = {}
        for tag in self.tags:
            ns, value = _split_tag(tag)
            tags_by_ns.setdefault(ns or "misc", []).append(value)
        info: dict = {
            "title": self.title,
            "title_original": self.title_jpn or "",
            "tags": tags_by_ns,
            "category": self.category or "",
            "uploader": self.uploader or "",
            "posted": str(self.posted) if self.posted is not None else "",
            "rating": f"{self.rating:.2f}" if self.rating is not None else "",
            "filecount": str(self.filecount) if self.filecount is not None else "",
            "filesize": self.filesize if self.filesize is not None else 0,
        }
        if self.gid and self.token:
            info["source"] = {
                "site": self.site or "e-hentai",
                "gid": self.gid,
                "token": self.token,
            }
        return {"gallery_info": info, "version": SIDECAR_VERSION}

    @classmethod
    def from_eze(cls, raw: dict) -> GalleryMetadata:
        info = raw.get("gallery_info", raw)
        tags: list[str] = []
        raw_tags = info.get("tags", {})
        if isinstance(raw_tags, dict):
            for ns, values in raw_tags.items():
                for value in values:
                    tags.append(_join_tag("" if ns == "misc" else ns, value))
        elif isinstance(raw_tags, list):
            tags = [str(t) for t in raw_tags]
        source = info.get("source", {}) or {}

        def _int(v: object) -> int | None:
            try:
                return int(str(v)) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        def _float(v: object) -> float | None:
            try:
                return float(str(v)) if v not in (None, "") else None
            except (TypeError, ValueError):
                return None

        return cls(
            title=info.get("title", ""),
            title_jpn=info.get("title_original") or None,
            category=info.get("category") or None,
            uploader=info.get("uploader") or None,
            posted=_int(info.get("posted")),
            rating=_float(info.get("rating")),
            filecount=_int(info.get("filecount")),
            filesize=_int(info.get("filesize")),
            gid=_int(source.get("gid")),
            token=source.get("token") or None,
            site=source.get("site") or None,
            tags=tags,
        )


@dataclass(slots=True)
class McSidecar:
    """MangaCouch-native sidecar — fully reconstructs our DB row on re-import."""

    archive_id: str
    fingerprint: str | None
    format: str
    page_count: int
    original_filename: str
    title: str = ""
    title_jpn: str | None = None
    summary: str = ""
    rating: float | None = None
    language: str | None = None
    category: str | None = None
    source_url: str | None = None
    source_gid: int | None = None
    source_token: str | None = None
    uploader: str | None = None
    posted: int | None = None
    tags: list[str] = field(default_factory=list)
    progress_page: int = 0
    added_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    ingest: dict = field(default_factory=dict)  # provenance: {"via": "scan"|"upload"|"download"}

    def to_dict(self) -> dict:
        return {
            "schema": "mangacouch.sidecar/1",
            "archive_id": self.archive_id,
            "fingerprint": self.fingerprint,
            "format": self.format,
            "page_count": self.page_count,
            "original_filename": self.original_filename,
            "title": self.title,
            "title_jpn": self.title_jpn,
            "summary": self.summary,
            "rating": self.rating,
            "language": self.language,
            "category": self.category,
            "source": {
                "url": self.source_url,
                "gid": self.source_gid,
                "token": self.source_token,
                "uploader": self.uploader,
                "posted": self.posted,
            },
            "tags": self.tags,
            "progress_page": self.progress_page,
            "added_at": self.added_at,
            "ingest": self.ingest,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> McSidecar:
        source = raw.get("source", {}) or {}
        return cls(
            archive_id=raw["archive_id"],
            fingerprint=raw.get("fingerprint"),
            format=raw.get("format", ""),
            page_count=int(raw.get("page_count", 0)),
            original_filename=raw.get("original_filename", ""),
            title=raw.get("title", ""),
            title_jpn=raw.get("title_jpn"),
            summary=raw.get("summary", ""),
            rating=raw.get("rating"),
            language=raw.get("language"),
            category=raw.get("category"),
            source_url=source.get("url"),
            source_gid=source.get("gid"),
            source_token=source.get("token"),
            uploader=source.get("uploader"),
            posted=source.get("posted"),
            tags=list(raw.get("tags", [])),
            progress_page=int(raw.get("progress_page", 0)),
            added_at=raw.get("added_at", datetime.now(UTC).isoformat()),
            ingest=raw.get("ingest", {}),
        )


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on the same filesystem; no symlink reliance (R3)


def write_eze(archive_path: Path, meta: GalleryMetadata) -> Path:
    path = eze_sidecar_path(archive_path)
    _atomic_write_json(path, meta.to_eze())
    return path


def read_eze(archive_path: Path) -> GalleryMetadata | None:
    path = eze_sidecar_path(archive_path)
    if not path.exists():
        return None
    try:
        return GalleryMetadata.from_eze(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, OSError):
        return None


def write_mc(archive_path: Path, sidecar: McSidecar) -> Path:
    path = mc_sidecar_path(archive_path)
    _atomic_write_json(path, sidecar.to_dict())
    return path


def read_mc(archive_path: Path) -> McSidecar | None:
    path = mc_sidecar_path(archive_path)
    if not path.exists():
        return None
    try:
        return McSidecar.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, OSError):
        return None
