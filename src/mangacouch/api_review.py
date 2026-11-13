"""Metadata editing, review confirmation and sidecar synchronization routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from .database import Book
from .models import Metadata, Model
from .services import Services
from .sidecars import Choice


class MetadataInput(Model):
    metadata: Metadata


class CandidateInput(Model):
    token: Annotated[str, Field(max_length=64)]
    candidate: Annotated[str, Field(max_length=64)]
    choices: dict[str, str] | None = None


class ReviewInput(Model):
    token: Annotated[str, Field(max_length=64)]


class CompleteInput(ReviewInput):
    acknowledged_issues: Annotated[list[str], Field(max_length=16)]


class SyncInput(Model):
    book_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    limit: Annotated[int, Field(ge=1, le=1000)] = 100
    after: Annotated[str, Field(max_length=2048)] = ""
    through_id: Annotated[str, Field(max_length=64)] | None = None
    mode: Literal["all", "pending"] = "all"
    replace_invalid: bool = False
    """With book_id: overwrite a damaged sidecar that otherwise fails every sync."""


class ResolveInput(Model):
    choice: Choice = "library"
    fields: dict[str, Choice] | None = None


def review_router(services: Services) -> APIRouter:
    router = APIRouter()
    review, sync, library = services.review, services.sync, services.files.library

    def saved(book: Book) -> dict:
        """A committed metadata change: the book plus `sidecar_sync`, the result of writing its
        sidecar. A failed write does not fail the request; it reports "pending" and is retried."""
        published = sync.publish(book.id)
        return {**services.book_output(library.get_book(book.id)), "sidecar_sync": published}

    @router.put("/api/books/{book_id}/metadata")
    async def metadata(
        book_id: str, expected_revision: Annotated[int, Query(ge=1)], data: MetadataInput
    ) -> dict:
        return await services.bounded(
            lambda: saved(review.edit(book_id, data.metadata, expected_revision=expected_revision))
        )

    @router.get("/api/books/{book_id}/review")
    async def preview(book_id: str) -> dict:
        return await services.bounded(lambda: review.preview(book_id))

    @router.post("/api/books/{book_id}/review/candidate")
    async def candidate(book_id: str, data: CandidateInput) -> dict:
        return await services.bounded(
            lambda: saved(review.candidate(book_id, data.candidate, data.token, data.choices))
        )

    @router.post("/api/books/{book_id}/review/automatic")
    async def automatic(book_id: str, data: ReviewInput) -> dict:
        def run() -> dict:
            result = review.automatic(book_id, data.token)
            return {**result, "book": services.book_output(library.get_book(book_id))}

        return await services.bounded(run)

    @router.post("/api/books/{book_id}/review/complete")
    async def complete(book_id: str, data: CompleteInput) -> dict:
        return await services.bounded(
            lambda: services.book_output(review.complete(book_id, data.token, data.acknowledged_issues))
        )

    @router.get("/api/sidecar/conflicts")
    def conflicts(limit: int = 100, after: str = "", book_id: str | None = None) -> dict:
        return sync.conflict_page(limit=limit, after=after, book_id=book_id)

    @router.get("/api/sidecar/status")
    def sidecar_status(limit: int = 50, after: str = "") -> dict:
        return sync.status(limit=limit, after=after)

    @router.post("/api/sidecar/sync")
    async def synchronize(data: SyncInput) -> dict:
        return await services.bounded(
            lambda: (
                sync.sync_one(data.book_id, replace_invalid=data.replace_invalid)
                if data.book_id
                else sync.batch(
                    limit=data.limit, after=data.after, through_id=data.through_id, mode=data.mode
                )
            )
        )

    @router.post("/api/sidecar/conflicts/{conflict_id}/resolve")
    async def resolve(conflict_id: str, data: ResolveInput) -> dict:
        return await services.bounded(
            lambda: saved(sync.resolve(conflict_id, choice=data.choice, fields=data.fields))
        )

    return router
