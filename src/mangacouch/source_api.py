from __future__ import annotations

import time
from contextlib import suppress
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from .database import busy
from .downloads import BATCH_LIMIT, Downloads
from .errors import InvalidInput, LibraryBusy, NotFound, RevisionConflict
from .models import Model
from .scanning import Scanner
from .source_config import SourceSettings
from .source_http import SourceError, gallery_url

# Source failures as HTTP responses: (status, message). Unknown codes are 502 upstream failures.
SOURCE_ERRORS: dict[str, tuple[int, str]] = {
    "source_restore_paused": (
        409,
        "Online sources are paused after restoring a backup; resume them in settings",
    ),
    "source_rate_limited": (
        429,
        "Requests to the source are halted after a rate limit; retry once the halt ends",
    ),
    "source_login_required": (409, "The source requires valid login cookies"),
    "unsupported_source": (422, "Expected an E-Hentai or ExHentai gallery URL"),
    "verification_required": (409, "The source asks for a browser verification"),
    "unsafe_source_url": (422, "The source returned a link that is not allowed"),
    "source_private_address": (
        409,
        "The source host resolved to a private network address; set a proxy URL, or allow "
        "fake-IP DNS answers if a TUN proxy is in use",
    ),
    "gallery_unavailable": (404, "The gallery is no longer available"),
    "source_unavailable": (502, "The source is unavailable"),
    "source_network": (502, "The source could not be reached"),
    "source_timeout": (504, "The source request timed out; try again"),
    "source_key_lost": (
        409,
        "Source credentials cannot be read with this key; reset them in the source settings",
    ),
}


DEFERRED = frozenset({"source_rate_limited", "source_timeout"})
"""Errors that put a refresh or comment fetch asked for by hand into the background (202): the
budget is short, or the request's own deadline ran out while it waited or read."""


def request_deadline():
    until = time.monotonic() + 20

    def cancelled():
        if time.monotonic() >= until:
            raise SourceError("source_timeout")
        return False

    return cancelled


class SourceConfigInput(Model):
    settings: SourceSettings
    secrets: dict[str, SecretStr] = Field(default_factory=dict)


class VerifyInput(Model):
    secrets: dict[str, SecretStr] = Field(default_factory=dict)
    """Typed, unsaved credentials to check with instead of the stored ones; never stored."""


class DownloadCreate(Model):
    url: Annotated[str, Field(min_length=1, max_length=2048)]


class DownloadBatch(Model):
    urls: Annotated[list[Annotated[str, Field(max_length=2048)]], Field(min_length=1, max_length=BATCH_LIMIT)]


class DownloadAction(Model):
    action: Literal["approve", "resume", "recover", "cancel", "pause", "redownload"]
    mode: Literal["org", "res", "reuse"] = "org"


class DownloadBatchItem(Model):
    id: Annotated[str, Field(min_length=1, max_length=100)]
    expected_revision: Annotated[int, Field(ge=1)]
    mode: Literal["org", "res", "reuse"] = "org"


class DownloadActions(Model):
    action: Literal["approve", "cancel", "pause", "resume", "delete", "redownload"]
    items: Annotated[list[DownloadBatchItem], Field(min_length=1, max_length=500)]


class OnlineInput(Model):
    url: Annotated[str, Field(max_length=2048)]


class CommentsInput(Model):
    url: Annotated[str, Field(max_length=2048)]


class CheckAction(Model):
    action: Literal["retry"]


def _source(url: str):
    try:
        return gallery_url(url)
    except ValueError:
        raise InvalidInput(
            "Expected an E-Hentai or ExHentai gallery URL", code="unsupported_source"
        ) from None


