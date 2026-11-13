"""Library browsing routes: book lists and details, personal state, history, duplicates."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from pydantic import Field, ValidationError

from .activity import Activity
from .cursors import decode_cursor, encode_cursor
from .errors import InvalidInput
from .models import Model, PersonalState, canonical_json, label_key
from .searching import (
    SearchOptions,
    rule_conditions,
    search_count,
    search_ids,
    suggestion_scope,
    tag_suggestions,
)
from .services import Services

MAX_PAGE = 400_001


class PersonalInput(Model):
    state: PersonalState
    record_activity: bool = True
    base_state: PersonalState | None = None


BookId = Annotated[str, Field(min_length=1, max_length=64)]


class BookIdsInput(Model):
    book_ids: Annotated[list[BookId], Field(min_length=1, max_length=500)]


class SelectionStateInput(Model):
    book_ids: Annotated[list[BookId], Field(max_length=500)] | None = None
    query: Annotated[str, Field(max_length=1024)] = ""
    options: SearchOptions = Field(default_factory=SearchOptions)
    exclude: Annotated[list[BookId], Field(max_length=500)] = Field(default_factory=list)


class DuplicateIgnoreInput(Model):
    left_id: Annotated[str, Field(min_length=1, max_length=64)]
    right_id: Annotated[str, Field(min_length=1, max_length=64)]


def library_router(services: Services) -> APIRouter:
    router = APIRouter()
    library = services.files.library

    @router.get("/api/books")
    def books(
        query: str = "",
        limit: int = 50,
        after: str = "",
        reading: str = "all",
        page: int | None = None,
        include_total: bool = True,
        collection_id: str | None = None,
        work_type: str = "",
        review: str = "all",
        rating: str = "all",
        min_rating: float | None = None,
        source_rating_min: float | None = None,
        language: str = "",
        source: str = "",
        order: str = "default",
        seed: str | None = None,
    ) -> dict:
        try:
            options = SearchOptions.model_validate(
                {
                    "language": language,
                    "work_type": work_type,
                    "source": source,
                    "reading": reading,
                    "review": review,
                    "rating": rating,
                    "min_rating": min_rating,
                    "source_rating_min": source_rating_min,
                    "order": order,
                    "seed": seed,
                }
            )
        except ValidationError as error:
            raise InvalidInput("; ".join(item["msg"] for item in error.errors())) from error
        if page is not None and (page < 1 or page > MAX_PAGE or after):
            raise InvalidInput("Invalid page")
        after_order = None
        if after and options.order == "recent":
            after_order, after_id = decode_cursor(after, int, str, signed=True)
        else:
            (after_id,) = decode_cursor(after, str) if after else ("",)
        with library.connection() as db:
            db.execute("BEGIN")
            ids = search_ids(
                db,
                query,
                limit=limit,
                after_id=after_id,
                options=options,
                offset=((page or 1) - 1) * limit,
                after_order=after_order,
            )
            # Continuation requests (`after` loops) already know the total.
            total = None
            if include_total and not after:
                total = (
                    len(ids) if (page or 1) == 1 and len(ids) < limit else search_count(db, query, options)
                )
            cursor = None
            if len(ids) == limit:
                cursor = (
                    encode_cursor(
                        db.execute("SELECT recent_order FROM book WHERE id=?", (ids[-1],)).fetchone()[0],
                        ids[-1],
                    )
                    if options.order == "recent"
                    else encode_cursor(ids[-1])
                )
            projected = services.project_books(db, ids)
            if collection_id is not None:
                services.collections._get(db, collection_id)
                members = {
                    row[0]
                    for row in db.execute(
                        "SELECT book_id FROM collection_item WHERE collection_id=? AND "
                        "book_id IN (SELECT value FROM json_each(?))",
                        (collection_id, canonical_json(ids)),
                    )
                }
                for item in projected:
                    item["in_collection"] = item["id"] in members
        return {
            "items": projected,
            "total": total,
            "after": cursor,
        }

    @router.post("/api/thumbnails/refresh")
    def refresh_thumbnails(data: BookIdsInput) -> dict:
        """Version each book's thumbnails for every device; old cache entries expire normally."""
        with library.transaction() as db:
            books = library.get_books(list(dict.fromkeys(data.book_ids)), db)
            db.executemany(
                "INSERT INTO thumbnail_revision(book_id,revision) VALUES(?,1) "
                "ON CONFLICT(book_id) DO UPDATE SET revision=revision+1",
                [(book.id,) for book in books],
            )
        return {"refreshed": len(books)}

    @router.post("/api/books/selection-state")
    def selection_state(data: SelectionStateInput) -> dict:
        conditions, values = rule_conditions(data.query, data.options)
        if data.book_ids is not None:
            conditions.append("b.id IN (SELECT value FROM json_each(?))")
            values.append(canonical_json(data.book_ids))
        conditions += [
            "b.id NOT IN (SELECT value FROM json_each(?))",
            "json_extract(p.state_json,'$.completed')=0",
        ]
        values.append(canonical_json(data.exclude))
        with library.connection() as db:
            unfinished = db.execute(
                "SELECT EXISTS(SELECT 1 FROM book b JOIN personal p ON p.book_id=b.id WHERE "
                + " AND ".join(conditions)
                + ")",
                values,
            ).fetchone()[0]
        return {"unfinished": bool(unfinished)}

    @router.post("/api/client-cache/clear", status_code=204)
    def clear_client_cache(response: Response) -> None:
        """Asks the browser to empty its HTTP cache for this site (pages, covers, thumbnails).
        Only the cache: cookies and storage, and so the sign-in, stay. Browsers honour it on
        HTTPS and localhost only."""
        response.headers["Clear-Site-Data"] = '"cache"'

    @router.get("/api/books/{book_id}")
    def detail(book_id: str) -> dict:
        return services.book_output(library.get_book(book_id))

    @router.put("/api/books/{book_id}/personal")
    def personal(book_id: str, expected_revision: Annotated[int, Query(ge=1)], data: PersonalInput) -> dict:
        return services.book_output(
            library.save_personal(
                book_id,
                data.state,
                expected_revision=expected_revision,
                record_activity=data.record_activity,
                base_state=data.base_state,
            )
        )

    @router.get("/api/tag-suggestions")
    def suggestions(query: str = "", limit: int = 10, database: bool = False) -> dict:
        with library.connection() as db:
            tags = tag_suggestions(db, query, limit)
            if database and len(tags) < limit and query:
                known = {(tag["namespace"], tag["value"]) for tag in tags}
                scope, needle, _ = suggestion_scope(query)
                for row in db.execute(
                    "SELECT namespace,value,translation,0 AS count FROM effective_tag_alias WHERE "
                    "(?1='' OR namespace=?1) AND (instr(value,?2)>0 OR instr(search_text,?4)>0) ORDER BY "
                    "namespace,value LIMIT ?3",
                    (scope, needle, limit * 2, label_key(needle) if needle else ""),
                ):
                    if (row["namespace"], row["value"]) not in known:
                        tags.append(dict(row))
            return {"items": tags[:limit]}

    @router.get("/api/reading-history")
    def reading_history(after: str = "", limit: int = 50, utc_offset: int = 0, timezone: str = "") -> dict:
        result = Activity(library).recent(after=after, limit=limit, utc_offset=utc_offset, timezone=timezone)
        books = {book["id"]: book for book in services.book_outputs([e["book_id"] for e in result["items"]])}
        # Events of books removed since are skipped rather than failing the page.
        result["items"] = [
            {**event, "book": books[event["book_id"]]}
            for event in result["items"]
            if event["book_id"] in books
        ]
        return result

    @router.get("/api/statistics")
    def statistics(days: int = 30, utc_offset: int = 0, timezone: str = "") -> dict:
        return Activity(library).statistics(days=days, utc_offset=utc_offset, timezone=timezone)

    @router.get("/api/duplicates")
    def duplicate_groups(after: str = "", limit: int = 50) -> dict:
        result = services.duplicates.groups(after=after, limit=limit)
        identifiers = list(
            dict.fromkeys(group[key] for group in result["items"] for key in ("left_id", "right_id"))
        )
        books = {book["id"]: book for book in services.book_outputs(identifiers)}
        result["items"] = [
            {**group, "books": [books[group["left_id"]], books[group["right_id"]]]}
            for group in result["items"]
            if group["left_id"] in books and group["right_id"] in books
        ]
        return result

    @router.post("/api/duplicates/ignore", status_code=204)
    def ignore_duplicates(data: DuplicateIgnoreInput) -> None:
        services.duplicates.ignore(data.left_id, data.right_id)

    return router
