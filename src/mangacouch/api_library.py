"""Library browsing routes: book lists and details, personal state, history, duplicates."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import Field

from .activity import Activity
from .errors import InvalidInput
from .models import Model, PersonalState, canonical_json
from .searching import SearchOptions, search_count, search_ids, tag_suggestions
from .services import Services

MAX_PAGE = 400_001


class PersonalInput(Model):
    expected_revision: Annotated[int, Field(ge=1)]
    state: PersonalState


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
        after_id: str = "",
        reading: str = "all",
        page: int | None = None,
        include_total: bool = True,
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
        if page is not None and (page < 1 or page > MAX_PAGE or after_id):
            raise InvalidInput("Invalid page")
        with library.connection() as db:
            db.execute("BEGIN")
            ids = search_ids(
                db, query, limit=limit, after_id=after_id, options=options, offset=((page or 1) - 1) * limit
            )
            # Continuation requests (after_id loops) already know the total.
            total = search_count(db, query, options) if include_total and not after_id else None
            projected = services.project_books(db, ids)
        return {"books": projected, "total": total, "after_id": ids[-1] if ids else None}

    @router.get("/api/books/{book_id}")
    def detail(book_id: str) -> dict:
        result = services.book_output(library.get_book(book_id))
        with library.connection() as db:
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

    @router.put("/api/books/{book_id}/personal")
    def personal(book_id: str, data: PersonalInput) -> dict:
        return services.book_output(
            library.save_personal(book_id, data.state, expected_revision=data.expected_revision)
        )

    @router.get("/api/tag-suggestions")
    def suggestions(query: str = "", limit: int = 10, database: bool = False) -> dict:
        with library.connection() as db:
            tags = tag_suggestions(db, query, limit)
            if database and len(tags) < limit and query:
                known = {(tag["namespace"], tag["value"]) for tag in tags}
                namespace, separator, fragment = query.casefold().partition(":")
                needle = fragment if separator else query.casefold()
                for row in db.execute(
                    "SELECT namespace,value,translation,0 AS count FROM effective_tag_alias WHERE "
                    "(?='' OR namespace=?) AND (instr(value,?)>0 OR instr(search_text,?)>0) ORDER BY "
                    "namespace,value LIMIT ?",
                    (namespace if separator else "", namespace, needle, needle, limit * 2),
                ):
                    if (row["namespace"], row["value"]) not in known:
                        tags.append(dict(row))
            return {"tags": tags[:limit]}

    @router.get("/api/reading-history")
    def reading_history(before: str = "", limit: int = 50, utc_offset: int = 0) -> dict:
        result = Activity(library).recent(before=before, limit=limit, utc_offset=utc_offset)
        books = {book["id"]: book for book in services.book_outputs([e["book_id"] for e in result["events"]])}
        # Events of books removed since are skipped rather than failing the page.
        result["events"] = [
            {**event, "book": books[event["book_id"]]}
            for event in result["events"]
            if event["book_id"] in books
        ]
        return result

    @router.get("/api/statistics")
    def statistics(days: int = 30, utc_offset: int = 0) -> dict:
        return Activity(library).statistics(days=days, utc_offset=utc_offset)

    @router.get("/api/duplicates")
    def duplicate_groups(after: str = "", limit: int = 50) -> dict:
        result = services.duplicates.groups(after=after, limit=limit)
        identifiers = list(
            dict.fromkeys(group[key] for group in result["groups"] for key in ("left_id", "right_id"))
        )
        books = {book["id"]: book for book in services.book_outputs(identifiers)}
        result["groups"] = [
            {**group, "books": [books[group["left_id"]], books[group["right_id"]]]}
            for group in result["groups"]
            if group["left_id"] in books and group["right_id"] in books
        ]
        return result

    @router.post("/api/duplicates/ignore")
    def ignore_duplicates(data: DuplicateIgnoreInput) -> dict:
        services.duplicates.ignore(data.left_id, data.right_id)
        return {"ignored": True}

    return router
