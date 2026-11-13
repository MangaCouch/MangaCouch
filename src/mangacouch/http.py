from __future__ import annotations

import base64
import errno
import logging
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from http import HTTPStatus

import anyio
import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api_auth import auth_router
from .api_collections import collections_router
from .api_images import images_router
from .api_library import library_router
from .api_removals import removals_router
from .api_review import review_router
from .api_scans import scans_router
from .api_settings import settings_router
from .auth import Auth, Unauthorized
from .collections import Collections
from .configuration import HTTPSettings, load_http_settings
from .database import busy
from .downloads import Downloads
from .duplicates import Duplicates
from .editions import Editions
from .errors import InvalidInput, LibraryBusy, NotFound, RateLimited, RevisionConflict
from .eze_cache import EzeCache
from .files import ContentChanged, RootUnavailable
from .image_cache import ImageCache
from .ingest import FileLibrary
from .komga import install_komga
from .lifecycle import WEB_DIR
from .maintenance import Maintenance
from .opds import install_opds
from .organization import Organization
from .organization_api import organization_router
from .page_features import PageFeatures
from .removals import Removals
from .review import Review
from .scanning import Scanner
from .services import Services, require_manager
from .sidecars import SidecarSync
from .source_api import install_source_routes
from .warming import Warmer

__all__ = ["HTTPSettings", "create_app"]

MAX_JSON_BYTES = 512 * 1024
LOGGER = logging.getLogger(__name__)
# DB-only reads that keep working while the library root is unavailable.
ROOT_EXEMPT_PREFIXES = (
    "/api/auth/",
    "/api/scans",
    "/api/removals",
    "/api/sources/",
    "/api/downloads",
    "/api/snapshots/",
)
ROOT_EXEMPT_PATHS = {
    "/api/tasks",
    "/api/source-checks",
    "/api/cache",
    "/api/diagnostics",
    "/api/snapshots",
    "/api/settings",
    "/api/settings/summary",
    "/api/preferences",
    "/api/sidecar/status",
    "/api/translations",
    "/api/tag-aliases",
    "/api/duplicates",
}
DOWNLOAD_KEY_ROUTES = re.compile(r"POST /api/downloads|GET /api/auth/me")
HTTP_CODES = {
    400: "bad_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    408: "request_timeout",
    413: "body_too_large",
    422: "invalid_input",
    503: "unavailable",
}
# The message of a code raised without one; any other code falls back to the HTTP status phrase.
MESSAGES = {
    "invalid_host": "The Host header names a host this server does not accept",
    "invalid_credentials": "Invalid or expired credentials",
    "cross_site_request": "Cross-site requests are not accepted",
    "same_origin_required": "Requests must come from this site's own pages",
    "read_only": "This key is read-only",
    "key_scope": "This key is not allowed to use this route",
    "root_unavailable": "The library root is unavailable",
    "storage_unavailable": "Library storage is unavailable",
    "database_busy": "The library database is busy; retry shortly",
    "insufficient_storage": "The server is out of disk space",
    "body_too_large": "The request body is too large",
    "body_timeout": "The request body did not arrive in time",
    "invalid_input": "Invalid input",
    "source_unavailable": "The source could not be reached",
    "internal_error": "Internal server error",
}


