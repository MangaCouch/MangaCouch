from __future__ import annotations

from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr

from .downloads import BATCH_LIMIT, Downloads
from .errors import InvalidInput
from .models import Model
from .scanning import Scanner
from .sidecars import SidecarSync
from .source_config import SourceSettings
from .source_http import SourceError, gallery_url

# Source failures as HTTP responses: (status, message). Unknown codes are 502 upstream failures.
SOURCE_ERRORS: dict[str, tuple[int, str]] = {
    "source_rate_limited": (429, "The source is rate limiting requests; wait and retry"),
    "source_login_required": (409, "The source requires valid login cookies"),
    "nh_key_required": (409, "An nHentai API key is required"),
    "verification_required": (409, "The source asks for a browser verification"),
    "insufficient_gp": (409, "Not enough GP for this archive"),
    "source_price_changed": (409, "The archive price changed; approve again"),
    "remote_result_unknown": (409, "The archive request result is unknown"),
    "source_link_expired": (409, "The archive link expired"),
    "unsafe_source_url": (422, "The source returned a link that is not allowed"),
    "source_title_missing": (422, "The gallery has no title to search for"),
    "download_too_large": (422, "The archive exceeds the download size limit"),
    "gallery_unavailable": (404, "The gallery is no longer available"),
    "source_unavailable": (503, "The source is unavailable"),
    "archive_preparing": (503, "The source is still preparing the archive"),
    "source_network": (502, "The source could not be reached"),
}


class SourceConfigInput(Model):
    expected_revision: Annotated[int, Field(ge=0)]
    settings: SourceSettings
    secrets: dict[str, SecretStr] = Field(default_factory=dict)


class DownloadCreate(Model):
    url: Annotated[str, Field(min_length=1, max_length=2048)]
    entry: Literal["find_eh", "nh_direct"] = "find_eh"


class DownloadBatch(Model):
    urls: Annotated[list[Annotated[str, Field(max_length=2048)]], Field(min_length=1, max_length=BATCH_LIMIT)]
    entry: Literal["find_eh", "nh_direct"] = "find_eh"


class DownloadAction(Model):
    revision: Annotated[int, Field(ge=1)]
    action: Literal["choose", "approve", "resume", "recover", "cancel", "skip", "search", "pause"]
    url: Annotated[str, Field(max_length=2048)] | None = None
    mode: Literal["org", "res"] = "org"


class OnlineInput(Model):
    url: Annotated[str, Field(max_length=2048)]
    expected_revision: Annotated[int, Field(ge=1)]


class CommentsInput(Model):
    url: Annotated[str, Field(max_length=2048)]


def _source(url: str):
    try:
        return gallery_url(url)
    except ValueError:
        raise InvalidInput(
            "Expected an EH, ExHentai or nHentai gallery URL", code="unsupported_source"
        ) from None


def install_source_routes(app: FastAPI, downloads: Downloads, manager, book_output):
    @app.exception_handler(SourceError)
    async def source_error(_request: Request, error: SourceError):
        status, message = SOURCE_ERRORS.get(error.code, (502, "The source request failed"))
        code = "rate_limited" if error.code == "source_rate_limited" else error.code
        return JSONResponse(
            {"error": code, "message": message},
            status_code=status,
            headers={"Retry-After": str(error.retry_after)} if error.retry_after and status >= 429 else None,
        )

    def public() -> dict:
        return {**downloads.vault.public(), "cooldowns": downloads.http.cooldowns()}

    @app.get("/api/sources/settings")
    def settings(request: Request):
        manager(request)
        return public()

    @app.put("/api/sources/settings")
    def save(request: Request, data: SourceConfigInput):
        manager(request)
        downloads.save_settings(
            data.settings,
            {key: value.get_secret_value() for key, value in data.secrets.items()},
            data.expected_revision,
        )
        return public()

    @app.post("/api/sources/verify/{site}")
    def verify_source(request: Request, site: Literal["ehentai", "exhentai", "nhentai"]):
        manager(request)
        return downloads.sources.verify(site)

    @app.get("/api/downloads")
    def listing(
        request: Request,
        before: int | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        scope: Literal["all", "active", "history"] = "all",
    ):
        manager(request)
        return downloads.list(before, limit, scope)

    @app.post("/api/downloads")
    def create(request: Request, data: DownloadCreate):
        manager(request)
        return downloads.create(data.url, data.entry)

    @app.post("/api/downloads/batch")
    def create_batch(request: Request, data: DownloadBatch):
        manager(request)
        return {"items": downloads.create_many(data.urls, data.entry)}

    @app.get("/api/tasks")
    def task_summary(request: Request):
        manager(request)
        scan = Scanner(downloads.files).summary()
        with downloads.library.connection() as db:
            checks = dict(db.execute("SELECT state,count(*) FROM source_check GROUP BY state").fetchall())
        return {
            "downloads": downloads.summary(),
            "scan": scan,
            "source_checks": checks,
            "cooldowns": downloads.http.cooldowns(),
        }

    @app.get("/api/downloads/{identifier}")
    def get(request: Request, identifier: str):
        manager(request)
        return downloads.get(identifier)

    @app.post("/api/downloads/{identifier}/action")
    def act(request: Request, identifier: str, data: DownloadAction):
        manager(request)
        return downloads.action(identifier, data.action, data.revision, url=data.url, mode=data.mode)

    @app.get("/api/downloads/{identifier}/cover/{index}")
    def cover(request: Request, identifier: str, index: int):
        manager(request)
        payload, media_type = downloads.cover(identifier, index)
        return Response(payload, media_type=media_type, headers={"Cache-Control": "private,max-age=300"})

    @app.get("/api/books/{book_id}/source-comments")
    def comments(request: Request, book_id: str):
        manager(request)
        return downloads.online.notes(book_id)

    @app.post("/api/books/{book_id}/source-comments")
    def refresh_comments(request: Request, book_id: str, data: CommentsInput):
        manager(request)
        return downloads.online.refresh_comments(book_id, _source(data.url))

    @app.post("/api/books/{book_id}/source")
    def refresh(request: Request, book_id: str, data: OnlineInput):
        manager(request)
        result = downloads.online.refresh(
            book_id, _source(data.url), data.expected_revision, link_source=True
        )
        # Same DB-first path as local edits; conflicts remain reviewable.
        sync = SidecarSync(downloads.files).sync_one(book_id)
        return {**result, "book": book_output(downloads.library.get_book(book_id)), "sync": sync}
