"""Serialise ORM rows into the JSON shapes the PWA expects (§6.1)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import Archive, ArchiveTag, Favorite
from ..tags.translation import TagTranslator

_READ_FRACTION = 0.85


def _percent(page: int, page_count: int) -> float:
    if page_count <= 0:
        return 0.0
    return min(1.0, page / page_count)


def serialize_tags(arch: Archive, translator: TagTranslator) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for link in sorted(arch.tags, key=lambda t: (t.tag.namespace, t.tag.value)):
        tag = link.tag
        translated = translator.translate(tag.namespace, tag.value)
        out.append(
            {
                "namespace": tag.namespace,
                "value": tag.value,
                "translated": translated,
            }
        )
    return out


def serialize_archive(
    arch: Archive,
    translator: TagTranslator,
    *,
    db: Session | None = None,
    detail: bool = False,
) -> dict[str, Any]:
    page = arch.progress.page if arch.progress else 0
    percent = _percent(page, arch.page_count)
    data: dict[str, Any] = {
        "id": arch.id,
        "title": arch.title,
        "title_jpn": arch.title_jpn,
        "original_filename": arch.original_filename,
        "summary": arch.summary,
        "rating": arch.rating,
        "language": arch.language,
        "category": arch.category,
        "format": arch.format,
        "size": arch.size,
        "page_count": arch.page_count,
        "cover_status": arch.cover_status,
        "added_at": arch.added_at.isoformat() if arch.added_at else None,
        "posted_at": arch.posted_at.isoformat() if arch.posted_at else None,
        "uploader": arch.uploader,
        "source_url": arch.source_url,
        "source_gid": arch.source_gid,
        "source_token": arch.source_token,
        "fingerprint": arch.fingerprint,
        "tags": serialize_tags(arch, translator),
        "progress": {"page": page, "percent": percent},
        "read": percent > _READ_FRACTION,
        "love_count": arch.love_count,
        "view_count": arch.view_count,
    }
    if db is not None:
        favorite_count = int(
            db.scalar(select(func.count()).select_from(Favorite).where(Favorite.archive_id == arch.id))
            or 0
        )
        data["favorite_count"] = favorite_count
        data["favorite"] = favorite_count > 0
    if detail:
        data["comments"] = serialize_comments(arch)
    return data


def serialize_comments(arch: Archive) -> list[dict[str, Any]]:
    return [
        {
            "username": c.username,
            "posted_at": c.posted_at.isoformat() if c.posted_at else None,
            "content": c.content,
        }
        for c in sorted(arch.comments, key=lambda c: (c.posted_at or c.id, c.id))
    ]


def related_archives(db: Session, arch: Archive, translator: TagTranslator, limit: int = 12) -> dict:
    """Best-effort 'similar' (shared artist/parody tags) and 'same series' (same parody)."""
    artist_parody = [
        link.tag.id for link in arch.tags if link.tag.namespace in ("artist", "parody", "group")
    ]
    parody_tag_ids = [link.tag.id for link in arch.tags if link.tag.namespace == "parody"]

    def by_tag_ids(tag_ids: list[int]) -> list[dict]:
        if not tag_ids:
            return []
        rows = db.execute(
            select(Archive)
            .join(ArchiveTag, ArchiveTag.archive_id == Archive.id)
            .where(ArchiveTag.tag_id.in_(tag_ids), Archive.id != arch.id)
            .group_by(Archive.id)
            .order_by(func.count().desc())
            .limit(limit)
            .options(
                selectinload(Archive.tags).selectinload(ArchiveTag.tag),
                selectinload(Archive.progress),
            )
        ).scalars().unique().all()
        return [serialize_card(a, translator) for a in rows]

    return {
        "similar": by_tag_ids(artist_parody),
        "same_series": by_tag_ids(parody_tag_ids),
    }


def serialize_card(arch: Archive, translator: TagTranslator) -> dict[str, Any]:
    """A lighter projection for grid cards / related lists."""
    page = arch.progress.page if arch.progress else 0
    return {
        "id": arch.id,
        "title": arch.title,
        "page_count": arch.page_count,
        "category": arch.category,
        "rating": arch.rating,
        "cover_status": arch.cover_status,
        "progress": {"page": page, "percent": _percent(page, arch.page_count)},
        "read": _percent(page, arch.page_count) > _READ_FRACTION,
        "tags": serialize_tags(arch, translator),
    }