def error_response(
    status: int, code: str, message: str | None = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    """The one error body: `{"error": code, "message": text}`."""
    text = message or MESSAGES.get(code) or HTTPStatus(status).phrase
    return JSONResponse({"error": code, "message": text[:500]}, status_code=status, headers=headers)


class AccessLog:
    """Debug-level line per request: method, path (without the query, which holds search terms), status,
    time. uvicorn's own access log stays off."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not LOGGER.isEnabledFor(logging.DEBUG):
            await self.app(scope, receive, send)
            return
        started, status = time.monotonic(), 0

        async def record(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record)
        finally:
            LOGGER.debug(
                "%s %s -> %s (%.0f ms)",
                scope["method"],
                scope["path"],
                status or "aborted",
                (time.monotonic() - started) * 1000,
            )


class Unhandled:
    """Answer an unexpected exception with a logged `internal_error`, inside the boundary so the
    response keeps its security headers. Starlette's outermost handler would also re-raise it, and
    the server would log the same traceback a second time."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def record(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, receive, record)
        except Exception as error:
            if started:
                raise  # too late for an error response; the server logs it and drops the connection
            LOGGER.error("Unhandled error on %s %s", scope["method"], scope["path"], exc_info=error)
            await error_response(500, "internal_error")(scope, receive, send)


class Boundary:
    def __init__(self, app: ASGIApp, *, auth: Auth, files: FileLibrary, settings: HTTPSettings):
        self.app, self.auth, self.files, self.settings = app, auth, files, settings
        self.rejected_hosts: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope, receive)

        async def headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                values = list(message.get("headers", []))
                if not any(name.lower() == b"cache-control" for name, _ in values):
                    values.append(
                        (
                            b"cache-control",
                            b"public, max-age=31536000, immutable"
                            if 200 <= message["status"] < 300
                            and re.fullmatch(r"/assets/[^/]+-[A-Za-z0-9_-]+\.(js|css)", request.url.path)
                            else b"no-store",
                        )
                    )
                values.extend(
                    [
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                            b"img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; "
                            b"base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                        ),
                    ]
                )
                message["headers"] = values
            await send(message)

        async def reject(status: int, code: str) -> None:
            await error_response(
                status,
                code,
                headers=(
                    {"WWW-Authenticate": 'Basic realm="MangaCouch", charset="UTF-8"'}
                    if status == 401 and request.url.path.startswith(("/opds", "/komga/"))
                    else {"Retry-After": "2"}
                    if code == "database_busy"
                    else None
                ),
            )(scope, receive, headers)

        started = False
        original_headers = headers

        async def record(message: Message) -> None:
            nonlocal started
            started = started or message["type"] == "http.response.start"
            await original_headers(message)

        headers = record
        try:
            authority = request.headers.get("host", "").lower()
            if len(request.headers.getlist("host")) != 1 or not self.settings.accepts_host(authority):
                if authority not in self.rejected_hosts and len(self.rejected_hosts) < 100:
                    # Once per host: usually a missing MANGACOUCH_ALLOWED_HOSTS / --allowed-host entry.
                    self.rejected_hosts.add(authority)
                    LOGGER.warning("Rejected request for Host %r: not an allowed host", authority[:255])
                await reject(400, "invalid_host")
                return
            if request.method in {"GET", "HEAD"} and (
                request.url.path == "/" or request.url.path.startswith("/assets/")
            ):
                await self.app(scope, receive, headers)
                return
            origin = request.headers.get("origin")
            cookie_name = self.settings.session_cookie(authority, request.url.scheme, origin, request.cookies)
            scheme = self.settings.request_scheme(authority, request.url.scheme, origin)
            login = request.url.path == "/api/auth/login" and request.method == "POST"
            authorization = request.headers.get("authorization")
            if (
                authorization
                and authorization.startswith("Basic ")
                and not request.url.path.startswith(("/opds", "/komga/"))
                and request.cookies.get(cookie_name)
            ):
                try:
                    await anyio.to_thread.run_sync(
                        lambda: self.auth.authenticate(request.cookies[cookie_name], session=True)
                    )
                    authorization = None
                except Unauthorized:
                    pass
                except (OSError, sqlite3.Error) as error:
                    await reject(503, "database_busy" if busy(error) else "storage_unavailable")
                    return
            cookie_auth = login or authorization is None
            if len(request.headers.getlist("authorization")) > 1:
                await reject(401, "invalid_credentials")
                return
            challenge_basic = (
                authorization is None
                and request.url.path.startswith("/komga/")
                and not request.cookies.get(cookie_name)
            )
            if cookie_auth and not challenge_basic:
                if request.headers.get("sec-fetch-site") not in {None, "same-origin", "none"}:
                    await reject(403, "cross_site_request")
                    return
                if request.method not in {"GET", "HEAD"} and (
                    origin != f"{scheme}://{authority}" or request.headers.get("x-mangacouch-request") != "1"
                ):
                    await reject(403, "same_origin_required")
                    return
            if not login:
                try:
                    if authorization is not None:
                        if authorization.startswith("Basic ") and (
                            request.url.path == "/opds" or request.url.path.startswith(("/opds/", "/komga/"))
                        ):
                            try:
                                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                                _, separator, token = decoded.partition(":")
                                if (
                                    not separator
                                    or not token
                                    or (
                                        not request.url.path.startswith("/komga/")
                                        and request.method not in {"GET", "HEAD"}
                                    )
                                ):
                                    raise Unauthorized("Invalid credentials")
                            except ValueError:
                                raise Unauthorized("Invalid credentials") from None
                        elif authorization.startswith("Bearer "):
                            token = authorization[7:]
                        else:
                            raise Unauthorized("Invalid credentials")
                    else:
                        token = request.cookies.get(cookie_name, "")
                    principal = await anyio.to_thread.run_sync(
                        lambda: self.auth.authenticate(token, session=cookie_auth)
                    )
                except Unauthorized:
                    await reject(401, "invalid_credentials")
                    return
                except (OSError, sqlite3.Error) as error:
                    await reject(503, "database_busy" if busy(error) else "storage_unavailable")
                    return
                request.state.principal = principal
                if principal.kind == "readonly" and request.method not in {"GET", "HEAD"}:
                    await reject(403, "read_only")
                    return
                if principal.kind == "downloads" and not DOWNLOAD_KEY_ROUTES.fullmatch(
                    f"{request.method} {request.url.path}"
                ):
                    await reject(403, "key_scope")
                    return
                path = request.url.path
                if not path.startswith(ROOT_EXEMPT_PREFIXES) and path not in ROOT_EXEMPT_PATHS:
                    # A cheap cached identity check; file mutations probe writability themselves.
                    try:
                        await anyio.to_thread.run_sync(self.files.root.check)
                    except RootUnavailable:
                        await reject(503, "root_unavailable")
                        return
            if request.url.path != "/api/import" and request.method not in {"GET", "HEAD"}:
                body_limit = 8 * 1024**2 if request.url.path == "/api/bulk-selection" else MAX_JSON_BYTES
                payload = bytearray()
                try:
                    with anyio.fail_after(30):
                        async for chunk in request.stream():
                            if len(payload) + len(chunk) > body_limit:
                                await reject(413, "body_too_large")
                                return
                            payload.extend(chunk)
                except ClientDisconnect:
                    return
                except TimeoutError:
                    await reject(408, "body_timeout")
                    return
                used = False

                async def replay() -> Message:
                    nonlocal used
                    if not used:
                        used = True
                        return {"type": "http.request", "body": bytes(payload), "more_body": False}
                    return await receive()

                await self.app(scope, replay, headers)
            else:
                await self.app(scope, receive, headers)
        except Exception as error:
            if started:
                raise
            LOGGER.error("Unhandled boundary error on %s %s", scope["method"], scope["path"], exc_info=error)
            await error_response(500, "internal_error")(scope, receive, headers)


