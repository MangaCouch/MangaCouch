"""Tag alias and bulk-operation routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import Field

from .models import Model, Tag
from .organization import BulkSearchRequest, BulkSelectionRequest
from .services import Services, require_manager


class AliasInput(Model):
    tag: Tag
    translation: Annotated[str, Field(max_length=512)]
    expected: Annotated[str, Field(max_length=512)] | None = None


def organization_router(services: Services) -> APIRouter:
    router = APIRouter()
    organization = services.organization

    @router.get("/api/tag-aliases")
    def aliases(after: str = "", limit: int = 100) -> dict:
        return organization.aliases(after=after, limit=limit)

    @router.put("/api/tag-aliases", response_model=None)
    def alias(data: AliasInput) -> dict | Response:
        saved = organization.alias(data.tag, data.translation, data.expected)
        return saved if saved is not None else Response(status_code=204)

    async def create(request: BulkSelectionRequest | BulkSearchRequest) -> dict:
        # Up to 50 books finish inside the request; larger jobs are applied by the maintenance
        # worker while clients poll GET /api/bulk/{id}.
        job = await services.bounded(lambda: organization.create_bulk(request))
        if sum(job["counts"].values()) <= 50:
            return await services.bounded(lambda: organization.advance_job(job["id"]))
        services.maintenance.wake()
        return job

    @router.post("/api/bulk-search")
    async def start_search(data: BulkSearchRequest) -> dict:
        return await create(data)

    @router.post("/api/bulk-selection")
    async def start_selection(data: BulkSelectionRequest) -> dict:
        return await create(data)

    @router.get("/api/bulk/{identifier}")
    def job(request: Request, identifier: str, after: str = "", limit: int = 200) -> dict:
        require_manager(request)
        return organization.bulk(identifier, after=after, limit=limit)

    @router.post("/api/bulk/{identifier}/cancel")
    def cancel_job(identifier: str) -> dict:
        return organization.cancel(identifier)

    return router
