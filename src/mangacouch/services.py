"""Shared state for the HTTP route modules: services, limiters, projections and helpers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

import anyio
from fastapi import HTTPException, Request, Response
from fastapi.encoders import jsonable_encoder

from .archives import IMAGE_VERSION
from .auth import Auth, Principal
from .collections import Collections
from .configuration import HTTPSettings
from .database import Book
from .downloads import Downloads
from .duplicates import Duplicates
from .editions import Editions
from .image_cache import ImageCache
from .ingest import FileLibrary
from .maintenance import Maintenance
from .models import canonical_json
from .organization import Organization
from .removals import Removals
from .review import Review
from .scanning import Scanner
from .sidecars import SidecarSync

T = TypeVar("T")
IMMUTABLE = "private, max-age=31536000, immutable"
REVALIDATE = "private, no-cache"
CLIENT_IMAGE = "private, max-age=86400"
VARY = "Cookie, Authorization"


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

    async def bounded(self, call: Callable[[], T]) -> T:
        """Run a mutating call in a worker thread, at most two at a time."""
        return await anyio.to_thread.run_sync(call, limiter=self.mutation_limiter)

    def project_books(
        self, db: sqlite3.Connection, identifiers: list[str], *, full: bool = False
    ) -> list[dict]:
        """API projection of books in the given order; ids that no longer exist are skipped."""
        books = self.files.library.get_books(identifiers, db)
        selected = canonical_json([book.id for book in books])
        states = {
            row["book_id"]: dict(row)
            for row in db.execute(
                "SELECT book_id,pending_revision,synced_revision,last_error FROM sidecar_sync "
                "WHERE book_id IN (SELECT value FROM json_each(?))",
                (selected,),
            )
        }
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
            state = states.get(book.id, {}).copy()
            state.pop("book_id", None)
            item = {
                **jsonable_encoder(book),
                "tag_translations": translations[book.id],
                "source_check": checks.get(book.id),
                "sidecar": state,
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
            return self.project_books(db, [book.id], full=True)[0]

    def clear_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.settings.cookie_name,
            path="/",
            secure=self.settings.public_origin.startswith("https://"),
            httponly=True,
            samesite="strict",
        )

    def set_cookie(self, response: Response, token: str, expires_at: int) -> None:
        response.set_cookie(
            self.settings.cookie_name,
            token,
            max_age=max(0, expires_at - int(self.auth.clock())),
            secure=self.settings.public_origin.startswith("https://"),
            httponly=True,
            samesite="strict",
            path="/",
        )


def image_version(book: Book) -> str:
    """The `v` query value that makes a cover or thumbnail URL immutable."""
    return book.content.sha256[:16]


def image_etag(files: FileLibrary, book: Book, index: int, kind: str) -> str:
    """Weak validator from registered content only; changed files fail when rendered."""
    payload = f"{book.content.sha256}:{index}:{kind}:{files.limits}:{IMAGE_VERSION}"
    return 'W/"' + hashlib.sha256(payload.encode()).hexdigest() + '"'


async def image_response(
    request: Request,
    render: Callable[[], bytes],
    *,
    media_type: str | Callable[[bytes], str],
    limiter: anyio.CapacityLimiter,
    cache_control: str,
    etag: str | None = None,
) -> Response:
    """One image response path for web, OPDS and Komga: conditional GET, HEAD, limiter."""
    headers = {"Cache-Control": cache_control, "Vary": VARY}
    if etag is not None:
        headers["ETag"] = etag
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers=headers)
    async with limiter:
        if await request.is_disconnected():
            return Response(status_code=499)
        data = await anyio.to_thread.run_sync(render)
    headers["Content-Length"] = str(len(data))
    return Response(
        data if request.method != "HEAD" else b"",
        media_type=media_type if isinstance(media_type, str) else media_type(data),
        headers=headers,
    )
