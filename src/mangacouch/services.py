"""Shared state for the HTTP route modules: services, limiters, projections and helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import anyio
from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .archives import IMAGE_VERSION
from .auth import Auth, Principal
from .collections import Collections
from .configuration import HTTPSettings
from .database import Book
from .downloads import Downloads
from .duplicates import Duplicates
from .editions import Editions
from .errors import LibraryBusy
from .files import set_thread_cancel
from .image_cache import ImageCache
from .ingest import FileLibrary
from .limits import ImportCancelled
from .maintenance import Maintenance
from .models import canonical_json
from .organization import Organization
from .removals import Removals
from .review import Review
from .scanning import Scanner
from .sidecars import SidecarSync
from .warming import Warmer

MUTATION_TIMEOUT = 25.0
SLOT_TIMEOUT = 30.0
T = TypeVar("T")
IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"
CLIENT_IMAGE = "private, max-age=86400"
VARY = "Authorization"


def require_manager(request: Request) -> Principal:
    """Reads that read-only keys must not see; Boundary already blocks their writes."""
    principal: Principal = request.state.principal
    if principal.kind == "readonly":
        raise HTTPException(403, "Management permission required")
    return principal


def require_session(request: Request) -> Principal:
    principal = require_manager(request)
    if principal.kind != "session":
        raise HTTPException(403, "Web session required")
    return principal


@dataclass
class Services:
    files: FileLibrary
    settings: HTTPSettings
    auth: Auth
    downloads: Downloads
    thumbnail_cache: ImageCache
    warmers: dict[str, Warmer]
    """Cache warm-up passes by kind: covers, thumbnails."""
    review: Review
    sync: SidecarSync
    collections: Collections
    organization: Organization
    scanner: Scanner
    removals: Removals
    duplicates: Duplicates
    editions: Editions
    maintenance: Maintenance
    file_limiter: anyio.CapacityLimiter = field(default_factory=lambda: anyio.CapacityLimiter(2))
    mutation_limiter: anyio.CapacityLimiter = field(default_factory=lambda: anyio.CapacityLimiter(2))
    cover_limiter: anyio.CapacityLimiter = field(default_factory=lambda: anyio.CapacityLimiter(2))
    upload_slots: anyio.Semaphore = field(default_factory=lambda: anyio.Semaphore(2))

    async def bounded(self, call: Callable[[], T], *, timeout: float | None = None) -> T:
        """Run heavy library work in a worker thread, at most two at a time.

        Routes use it for work that takes the library operation lock, reads or writes library
        files, or writes rows in proportion to the library (imports, scans, reviews, removals,
        sidecars, bulk jobs, snapshots, translation imports), so waiting requests do not pile up
        in the thread pool. A short database-only write (collections, personal state,
        preferences, aliases, scan pause) runs as an ordinary sync route, and
        a network fetch never holds a slot.
        """

        def bounded_call() -> T:
            set_thread_cancel(lambda: time.monotonic() >= deadline)
            try:
                if time.monotonic() >= deadline:
                    raise LibraryBusy()
                return call()
            except ImportCancelled as error:
                raise LibraryBusy() from error
            finally:
                set_thread_cancel(None)

        try:
            with anyio.fail_after(SLOT_TIMEOUT):
                await self.mutation_limiter.acquire()
        except TimeoutError as error:
            raise LibraryBusy() from error
        try:
            deadline = time.monotonic() + (MUTATION_TIMEOUT if timeout is None else timeout)
            return await anyio.to_thread.run_sync(bounded_call, limiter=None)
        finally:
            self.mutation_limiter.release()

    def project_books(
        self, db: sqlite3.Connection, identifiers: list[str], *, full: bool = False
    ) -> list[dict]:
        """API projection of books in the given order; ids that no longer exist are skipped."""
        books = self.files.library.get_books(identifiers, db)
        selected = canonical_json([book.id for book in books])
        checks = {}
        for row in db.execute(
            "SELECT * FROM source_check WHERE book_id IN (SELECT value FROM json_each(?))", (selected,)
        ):
            check = dict(row)
            check["reasons"] = json.loads(check.pop("reasons_json"))
            checks[row["book_id"]] = check
        translations: dict[str, list[dict]] = {book.id: [] for book in books}
        for row in db.execute(
            "SELECT b.id AS book_id,json_extract(t.value,'$.namespace') AS namespace, "
            "json_extract(t.value,'$.value') AS value,a.translation FROM book b, "
            "json_each(b.metadata_json,'$.tags') t JOIN effective_tag_alias a "
            "ON a.namespace=mc_normalized(json_extract(t.value,'$.namespace')) "
            "AND a.value=mc_normalized(json_extract(t.value,'$.value')) "
            "WHERE b.id IN (SELECT value FROM json_each(?))",
            (selected,),
        ):
            label = dict(row)
            translations[label.pop("book_id")].append(label)
        sizes = {}
        if full:
            sizes = {
                row[0]: [[page.get("width", 0), page.get("height", 0)] for page in json.loads(row[1])]
                for row in db.execute(
                    "SELECT book_id,pages_json FROM archive_manifest "
                    "WHERE book_id IN (SELECT value FROM json_each(?))",
                    (selected,),
                )
            }
        result = []
        for book in books:
            item = {
                **jsonable_encoder(book),
                "tag_translations": translations[book.id],
                "source_check": checks.get(book.id),
                "page_version": image_version(book),
            }
            if full:
                page_sizes = sizes.get(book.id, [])
                if len(page_sizes) != book.content.page_count:
                    page_sizes = [[0, 0]] * book.content.page_count
                item["page_sizes"] = page_sizes
            result.append(item)
        return result

    def book_outputs(self, identifiers: list[str]) -> list[dict]:
        with self.files.library.connection() as db:
            db.execute("BEGIN")
            return self.project_books(db, identifiers)

    def book_output(self, book: Book) -> dict:
        with self.files.library.connection() as db:
            db.execute("BEGIN")
            result = self.project_books(db, [book.id], full=True)[0]
            result["tag_counts"] = [
                dict(row)
                for row in db.execute(
                    "SELECT coalesce(nullif(json_extract(t.value,'$.namespace'),''),'other') AS namespace, "
                    "json_extract(t.value,'$.value') AS value,(SELECT count(*) FROM book_tag bt WHERE "
                    "bt.namespace=coalesce(nullif(mc_normalized(json_extract(t.value,'$.namespace')),''),"
                    "'other') "
                    "AND bt.value=mc_normalized(json_extract(t.value,'$.value'))) AS count "
                    "FROM json_each(?,'$.tags') t",
                    (canonical_json(result["metadata"]),),
                )
            ]
            return result

    def _cookie(self, request: Request) -> tuple[str, bool]:
        """The session cookie's name for this request and whether it is Secure."""
        origin = request.headers.get("origin")
        scheme = self.settings.request_scheme(request.url.netloc, request.url.scheme, origin)
        return self.settings.cookie_for(request.url.netloc, scheme, origin), scheme == "https"

    def clear_cookie(self, response: Response, request: Request) -> None:
        name, secure = self._cookie(request)
        response.delete_cookie(name, path="/", secure=secure, httponly=True, samesite="strict")
        self._clear_http_cookie(response, request, name)

    def _clear_http_cookie(self, response: Response, request: Request, name: str) -> None:
        authority = request.url.netloc
        previous = self.settings.cookie_for(authority, "http", f"http://{authority}")
        if previous != name and previous in request.cookies:
            response.delete_cookie(previous, path="/", httponly=True, samesite="strict")

    def set_cookie(self, response: Response, token: str, expires_at: int, request: Request) -> None:
        name, secure = self._cookie(request)
        response.set_cookie(
            name,
            token,
            max_age=max(0, expires_at - int(self.auth.clock())),
            secure=secure,
            httponly=True,
            samesite="strict",
            path="/",
        )
        self._clear_http_cookie(response, request, name)


