"""Library + reader routes (§6.1): listing, detail, page list, page/thumbnail serving, metadata,
progress, delete."""

from __future__ import annotations

import contextlib
import mimetypes
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ...core.archives import open_archive
from ...core.thumbnails import (
    COVER_PAGE,
    VARIANT_COVER,
    VARIANT_PAGE,
    generate_cover,
    generate_page_thumb,
)
from ...db.models import Archive, ArchiveTag, History, Progress, Tag
from ...search import parse_query, search_archives
from ...state import AppContext
from ..deps import get_context, get_db, require_auth, require_auth_media, require_owner
from ..serialization import related_archives, serialize_archive, serialize_card

router = APIRouter(prefix="/api", tags=["library"])

_IMMUTABLE = {"Cache-Control": "public, max-age=31536000, immutable"}


def _load(db: Session, archive_id: str) -> Archive:
    arch = db.scalar(
        select(Archive)
        .options(
            selectinload(Archive.tags).selectinload(ArchiveTag.tag),
            selectinload(Archive.progress),
            selectinload(Archive.comments),
        )
        .where(Archive.id == archive_id)
    )
    if arch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    return arch


def _archive_path(ctx: AppContext, arch: Archive) -> Path:
    return ctx.config.manga_root / arch.rel_path


# -- listing & detail -------------------------------------------------------------------------