def install_error_handlers(app: FastAPI) -> None:
    """Map domain exceptions to stable codes; anything unexpected is a logged 500."""

    async def domain(_request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, LibraryBusy):
            return error_response(503, error.code, error.message, {"Retry-After": "5"})
        if isinstance(error, RateLimited):
            return error_response(429, error.code, error.message, {"Retry-After": str(error.retry_after)})
        if isinstance(error, NotFound):
            return error_response(404, error.code, error.message)
        if isinstance(error, RevisionConflict):
            return JSONResponse(
                {
                    "error": error.code,
                    "message": error.message,
                    **({"conflict": error.conflict} if error.conflict else {}),
                },
                status_code=409,
            )
        if isinstance(error, InvalidInput):
            return error_response(422, error.code, error.message)
        if isinstance(error, Unauthorized):
            return error_response(401, "invalid_credentials")
        return error_response(409, "content_changed", str(error))

    async def validation(_request: Request, error: Exception) -> JSONResponse:
        # Only request validation belongs to a 422. Invalid persisted state remains a server error.
        problems = error.errors() if isinstance(error, RequestValidationError) else []
        message = None
        if problems:
            loc = problems[0].get("loc", ())
            location = ".".join(str(part) for part in loc[1:])
            message = f"{location}: {problems[0].get('msg', '')}" if location else problems[0].get("msg")
        return error_response(422, "invalid_input", message)

    async def http_error(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, StarletteHTTPException):
            raise error
        fallback = "internal_error" if error.status_code >= 500 else "bad_request"
        code = HTTP_CODES.get(error.status_code, fallback)
        detail = error.detail if isinstance(error.detail, str) and error.detail != code else None
        return error_response(error.status_code, code, detail, dict(error.headers or {}))

    async def network(_request: Request, _error: Exception) -> JSONResponse:
        return error_response(502, "source_unavailable")

    async def disconnected(_request: Request, _error: Exception) -> Response:
        return Response(status_code=499)

    async def storage(request: Request, error: Exception) -> JSONResponse:
        if isinstance(error, RootUnavailable):
            return error_response(503, "root_unavailable", str(error))
        if isinstance(error, OSError):
            LOGGER.warning("Storage request failed: %s", type(error).__name__, exc_info=error)
            if error.errno in {errno.ENOSPC, errno.EDQUOT}:
                return error_response(507, "insufficient_storage")
            return error_response(503, "storage_unavailable")
        if busy(error):
            LOGGER.warning("Database busy on %s %s", request.method, request.url.path)
            return error_response(503, "database_busy", headers={"Retry-After": "2"})
        LOGGER.error("Database request failed", exc_info=error)
        sqlite_code = getattr(error, "sqlite_errorcode", 0) & 0xFF
        if sqlite_code == sqlite3.SQLITE_FULL:
            return error_response(507, "insufficient_storage")
        if sqlite_code in {
            sqlite3.SQLITE_READONLY,
            sqlite3.SQLITE_IOERR,
            sqlite3.SQLITE_CORRUPT,
            sqlite3.SQLITE_CANTOPEN,
            sqlite3.SQLITE_NOTADB,
        }:
            return error_response(503, "storage_unavailable")
        return error_response(500, "internal_error")

    for error_type in (
        NotFound,
        InvalidInput,
        RevisionConflict,
        RateLimited,
        LibraryBusy,
        Unauthorized,
        ContentChanged,
    ):
        app.add_exception_handler(error_type, domain)
    app.add_exception_handler(RequestValidationError, validation)
    app.add_exception_handler(StarletteHTTPException, http_error)
    app.add_exception_handler(httpx.HTTPError, network)
    app.add_exception_handler(ClientDisconnect, disconnected)
    app.add_exception_handler(OSError, storage)
    app.add_exception_handler(sqlite3.Error, storage)