def install_source_routes(
    app: FastAPI, downloads: Downloads, manager, book_output, *, scanner: Scanner, bounded, extras=None
):
    """`extras()` adds other background work (cover warm-up) to the task summary."""

    @app.exception_handler(SourceError)
    async def source_error(_request: Request, error: SourceError):
        status, message = SOURCE_ERRORS.get(error.code, (502, "The source request failed"))
        body: dict = {"error": error.code, "message": message}
        if error.code == "source_rate_limited" and error.retry_after:
            body["retry_at"] = int(time.time()) + error.retry_after
        return JSONResponse(
            body,
            status_code=status,
            headers={"Retry-After": str(error.retry_after)} if error.retry_after and status >= 429 else None,
        )

    def usage() -> dict:
        return {"budget": downloads.http.budget(), "cooldowns": downloads.http.cooldowns()}

    def public() -> dict:
        with downloads.library.connection() as db:
            account = db.execute(
                "SELECT balance_gp FROM source_account WHERE site_group='ehentai'"
            ).fetchone()
        return {**downloads.vault.public(), **usage(), "balance_gp": account[0] if account else None}

    @app.get("/api/sources/settings")
    def settings(request: Request):
        manager(request)
        return public()

    @app.get("/api/sources/budget")
    def budget(request: Request):
        """The request budgets and halts as they stand, for the settings page to refresh."""
        manager(request)
        return usage()

    @app.put("/api/sources/settings")
    def save(request: Request, expected_revision: Annotated[int, Query(ge=0)], data: SourceConfigInput):
        manager(request)
        downloads.save_settings(
            data.settings,
            {key: value.get_secret_value() for key, value in data.secrets.items()},
            expected_revision,
        )
        return public()

    @app.post("/api/sources/reset-credentials")
    async def reset_credentials(request: Request):
        """Discard the stored credentials when their key is lost; online sources work again."""
        manager(request)
        return {**(await bounded(downloads.reset_credentials)), **usage()}

    @app.post("/api/source-checks/retry-failed")
    async def retry_checks(request: Request):
        manager(request)
        count = await bounded(downloads.checks.retry_failed)
        downloads.wake.set()
        return {"queued": count}

    @app.get("/api/source-checks")
    def source_checks(
        request: Request,
        page: Annotated[int, Query(ge=1, le=1_000_000)] = 1,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
    ):
        """Books whose source check needs review, latest first."""
        manager(request)
        identifiers, total = downloads.checks.attention(page, limit)
        items = []
        for identifier in identifiers:
            try:
                items.append(book_output(downloads.library.get_book(identifier)))
            except NotFound:
                total -= 1  # removed meanwhile
        return {"items": items, "total": total}

    @app.post("/api/books/{book_id}/source-check")
    def check_action(
        request: Request, book_id: str, expected_generation: Annotated[int, Query(ge=1)], data: CheckAction
    ):
        """Retry a book's source check; the check as it is now."""
        manager(request)
        result = downloads.checks.action(book_id, data.action, expected_generation)
        downloads.wake.set()
        return result

    @app.post("/api/sources/verify/{site}")
    def verify_source(
        request: Request, site: Literal["ehentai", "exhentai"], data: VerifyInput | None = None
    ):
        manager(request)
        typed = data.secrets if data else {}
        return downloads.verify(
            site,
            {key: value.get_secret_value() for key, value in typed.items()},
            cancelled=request_deadline(),
        )

    @app.get("/api/downloads")
    def listing(
        request: Request,
        after: str = "",
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        scope: Literal["all", "active", "history"] = "all",
        page: Annotated[int | None, Query(ge=1)] = None,
        through: Annotated[int | None, Query(ge=0)] = None,
        focus: str = "",
    ):
        manager(request)
        return downloads.list(after, limit, scope, page=page, through=through, focus=focus)

    @app.post("/api/downloads")
    def create(request: Request, data: DownloadCreate):
        manager(request)
        return downloads.create(data.url)

    @app.post("/api/downloads/batch")
    def create_batch(request: Request, data: DownloadBatch):
        manager(request)
        return {"items": downloads.create_many(data.urls)}

    @app.post("/api/downloads/actions")
    async def batch_actions(request: Request, data: DownloadActions):
        manager(request)
        results = []
        for item in data.items:

            def execute(item=item):
                if data.action == "delete":
                    downloads.delete(item.id, expected_revision=item.expected_revision)
                    return None
                return downloads.action(
                    item.id, data.action, item.expected_revision, mode=item.mode, strict_revision=True
                )

            try:
                job = await bounded(execute)
                results.append({"id": item.id, "ok": True, "job": job})
            except Exception as error:
                if busy(error):
                    code = "database_busy"
                elif isinstance(error, (InvalidInput, NotFound, RevisionConflict, SourceError, LibraryBusy)):
                    code = error.code
                else:
                    raise
                latest = None
                if isinstance(error, RevisionConflict):
                    with suppress(NotFound):
                        latest = downloads.get(item.id)
                results.append(
                    {
                        "id": item.id,
                        "ok": False,
                        "job": latest,
                        "error": code,
                    }
                )
        return {"results": results}

    @app.post("/api/sources/balance")
    def balance(request: Request):
        manager(request)
        return downloads._gp_probe(force=True, cancelled=request_deadline())

    @app.get("/api/tasks")
    def task_summary(request: Request):
        manager(request)
        scan = scanner.summary()
        with downloads.library.connection() as db:
            checks = dict(db.execute("SELECT state,count(*) FROM source_check GROUP BY state").fetchall())
        return {
            "downloads": downloads.summary(),
            "scan": scan,
            "source_checks": checks,
            "cooldowns": downloads.http.cooldowns(),
            **(extras() if extras else {}),
        }

    @app.get("/api/downloads/{identifier}")
    def get(request: Request, identifier: str):
        manager(request)
        return downloads.get(identifier)

    @app.delete("/api/downloads/{identifier}", status_code=204)
    async def delete(request: Request, identifier: str) -> None:
        manager(request)
        await bounded(lambda: downloads.delete(identifier))

    @app.post("/api/downloads/{identifier}/action")
    async def act(
        request: Request,
        identifier: str,
        expected_revision: Annotated[int, Query(ge=1)],
        data: DownloadAction,
    ):
        manager(request)
        return await bounded(
            lambda: downloads.action(identifier, data.action, expected_revision, mode=data.mode)
        )

    @app.get("/api/books/{book_id}/source-comments")
    def comments(request: Request, book_id: str):
        manager(request)
        return downloads.online.notes(book_id)

    @app.post("/api/books/{book_id}/source-comments")
    def refresh_comments(request: Request, response: Response, book_id: str, data: CommentsInput):
        """Fresh comments, or 202 with `retry_at` when a rate limit or the request deadline put
        the fetch off: it then runs in the background by itself."""
        manager(request)
        source = _source(data.url)
        try:
            return downloads.online.refresh_comments(book_id, source, cancelled=request_deadline())
        except SourceError as error:
            if error.code not in DEFERRED:
                raise
            retry_at = downloads.online.defer(book_id, source, "comments", error.retry_after)
        response.status_code = 202
        return {**downloads.online.notes(book_id), "comments_error": None, "retry_at": retry_at}

    @app.post("/api/books/{book_id}/source")
    def refresh(
        request: Request,
        response: Response,
        book_id: str,
        expected_revision: Annotated[int, Query(ge=1)],
        data: OnlineInput,
    ):
        """Refresh a book from a source link, or 202 with `retry_at` when a rate limit or the
        request deadline put it off: the refresh then runs in the background by itself, on the
        book as it is by then."""
        manager(request)
        source = _source(data.url)
        try:
            result = downloads.online.refresh(
                book_id, source, expected_revision, link_source=True, cancelled=request_deadline()
            )
        except SourceError as error:
            if error.code not in DEFERRED:
                raise
            retry_at = downloads.online.defer(book_id, source, "metadata", error.retry_after)
            response.status_code = 202
            return {
                "comments_error": None,
                "retry_at": retry_at,
                "book": book_output(downloads.library.get_book(book_id)),
            }
        # Same DB-first path as local edits; conflicts remain reviewable.
        sync = downloads.sync.publish(book_id)
        return {**result, "book": book_output(downloads.library.get_book(book_id)), "sync": sync}
