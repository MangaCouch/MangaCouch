from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import Field

from .archives import image_media_type
from .errors import InvalidInput, NotFound, RevisionConflict
from .exports import ArchiveResponse
from .models import Model
from .preferences import load_preferences, preferred_title
from .searching import SearchOptions, rule_conditions
from .services import CLIENT_IMAGE, image_response

EPOCH = "1970-01-01T00:00:00Z"
LIBRARY = "mangacouch"
# Komga clients gate features on the server major version; 1.x is the API subset implemented here.
KOMGA_VERSION = "1.0.0"
TITLE_SORT = (
    "coalesce(json_extract(b.metadata_json,'$.title'),json_extract(b.content_json,'$.original_filename')) "
    "COLLATE NOCASE"
)
ACTIVITY = "coalesce((SELECT updated_at FROM reading_activity WHERE book_id=b.id),0)"
SORT_FIELDS = {
    "metadata.titleSort": TITLE_SORT,
    "metadata.title": TITLE_SORT,
    "name": "json_extract(b.content_json,'$.original_filename') COLLATE NOCASE",
    "readProgress.readDate": ACTIVITY,
    "lastModified": ACTIVITY,
    "lastModifiedDate": ACTIVITY,
    "createdDate": "b.rowid",
    "created": "b.rowid",
}
READ_STATUS = {
    "READ": "json_extract(p.state_json,'$.completed')=1",
    "UNREAD": (
        "json_extract(p.state_json,'$.resume_page') IS NULL AND json_extract(p.state_json,'$.completed')=0"
    ),
    "IN_PROGRESS": (
        "json_extract(p.state_json,'$.resume_page') IS NOT NULL "
        "AND json_extract(p.state_json,'$.completed')=0"
    ),
}


class ProgressInput(Model):
    page: Annotated[int, Field(ge=1)] | None = None
    completed: bool | None = None
    deviceId: Annotated[str, Field(max_length=256)] | None = None
    deviceName: Annotated[str, Field(max_length=256)] | None = None