def create_app(
    files: FileLibrary,
    settings: HTTPSettings | None = None,
    *,
    auth: Auth | None = None,
    thumbnail_cache: ImageCache | None = None,
    downloads: Downloads | None = None,
) -> FastAPI:
    settings = settings or load_http_settings(files.library)
    auth = auth or Auth(files.library)
    if not auth.configured():
        raise InvalidInput("Initialize a passcode locally before starting HTTP")
    downloads = downloads or Downloads(files)
    thumbnail_cache = thumbnail_cache or ImageCache(
        files.library.path.parent / f".mangacouch-cache-{files.library.path.name}",
        disk_bytes=settings.thumbnail_cache_bytes,
    )
    warmers = {kind: Warmer(files, thumbnail_cache, kind) for kind in ("covers", "thumbnails")}
    organization = Organization(files.library)
    scanner = Scanner(files)
    removals = Removals(files)
    duplicates = Duplicates(PageFeatures(files))
    eze_cache = EzeCache(files)
    review = Review(files)
    sync = SidecarSync(files)

    def scan_step() -> bool:
        current = scanner.summary()
        if current is None or (
            current["state"] != "active" and not scanner.resume_storage_wait(current["id"])
        ):
            return False
        scanner.step(current["id"], limit=10)
        duplicates.poke()
        return True

    def warm_covers() -> bool:
        return warmers["covers"].step()

    def warm_thumbnails() -> bool:
        # Covers first: the shelf needs them, the page lists can wait.
        return not warmers["covers"].active() and warmers["thumbnails"].step()

    def sync_eze() -> bool:
        return bool(eze_cache.sync_pending())

    def sidecar_pending() -> bool:
        return bool(sync.sync_pending())

    def refresh_source_checks() -> None:
        if downloads.checks.schedule_refresh():
            downloads.wake.set()

    maintenance = Maintenance(
        steps=[
            scan_step,
            organization.advance,
            removals.finish_pending,
            sidecar_pending,
            sync_eze,
            duplicates.run,
            warm_covers,
            warm_thumbnails,
        ],
        hourly=[
            removals.purge,
            organization.purge,
            files.purge_imports,
            files.recover_ready,
            files.recheck_unavailable,
            scanner.purge,
            auth.purge,
            downloads.purge,
            review.purge,
            sync.purge,
            refresh_source_checks,
        ],
    )
    services = Services(
        files=files,
        settings=settings,
        auth=auth,
        downloads=downloads,
        thumbnail_cache=thumbnail_cache,
        warmers=warmers,
        review=review,
        sync=sync,
        collections=Collections(files.library),
        organization=organization,
        scanner=scanner,
        removals=removals,
        duplicates=duplicates,
        editions=Editions(files.library),
        maintenance=maintenance,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Takes the worker lock, then recovers interrupted imports before any worker runs.
        files.library.keep_alive()
        try:
            await anyio.to_thread.run_sync(downloads.start)
            maintenance.start()
            yield
        finally:
            scanner.stopping = True

            # Every step runs even when an earlier one fails, so caches are flushed and locks released.
            async def close(name: str, function) -> None:
                try:
                    await anyio.to_thread.run_sync(function)
                except Exception:
                    LOGGER.exception("Shutdown of the %s failed", name)

            # Side by side, so their 30 s stop timeouts do not add up.
            async with anyio.create_task_group() as group:
                group.start_soon(close, "maintenance", maintenance.close)
                group.start_soon(close, "downloads", downloads.close)
            await close("thumbnail cache", thumbnail_cache.close)
            await close("page cache", files.page_cache.close)

            def optimize() -> None:
                with files.library.connection() as db:
                    db.execute("PRAGMA optimize")

            await close("query planner", optimize)
            files.library.close_keep_alive()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.add_middleware(Unhandled)
    app.add_middleware(Boundary, auth=auth, files=files, settings=settings)
    app.add_middleware(AccessLog)
    install_error_handlers(app)

    @app.api_route("/", methods=["GET", "HEAD"])
    def shell() -> FileResponse:
        if not (WEB_DIR / "index.html").is_file():
            raise HTTPException(503, "Build the web frontend before opening the library")
        return FileResponse(WEB_DIR / "index.html", media_type="text/html")

    if (WEB_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="web-assets")

    for router in (
        auth_router,
        settings_router,
        library_router,
        review_router,
        collections_router,
        organization_router,
        scans_router,
        images_router,
        removals_router,
    ):
        app.include_router(router(services))
    install_source_routes(
        app,
        downloads,
        require_manager,
        services.book_output,
        scanner=scanner,
        bounded=services.bounded,
        extras=lambda: {kind: warmer.summary() for kind, warmer in warmers.items()},
    )
    origin = settings.public_origin
    install_opds(
        app,
        files,
        origin,
        thumbnail_cache,
        services.file_limiter,
        services.cover_limiter,
        prefix="/komga/opds/v1.2",
        komga=True,
    )
    install_opds(app, files, origin, thumbnail_cache, services.file_limiter, services.cover_limiter)
    install_komga(app, files, thumbnail_cache, services.file_limiter, services.cover_limiter)
    return app
