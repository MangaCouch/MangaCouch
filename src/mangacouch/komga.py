from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Annotated

import anyio
from fastapi import APIRouter, FastAPI, Query, Request, Response
from pydantic import Field

from .archives import ThumbnailFormat, image_media_type
from .database import busy
from .errors import InvalidInput, NotFound, RevisionConflict
from .exports import ArchiveResponse
from .models import Model, protocol_progress
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
# When a book was last read or else marked read: a manual completion records no reading activity.
ACTIVITY = (
    "max(coalesce((SELECT updated_at FROM reading_activity WHERE book_id=b.id),0),"
    "coalesce((SELECT max(created_at) FROM reading_event WHERE book_id=b.id AND kind='completed'),0))"
)
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


def read_dates(db, book_ids: list[str]) -> dict[str, int]:
    """The ACTIVITY time of each book that has one."""
    ids = json.dumps(book_ids)
    return dict(
        db.execute(
            "SELECT book_id,max(at) FROM (SELECT book_id,updated_at AS at FROM reading_activity "
            "WHERE book_id IN (SELECT value FROM json_each(?)) UNION ALL "
            "SELECT book_id,created_at FROM reading_event WHERE kind='completed' "
            "AND book_id IN (SELECT value FROM json_each(?))) GROUP BY book_id",
            (ids, ids),
        )
    )


class ProgressInput(Model):
    page: Annotated[int, Field(ge=1)] | None = None
    completed: bool | None = None
    deviceId: Annotated[str, Field(max_length=256)] | None = None
    deviceName: Annotated[str, Field(max_length=256)] | None = None


def install_komga(app: FastAPI, files, cache, limiter, cover_limiter):
    router = APIRouter(prefix="/komga")
    library = files.library

    def title(book, preference=None):
        return preferred_title(book, preference or load_preferences(library).settings.title_preference)

    def observe(request, books) -> None:
        """Record the personal revision of each book this client was just shown, in one short
        transaction after the read; a progress write is later accepted against what it saw."""
        if request.state.principal.kind == "readonly" or not books:
            return
        try:
            with library.transaction() as db:
                db.executemany(
                    "INSERT INTO client_observation VALUES(?,?,?) ON CONFLICT(credential_id,book_id) "
                    "DO UPDATE SET revision=excluded.revision",
                    [(request.state.principal.id, book.id, book.personal_revision) for book in books],
                )
        except sqlite3.Error as error:
            if not busy(error):
                raise

    def book_dto(book, request, db=None, preference=None, activity_by_id=None):
        """A book as Komga clients see it; the caller records the observation (`observe`)."""
        if db is None:
            with library.connection() as connection:
                return book_dto(book, request, connection, preference)
        if activity_by_id is None:
            activity_by_id = read_dates(db, [book.id])
        activity = activity_by_id.get(book.id)
        date = datetime.fromtimestamp(activity, UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if activity else EPOCH
        progress = None
        page, completed = protocol_progress(book.personal, book.content.page_count)
        if page is not None:
            progress = {
                "page": page,
                "completed": completed,
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
        with library.connection() as db:
            db.execute("BEGIN")
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
            books = library.get_books([row[0] for row in rows], db)
            activity = read_dates(db, [book.id for book in books]) if kind == "books" else {}
            items = [
                book_dto(book, request, db, preference, activity)
                if kind == "books"
                else series_dto(book, preference)
                for book in books
            ]
        if kind == "books":
            observe(request, books)
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
        result = book_dto(book, request)
        observe(request, [book])
        return result

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
        book = await anyio.to_thread.run_sync(lambda: files.library.get_book(identifier))
        format: ThumbnailFormat = "webp" if "image/webp" in request.headers.get("accept", "") else "jpeg"
        response = await image_response(
            request,
            lambda: files.cover_bytes(identifier, cache, format=format, book=book),
            media_type=f"image/{format}",
            cached=lambda: files.cached_cover(book, cache, format=format),
            limiter=cover_limiter,
            cache_control=CLIENT_IMAGE,
        )
        response.headers["Vary"] += ", Accept"
        return response

    @router.api_route("/api/v1/books/{identifier}/file", methods=["GET", "HEAD"])
    def archive(identifier: str):
        return ArchiveResponse(files, library.get_book(identifier), limiter)

    def save_progress(request: Request, identifier: str, change) -> Response:
        # The boundary refuses read-only keys any write, so every principal here may save.
        principal = request.state.principal
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
        state = book.personal.model_copy(deep=True)
        reading = change(state, book.content.page_count)

        def observe_saved(db):
            db.execute(
                "UPDATE client_observation SET revision=? WHERE credential_id=? AND book_id=? AND revision=?",
                (seen[0] + 1, principal.id, identifier, seen[0]),
            )

        library.save_personal(
            identifier, state, expected_revision=seen[0], also=observe_saved, record_activity=reading
        )
        return Response(status_code=204)

    @router.patch("/api/v1/books/{identifier}/read-progress", status_code=204)
    def progress(request: Request, identifier: str, data: ProgressInput):
        if data.page is None and data.completed is None:
            raise InvalidInput("Page or completed is required")

        def change(state, page_count: int) -> bool:
            """Apply the update; whether it is reading (as opposed to a manual read mark)."""
            if data.page is not None:
                if data.page > page_count:
                    raise InvalidInput("Page outside book")
                # Reading never undoes "finished" (decision 2), whatever `completed` says.
                state.resume_page = data.page - 1
                state.completed = state.completed or bool(data.completed) or data.page == page_count
                return True
            if data.completed:
                # "Mark as read" without reading, like the web menu: no reading history or days.
                state.completed = True
            else:
                # {"completed": false} alone is "mark as unread", as DELETE is.
                state.completed, state.resume_page = False, None
            return False

        return save_progress(request, identifier, change)

    @router.delete("/api/v1/books/{identifier}/read-progress", status_code=204)
    def unread(request: Request, identifier: str):
        def change(state, _page_count: int) -> bool:
            state.completed, state.resume_page = False, None
            return False

        return save_progress(request, identifier, change)

    app.include_router(router)
