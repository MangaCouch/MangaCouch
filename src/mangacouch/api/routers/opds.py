"""OPDS 1.2 catalog + Page-Streaming Extension (PSE) — P1 (§6.1)."""

from __future__ import annotations

import mimetypes
from urllib.parse import quote
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.archives import open_archive
from ...db.models import Archive
from ...state import AppContext
from ..deps import get_context, get_db, require_reader_media

router = APIRouter(prefix="/api/opds", tags=["opds"])

_ATOM = "application/atom+xml;profile=opds-catalog"
_PSE_NS = "http://vaemendis.net/opds-pse/ns"


def _feed(title: str, entries: str, *, self_url: str, extra_links: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opds="http://opds-spec.org/2010/catalog" '
        f'xmlns:pse="{_PSE_NS}">\n'
        f"  <title>{escape(title)}</title>\n"
        f"  <id>{escape(self_url)}</id>\n"
        f'  <link rel="self" href="{escape(self_url)}" type="{_ATOM}"/>\n'
        f'  <link rel="start" href="/api/opds" type="{_ATOM}"/>\n'
        f"{extra_links}{entries}</feed>\n"
    )


def _archive_entry(arch: Archive, key: str | None = None) -> str:
    # OPDS readers authenticate the catalog with ?key=…, but fetch covers/pages with plain GETs
    # — carry the caller's key into every generated media URL or they all 401.
    suffix = f"key={quote(key)}" if key else ""
    cover = f"/api/archives/{arch.id}/thumbnail" + (f"?{suffix}" if suffix else "")
    pse = f"/api/opds/{arch.id}/pse?page={{pageNumber}}" + (f"&{suffix}" if suffix else "")
    return (
        "  <entry>\n"
        f"    <title>{escape(arch.title or arch.original_filename)}</title>\n"
        f"    <id>urn:mangacouch:{arch.id}</id>\n"
        f"    <link rel='http://opds-spec.org/image' href='{escape(cover)}' type='image/webp'/>\n"
        f"    <link rel='http://opds-spec.org/image/thumbnail' href='{escape(cover)}' "
        "type='image/webp'/>\n"
        f"    <link rel='http://vaemendis.net/opds-pse/stream' href='{escape(pse)}' "
        f"type='image/jpeg' pse:count='{arch.page_count}'/>\n"
        "  </entry>\n"
    )


@router.get("")
def opds_root(
    request: Request,
    q: str | None = None,
    limit: int = Query(60, ge=1, le=200),
    _: object = Depends(require_reader_media),
    db: Session = Depends(get_db),
) -> Response:
    stmt = select(Archive).order_by(Archive.added_at.desc()).limit(limit)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(Archive.title.like(like))
    entries = "".join(
        _archive_entry(a, request.query_params.get("key")) for a in db.scalars(stmt).all()
    )
    search_link = (
        '  <link rel="search" href="/api/opds?q={searchTerms}" type="' + _ATOM + '"/>\n'
    )
    feed = _feed("MangaCouch Library", entries, self_url=str(request.url), extra_links=search_link)
    return Response(content=feed, media_type=_ATOM)


@router.get("/{archive_id}")
def opds_entry(
    archive_id: str,
    request: Request,
    _: object = Depends(require_reader_media),
    db: Session = Depends(get_db),
) -> Response:
    arch = db.get(Archive, archive_id)
    if arch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    feed = _feed(
        arch.title or arch.original_filename,
        _archive_entry(arch, request.query_params.get("key")),
        self_url=str(request.url),
    )
    return Response(content=feed, media_type=_ATOM)


@router.get("/{archive_id}/pse")
def opds_pse(
    archive_id: str,
    page: int = Query(1, ge=1),
    _: object = Depends(require_reader_media),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> Response:
    arch = db.get(Archive, archive_id)
    if arch is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    path = ctx.config.manga_root / arch.rel_path
    try:
        with open_archive(path) as reader:
            pages = reader.list_pages()
            if not 1 <= page <= len(pages):
                raise HTTPException(status.HTTP_404_NOT_FOUND, "page out of range")
            page_id = pages[page - 1]
            data = reader.read_page_bytes(page_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"could not read page: {exc}") from exc
    mime = mimetypes.guess_type(page_id)[0] or "image/jpeg"
    return Response(content=data, media_type=mime)