@router.get("/archives")
def list_archives(
    request: Request,
    q: str | None = None,
    category: int | None = None,
    sort: str = "date_added",
    sortdir: str = "desc",
    page: int = 1,
    page_size: int = Query(50, ge=1, le=500),
    newonly: bool = False,
    _: object = Depends(require_auth),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query_text = q or ""
    # The PWA sends `newonly` as its own param; it is sugar for the query filter token.
    if newonly:
        query_text = f"{query_text},newonly" if query_text else "newonly"
    static_category_id = None
    if category is not None:
        from ...db.models import Category

        cat = db.get(Category, category)
        if cat is not None and cat.type == "dynamic" and cat.predicate:
            query_text = f"{query_text},{cat.predicate}" if query_text else cat.predicate
        elif cat is not None:
            static_category_id = cat.id

    parsed = parse_query(query_text)
    result = search_archives(
        db,
        ctx.search,
        parsed,
        static_category_id=static_category_id,
        sort=sort,
        sortdir=sortdir,
        page=page,
        per_page=page_size,
    )
    return {
        "archives": [serialize_card(a, ctx.translator) for a in result.items],
        "total": result.total,
        "page": result.page,
        "page_size": result.per_page,
    }


@router.get("/archives/random")
def random_archive(
    _: object = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """A random *new* (never-opened) archive when one exists, else any random archive."""
    from sqlalchemy import func

    unread = (
        select(Archive.id)
        .outerjoin(Progress, Progress.archive_id == Archive.id)
        .where(func.coalesce(Progress.page, 0) == 0)
        .order_by(func.random())
        .limit(1)
    )
    archive_id = db.scalar(unread)
    fresh = archive_id is not None
    if archive_id is None:
        archive_id = db.scalar(select(Archive.id).order_by(func.random()).limit(1))
    if archive_id is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "library is empty")
    return {"id": archive_id, "new": fresh}


@router.get("/archives/{archive_id}")
def get_archive(
    archive_id: str,
    _: object = Depends(require_auth),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    arch = _load(db, archive_id)
    data = serialize_archive(arch, ctx.translator, db=db, detail=True)
    data["related"] = related_archives(db, arch, ctx.translator)
    return data


_ARCHIVE_MIMES = {
    "zip": "application/zip",
    "cbz": "application/vnd.comicbook+zip",
    "pdf": "application/pdf",
}


@router.get("/archives/{archive_id}/download")
def download_archive(
    archive_id: str,
    _: object = Depends(require_auth_media),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> FileResponse:
    """Serve the original, unmodified archive file (browser download + OPDS acquisition)."""
    arch = _load(db, archive_id)
    path = _archive_path(ctx, arch)
    if not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive file missing on disk")
    return FileResponse(
        path,
        media_type=_ARCHIVE_MIMES.get(arch.format or "", "application/octet-stream"),
        filename=arch.original_filename or path.name,
    )


# -- page list & page serving -----------------------------------------------------------------


@router.get("/archives/{archive_id}/pages")
def list_pages(
    archive_id: str,
    _: object = Depends(require_auth),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    arch = _load(db, archive_id)
    pages = _page_list(ctx, arch)
    return {"pages": [{"index": i, "path": p} for i, p in enumerate(pages)]}


def _page_list(ctx: AppContext, arch: Archive) -> list[str]:
    key = f"pages:{arch.id}"
    cached = ctx.page_cache.get(key)
    if cached is not None:
        return cast("list[str]", cached)
    with open_archive(_archive_path(ctx, arch)) as reader:
        pages = reader.list_pages()
    ctx.page_cache.set(key, pages)
    return pages


def _guess_mime(path: str) -> str:
    mime, _ = mimetypes.guess_type(path)
    return mime or "application/octet-stream"


@router.get("/archives/{archive_id}/page")
def get_page(
    archive_id: str,
    path: str,
    _: object = Depends(require_auth_media),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> Response:
    arch = _load(db, archive_id)
    pages = set(_page_list(ctx, arch))
    if path not in pages:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "page not found")

    cache_key = f"page:{arch.id}:{path}"
    cached = ctx.page_cache.get(cache_key)
    if cached is not None:
        mime, data = cast("tuple[str, bytes]", cached)
    else:
        try:
            with open_archive(_archive_path(ctx, arch)) as reader:
                data = reader.read_page_bytes(path)
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not read page: {exc}") from exc
        mime = _guess_mime(path)
        ctx.page_cache.set(cache_key, (mime, data))
    return Response(content=data, media_type=mime, headers=dict(_IMMUTABLE))


# -- thumbnails --------------------------------------------------------------------------------


@router.get("/archives/{archive_id}/thumbnail")
def get_thumbnail(
    archive_id: str,
    page: int | None = None,
    _: object = Depends(require_auth_media),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> Response:
    arch = _load(db, archive_id)
    cfg = ctx.config.thumbnails

    if page is None or page < 0:
        hit = ctx.thumbs.get(arch.id, COVER_PAGE, VARIANT_COVER)
        if hit is None:
            with open_archive(_archive_path(ctx, arch)) as reader:
                generate_cover(
                    ctx.thumbs, arch.id, reader, size=cfg.cover_size, quality=cfg.quality
                )
            hit = ctx.thumbs.get(arch.id, COVER_PAGE, VARIANT_COVER)
        if hit is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no cover")
        mime, data = hit
        return Response(content=data, media_type=mime, headers=dict(_IMMUTABLE))

    # Page-grid thumbnail (generated lazily on first request, §4).
    hit = ctx.thumbs.get(arch.id, page, VARIANT_PAGE)
    if hit is None:
        try:
            with open_archive(_archive_path(ctx, arch)) as reader:
                generate_page_thumb(
                    ctx.thumbs, arch.id, reader, page, size=cfg.page_size, quality=cfg.quality
                )
        except Exception as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"thumbnail failed: {exc}") from exc
        hit = ctx.thumbs.get(arch.id, page, VARIANT_PAGE)
    if hit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no thumbnail")
    mime, data = hit
    return Response(content=data, media_type=mime, headers=dict(_IMMUTABLE))


# -- metadata, progress, delete ----------------------------------------------------------------


class MetadataUpdate(BaseModel):
    title: str | None = None
    summary: str | None = None
    rating: float | None = None
    language: str | None = None
    tags: list[str] | None = None  # full replacement, "namespace:value"


@router.put("/archives/{archive_id}/metadata")
def update_metadata(
    archive_id: str,
    body: MetadataUpdate,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    arch = _load(db, archive_id)
    if body.title is not None:
        arch.title = body.title
    if body.summary is not None:
        arch.summary = body.summary
    if body.rating is not None:
        arch.rating = body.rating
    if body.language is not None:
        arch.language = body.language
    if body.tags is not None:
        _replace_tags(db, arch, body.tags)
    db.flush()
    db.refresh(arch)
    # Reflect into the search index + native sidecar.
    tag_strings = [f"{t.tag.namespace}:{t.tag.value}" if t.tag.namespace else t.tag.value
                   for t in arch.tags]
    ctx.search.upsert(arch.id, arch.title, tag_strings)
    _resync_sidecar(ctx, arch, tag_strings)
    return serialize_archive(arch, ctx.translator, db=db, detail=True)


def _replace_tags(db: Session, arch: Archive, tags: list[str]) -> None:
    from ...core.sidecars import _split_tag
    from ...db.models import ArchiveTag

    db.query(ArchiveTag).filter(ArchiveTag.archive_id == arch.id).delete()
    seen: set[tuple[str, str]] = set()
    for raw in tags:
        ns, value = _split_tag(raw)
        if not value or (ns, value) in seen:
            continue
        seen.add((ns, value))
        tag = db.scalar(select(Tag).where(Tag.namespace == ns, Tag.value == value))
        if tag is None:
            tag = Tag(namespace=ns, value=value)
            db.add(tag)
            db.flush()
        db.add(ArchiveTag(archive_id=arch.id, tag_id=tag.id))
    db.flush()


def _resync_sidecar(ctx: AppContext, arch: Archive, tag_strings: list[str]) -> None:
    from ...core import sidecars

    path = _archive_path(ctx, arch)
    if not path.exists():
        return
    mc = sidecars.read_mc(path) or sidecars.McSidecar(
        archive_id=arch.id, fingerprint=arch.fingerprint, format=arch.format,
        page_count=arch.page_count, original_filename=arch.original_filename,
    )
    mc.title = arch.title
    mc.summary = arch.summary
    mc.rating = arch.rating
    mc.language = arch.language
    mc.tags = tag_strings
    with contextlib.suppress(OSError):
        sidecars.write_mc(path, mc)


@router.put("/archives/{archive_id}/progress/{page}")
def set_progress(
    archive_id: str,
    page: int,
    _: object = Depends(require_auth),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    arch = _load(db, archive_id)
    prog = db.get(Progress, archive_id)
    if prog is None:
        prog = Progress(archive_id=archive_id, page=max(0, page))
        db.add(prog)
    else:
        prog.page = max(0, page)
        prog.updated_at = datetime.now(UTC)
    if page <= 1:
        db.add(History(archive_id=archive_id))  # opening (re)starts a history entry
    db.flush()
    return {"archive_id": archive_id, "page": prog.page, "page_count": arch.page_count}


# -- favorite (simple boolean; stored as membership of one implicit default list) ---------------

_DEFAULT_FAVORITES = "Favorites"


def _default_favorite_list(db: Session):
    from ...db.models import FavoriteList

    fl = db.scalar(select(FavoriteList).order_by(FavoriteList.position, FavoriteList.id).limit(1))
    if fl is None:
        fl = FavoriteList(name=_DEFAULT_FAVORITES, position=0)
        db.add(fl)
        db.flush()
    return fl


@router.put("/archives/{archive_id}/favorite")
def set_favorite(
    archive_id: str,
    _: object = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from ...db.models import Favorite

    _load(db, archive_id)
    fl = _default_favorite_list(db)
    if db.get(Favorite, {"list_id": fl.id, "archive_id": archive_id}) is None:
        db.add(Favorite(list_id=fl.id, archive_id=archive_id))
    return {"archive_id": archive_id, "favorite": True}


@router.delete("/archives/{archive_id}/favorite")
def unset_favorite(
    archive_id: str,
    _: object = Depends(require_auth),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from sqlalchemy import delete as sql_delete

    from ...db.models import Favorite

    db.execute(sql_delete(Favorite).where(Favorite.archive_id == archive_id))
    return {"archive_id": archive_id, "favorite": False}


@router.delete("/archives/{archive_id}")
def delete_archive(
    archive_id: str,
    delete_file: bool = True,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    arch = _load(db, archive_id)
    path = _archive_path(ctx, arch)
    rel = arch.rel_path
    db.delete(arch)
    db.flush()
    ctx.thumbs.delete_archive(archive_id)
    ctx.search.delete(archive_id)
    ctx.page_cache.delete(f"pages:{archive_id}")
    if delete_file:
        from ...core import sidecars

        for p in (path, sidecars.eze_sidecar_path(path), sidecars.mc_sidecar_path(path)):
            with contextlib.suppress(OSError):
                p.unlink(missing_ok=True)
    return {"deleted": archive_id, "rel_path": rel, "file_removed": delete_file}