def install_komga(app: FastAPI, files, cache, limiter, manager):
    router = APIRouter(prefix="/komga")
    library = files.library

    def title(book, preference=None):
        return preferred_title(book, preference or load_preferences(library).settings.title_preference)

    def book_dto(book, request, db=None, preference=None):
        if db is None:
            with library.transaction() as connection:
                return book_dto(book, request, connection, preference)
        activity = db.execute(
            "SELECT updated_at FROM reading_activity WHERE book_id=?", (book.id,)
        ).fetchone()
        if request.state.principal.kind != "readonly":
            db.execute(
                "INSERT INTO client_observation VALUES(?,?,?) ON CONFLICT(credential_id,book_id) "
                "DO UPDATE SET revision=excluded.revision",
                (request.state.principal.id, book.id, book.personal_revision),
            )
        date = datetime.fromtimestamp(activity[0], UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if activity else EPOCH
        progress = None
        if book.personal.resume_page is not None or book.personal.completed:
            progress = {
                "page": book.personal.resume_page + 1
                if book.personal.resume_page is not None
                else book.content.page_count,
                "completed": book.personal.completed,
                "readDate": date,
                "created": date,
                "lastModified": date,
                "deviceId": "mangacouch",
                "deviceName": "MangaCouch",
            }
        tags = [f"{tag.namespace}:{tag.value}" for tag in book.metadata.tags]
        name = title(book, preference)
        return {
            "id": book.id,
            "seriesId": book.id,
            "seriesTitle": name,
            "libraryId": LIBRARY,
            "name": book.content.original_filename,
            "url": book.content.original_filename,
            "number": 1,
            "created": EPOCH,
            "lastModified": date,
            "fileLastModified": EPOCH,
            "sizeBytes": book.content.byte_size,
            "size": f"{book.content.byte_size / 1024**2:.1f} MB",
            "media": {
                "status": "READY",
                "mediaType": "application/pdf" if book.content.format == "pdf" else "application/zip",
                "pagesCount": book.content.page_count,
                "comment": "",
                "epubDivinaCompatible": False,
                "epubIsKepub": False,
                "mediaProfile": "PDF" if book.content.format == "pdf" else "DIVINA",
            },
            "metadata": {
                "title": name,
                "titleLock": False,
                "summary": "",
                "summaryLock": False,
                "number": "1",
                "numberLock": False,
                "numberSort": 1,
                "numberSortLock": False,
                "releaseDate": None,
                "releaseDateLock": False,
                "authors": [],
                "authorsLock": False,
                "tags": tags,
                "tagsLock": False,
                "isbn": "",
                "isbnLock": False,
                "links": [],
                "linksLock": False,
                "created": EPOCH,
                "lastModified": EPOCH,
            },
            "readProgress": progress,
            "deleted": False,
            "fileHash": book.content.sha256,
            "oneshot": True,
        }

    def series_dto(book, preference=None):
        name = title(book, preference)
        complete, started = book.personal.completed, book.personal.resume_page is not None
        return {
            "id": book.id,
            "libraryId": LIBRARY,
            "name": name,
            "url": "",
            "created": EPOCH,
            "lastModified": EPOCH,
            "fileLastModified": EPOCH,
            "booksCount": 1,
            "booksReadCount": int(complete),
            "booksUnreadCount": int(not started and not complete),
            "booksInProgressCount": int(started and not complete),
            "deleted": False,
            "oneshot": True,
            "metadata": {
                "status": "ENDED",
                "statusLock": False,
                "title": name,
                "titleLock": False,
                "titleSort": name,
                "titleSortLock": False,
                "summary": "",
                "summaryLock": False,
                "readingDirection": "",
                "readingDirectionLock": False,
                "publisher": "",
                "publisherLock": False,
                "ageRating": None,
                "ageRatingLock": False,
                "language": book.metadata.language or "",
                "languageLock": False,
                "genres": [],
                "genresLock": False,
                "tags": [],
                "tagsLock": False,
                "totalBookCount": 1,
                "totalBookCountLock": False,
                "sharingLabels": [],
                "sharingLabelsLock": False,
                "links": [],
                "linksLock": False,
                "alternateTitles": [],
                "alternateTitlesLock": False,
                "created": EPOCH,
                "lastModified": EPOCH,
            },
            "booksMetadata": {
                "authors": [],
                "tags": [],
                "releaseDate": None,
                "summary": "",
                "summaryNumber": "",
                "created": EPOCH,
                "lastModified": EPOCH,
            },
        }

    def page_result(items, total, page, size):
        return {
            "content": items,
            "totalElements": total,
            "totalPages": math.ceil(total / size),
            "size": size,
            "number": page,
            "numberOfElements": len(items),
            "first": page == 0,
            "last": (page + 1) * size >= total,
            "empty": not items,
            "sort": {"sorted": True, "unsorted": False, "empty": False},
        }

    def listing(request, page, size, search, kind, identifier=None):
        if len(search) > 1024:
            raise InvalidInput("Search too long")
        preference = load_preferences(library).settings.title_preference
        with library.transaction() if kind == "books" else library.connection() as db:
            conditions, values = rule_conditions(search, SearchOptions())
            if identifier:
                conditions.append("b.id=?")
                values.append(identifier)
            requested = request.query_params.getlist("library_id")
            if requested and LIBRARY not in requested:
                conditions.append("0")
            # Unknown statuses are ignored, like unknown sort keys, rather than failing the listing.
            statuses = [key for key in request.query_params.getlist("read_status") if key in READ_STATUS]
            if statuses:
                conditions.append("(" + " OR ".join("(" + READ_STATUS[key] + ")" for key in statuses) + ")")
            where = " AND ".join(conditions) or "1"
            base = f"FROM book b JOIN personal p ON p.book_id=b.id WHERE {where}"
            total = db.execute("SELECT count(*) " + base, values).fetchone()[0]
            ordering = []
            for value in request.query_params.getlist("sort"):
                field, _, direction = value.partition(",")
                if field in SORT_FIELDS:
                    ordering.append(SORT_FIELDS[field] + (" DESC" if direction.lower() == "desc" else " ASC"))
            order = ",".join([*ordering, "b.id"])
            rows = db.execute(
                "SELECT b.id " + base + f" ORDER BY {order} LIMIT ? OFFSET ?", (*values, size, page * size)
            ).fetchall()
            items = [
                book_dto(book, request, db, preference) if kind == "books" else series_dto(book, preference)
                for book in library.get_books([row[0] for row in rows], db)
            ]
        return page_result(items, total, page, size)

    @router.get("/actuator/info")
    def info():
        return {"build": {"name": "MangaCouch Komga adapter", "version": KOMGA_VERSION}}

    @router.get("/api/v1/users/me")
    def me(request: Request):
        return {
            "id": request.state.principal.id,
            "email": "reader@mangacouch.local",
            "roles": ["USER", "PAGE_STREAMING", "FILE_DOWNLOAD"],
            "sharedAllLibraries": True,
            "sharedLibrariesIds": [LIBRARY],
            "labelsAllow": [],
            "labelsExclude": [],
            "ageRestriction": None,
        }

    @router.get("/api/v1/libraries")
    def libraries():
        return [{"id": LIBRARY, "name": "MangaCouch", "root": "", "unavailable": False}]

    @router.get("/api/v1/series")
    def series(
        request: Request,
        page: int = Query(0, ge=0, le=100000),
        size: int = Query(30, ge=1, le=200),
        search: str = "",
    ):
        return listing(request, page, size, search, "series")

    @router.get("/api/v1/books")
    def books(
        request: Request,
        page: int = Query(0, ge=0, le=100000),
        size: int = Query(30, ge=1, le=200),
        search: str = "",
    ):
        return listing(request, page, size, search, "books")

    @router.get("/api/v1/series/{identifier}")
    def series_detail(identifier: str):
        return series_dto(library.get_book(identifier))

    @router.get("/api/v1/series/{identifier}/books")
    def series_books(
        request: Request,
        identifier: str,
        page: int = Query(0, ge=0, le=100000),
        size: int = Query(30, ge=1, le=200),
    ):
        library.get_book(identifier)
        return listing(request, page, size, "", "books", identifier)

    @router.get("/api/v1/books/{identifier}")
    def detail(request: Request, response: Response, identifier: str):
        book = library.get_book(identifier)
        response.headers["ETag"] = f'"{book.personal_revision}"'
        return book_dto(book, request)

    @router.get("/api/v1/books/{identifier}/pages")
    def pages(identifier: str):
        library.get_book(identifier)
        with library.connection() as db:
            row = db.execute(
                "SELECT pages_json FROM archive_manifest WHERE book_id=?", (identifier,)
            ).fetchone()
        if row is None:
            raise NotFound("Book has no page manifest")
        return [
            {
                "number": i + 1,
                "fileName": page["name"],
                "mediaType": page["media_type"],
                "width": page["width"],
                "height": page["height"],
                "sizeBytes": page["byte_size"],
            }
            for i, page in enumerate(json.loads(row[0]))
        ]

    @router.api_route("/api/v1/books/{identifier}/pages/{number}", methods=["GET", "HEAD"])
    async def page_image(request: Request, identifier: str, number: int, zero_based: bool = False):
        index = number if zero_based else number - 1
        if index < 0:
            raise NotFound("Page not found")
        return await image_response(
            request,
            lambda: files.page_bytes(identifier, index),
            media_type=image_media_type,
            limiter=limiter,
            cache_control=CLIENT_IMAGE,
        )

    @router.api_route("/api/v1/books/{identifier}/thumbnail", methods=["GET", "HEAD"])
    @router.api_route("/api/v1/series/{identifier}/thumbnail", methods=["GET", "HEAD"])
    async def thumbnail(request: Request, identifier: str):
        return await image_response(
            request,
            lambda: files.cover_bytes(identifier, cache, format="jpeg"),
            media_type="image/jpeg",
            limiter=limiter,
            cache_control=CLIENT_IMAGE,
        )

    @router.api_route("/api/v1/books/{identifier}/file", methods=["GET", "HEAD"])
    def archive(identifier: str):
        return ArchiveResponse(files, library.get_book(identifier), limiter)

    @router.patch("/api/v1/books/{identifier}/read-progress", status_code=204)
    def progress(request: Request, identifier: str, data: ProgressInput):
        principal = manager(request)
        book = library.get_book(identifier)
        with library.connection() as db:
            seen = db.execute(
                "SELECT revision FROM client_observation WHERE credential_id=? AND book_id=?",
                (principal.id, identifier),
            ).fetchone()
        if not seen or seen[0] != book.personal_revision:
            raise RevisionConflict("Read current book progress before updating")
        if request.headers.get("if-match") not in {None, f'"{book.personal_revision}"'}:
            raise RevisionConflict("Progress changed")
        if data.page is None and data.completed is None:
            raise InvalidInput("Page or completed is required")
        if data.page is not None and data.page > book.content.page_count:
            raise InvalidInput("Page outside book")
        state = book.personal.model_copy(deep=True)
        if data.page is not None:
            state.resume_page = data.page - 1
        if data.completed is not None:
            state.completed = data.completed
        elif data.page is not None:
            state.completed = data.page == book.content.page_count
        saved = library.save_personal(identifier, state, expected_revision=seen[0])
        with library.transaction() as db:
            db.execute(
                "UPDATE client_observation SET revision=? WHERE credential_id=? AND book_id=? AND revision=?",
                (saved.personal_revision, principal.id, identifier, seen[0]),
            )
        return Response(status_code=204)

    app.include_router(router)