def image_version(book: Book) -> str:
    """The `v` query value that makes a page URL immutable; ZIP pages are the archive's own bytes."""
    return book.content.sha256[:16] + (f".{IMAGE_VERSION}" if book.content.format == "pdf" else "")


def thumbnail_version(book: Book) -> str:
    """The `v` query value that makes a cover or thumbnail URL immutable.

    Rendering changes and explicit refreshes both change the URL (web/src/pages.ts).
    """
    return f"{book.content.sha256[:16]}.{IMAGE_VERSION}.{book.thumbnail_revision}"


def image_etag(files: FileLibrary, book: Book, index: int, kind: str) -> str:
    """Weak validator from registered content only; changed files fail when rendered.

    Like the thumbnail cache key, it covers only the limits that shape a rendered image."""
    limits = files.limits
    render = f"{limits.max_dimension}x{limits.max_render_pixels}"
    payload = f"{book.content.sha256}:{index}:{kind}:{render}:{IMAGE_VERSION}"
    if kind in {"cover", "thumbnail"}:
        payload += f":{book.thumbnail_revision}"
    return 'W/"' + hashlib.sha256(payload.encode()).hexdigest() + '"'


class ImageResponse(Response):
    """Limit image generation; cache hits and response delivery need no generation slot."""

    def __init__(
        self,
        request: Request,
        render: Callable[[], bytes],
        media_type: str | Callable[[bytes], str],
        limiter: anyio.CapacityLimiter,
        headers: dict[str, str],
        cached: Callable[[], bytes | None] | None,
    ):
        super().__init__(headers=headers)
        self.request, self.render_image, self.image_type = request, render, media_type
        self.limiter, self.cached = limiter, cached

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if await self.request.is_disconnected():
            await Response(status_code=499)(scope, receive, send)
            return
        data = await anyio.to_thread.run_sync(self.cached) if self.cached is not None else None
        if data is None:
            try:
                with anyio.fail_after(SLOT_TIMEOUT):
                    await self.limiter.acquire()
            except TimeoutError as error:
                raise LibraryBusy() from error
            try:
                if await self.request.is_disconnected():
                    data = None
                else:
                    data = await anyio.to_thread.run_sync(self.render_image)
            finally:
                self.limiter.release()
            if data is None:
                await Response(status_code=499)(scope, receive, send)
                return
        headers = dict(self.headers)
        headers["content-length"] = str(len(data))
        media_type = self.image_type if isinstance(self.image_type, str) else self.image_type(data)
        if scope["method"] == "HEAD":
            await Response(headers=headers, media_type=media_type)(scope, receive, send)
            return

        async def chunks():
            for offset in range(0, len(data), 64 * 1024):
                yield data[offset : offset + 64 * 1024]

        await StreamingResponse(chunks(), headers=headers, media_type=media_type)(scope, receive, send)


async def image_response(
    request: Request,
    render: Callable[[], bytes],
    *,
    media_type: str | Callable[[bytes], str],
    limiter: anyio.CapacityLimiter,
    cache_control: str,
    etag: str | None = None,
    cached: Callable[[], bytes | None] | None = None,
) -> Response:
    """One image response path for web, OPDS and Komga: conditional GET, HEAD, limiter."""
    headers = {"Cache-Control": cache_control, "Vary": VARY}
    if etag is not None:
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
    return ImageResponse(request, render, media_type, limiter, headers, cached)
