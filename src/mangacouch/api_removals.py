"""Recycle-bin routes; the removal logic lives in removals.Removals."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import Field

from .errors import RevisionConflict
from .models import Model, canonical_json
from .searching import SearchOptions, rule_conditions, search_count
from .services import Services, require_manager

BookId = Annotated[str, Field(min_length=1, max_length=64)]


class RemovalPreviewInput(Model):
    book_id: BookId


class RemovalInput(RemovalPreviewInput):
    delete_file: bool = False
    id: Annotated[str, Field(min_length=36, max_length=36)]
    token: Annotated[str, Field(min_length=64, max_length=64)]
    confirmed: bool


class MaintenanceCheckInput(Model):
    after_id: Annotated[str, Field(max_length=64)] = ""
    through_id: Annotated[str, Field(max_length=64)] | None = None
    limit: Annotated[int, Field(ge=1, le=100)] = 100


class SelectedPreviewInput(Model):
    book_ids: Annotated[list[BookId], Field(min_length=1, max_length=100)]


class SearchSelectionInput(Model):
    query: Annotated[str, Field(max_length=1024)] = ""
    options: SearchOptions = Field(default_factory=SearchOptions)
    expected_count: Annotated[int, Field(ge=1, le=100000)]
    exclude: Annotated[list[BookId], Field(max_length=500)] = Field(default_factory=list)


class RestoreInput(Model):
    book_ids: Annotated[list[BookId], Field(min_length=1, max_length=100)] | None = None


class SelectedRemovalInput(SelectedPreviewInput):
    delete_file: bool = False
    id: Annotated[str, Field(min_length=36, max_length=36)]
    token: Annotated[str, Field(min_length=64, max_length=64)]
    confirmed: bool


def removals_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/removals")
    removals, bounded = services.removals, services.bounded

    @router.post("/search/selection")
    def search_selection(data: SearchSelectionInput) -> dict:
        with services.files.library.connection() as db:
            db.execute("BEGIN")
            if search_count(db, data.query, data.options) != data.expected_count:
                raise RevisionConflict("Search result count changed; refresh before selecting all")
            conditions, values = rule_conditions(data.query, data.options)
            conditions.append("b.id NOT IN (SELECT value FROM json_each(?))")
            values.append(canonical_json(data.exclude))
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT b.id FROM book b JOIN personal p ON p.book_id=b.id WHERE "
                    + " AND ".join(conditions)
                    + " ORDER BY b.id",
                    values,
                )
            ]
        return {"book_ids": ids}

    @router.post("/preview")
    async def removal_preview(data: RemovalPreviewInput) -> dict:
        return await bounded(lambda: removals.preview(data.book_id))

    @router.post("/selected/preview")
    async def selected_removal_preview(data: SelectedPreviewInput) -> dict:
        return await bounded(lambda: removals.preview_selected(data.book_ids))

    @router.post("/selected")
    async def remove_selected_records(data: SelectedRemovalInput) -> dict:
        return await bounded(
            lambda: removals.remove_selected(
                data.id, data.book_ids, data.token, confirmed=data.confirmed, delete_file=data.delete_file
            )
        )

    @router.post("/{identifier}/cancel-deletion")
    async def cancel_removal_deletion(identifier: str) -> dict:
        return await bounded(lambda: removals.cancel_deletion(identifier))

    @router.post("")
    async def remove_record(data: RemovalInput) -> dict:
        return await bounded(
            lambda: removals.remove(
                data.id, data.book_id, data.token, confirmed=data.confirmed, delete_file=data.delete_file
            )
        )

    @router.get("")
    def removal_history(request: Request, limit: int = 50, before: int | None = None) -> dict:
        require_manager(request)
        return removals.list(limit=limit, before=before)

    @router.post("/check")
    async def maintenance_check(data: MaintenanceCheckInput) -> dict:
        return await bounded(
            lambda: removals.check_page(after_id=data.after_id, through_id=data.through_id, limit=data.limit)
        )

    @router.post("/{identifier}/restore")
    async def restore_record(identifier: str, data: RestoreInput | None = None) -> dict:
        return await bounded(lambda: removals.restore(identifier, data.book_ids if data else None))

    return router
