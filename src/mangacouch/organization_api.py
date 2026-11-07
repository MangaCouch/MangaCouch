"""Tag alias and bulk-operation routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import Field

from .models import Model, Tag
from .organization import BulkRequest, BulkSearchRequest
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
        rows = organization.aliases(after=after, limit=limit)
        return {
            "aliases": rows,
            "after": rows[-1]["namespace"] + "\t" + rows[-1]["value"] if len(rows) == limit else None,
        }

    @router.put("/api/tag-aliases")
    def alias(data: AliasInput) -> dict:
        organization.alias(data.tag, data.translation, data.expected)
        return {"saved": True}

    async def create(request: BulkRequest | BulkSearchRequest) -> dict:
        # The maintenance worker applies the job; clients poll GET /api/bulk/{id}.
        job = await services.bounded(lambda: organization.create_bulk(request))
        services.maintenance.wake()
        return job

    @router.post("/api/bulk")
    async def start(data: BulkRequest) -> dict:
        return await create(data)

    @router.post("/api/bulk-search")
    async def start_search(data: BulkSearchRequest) -> dict:
        return await create(data)

    @router.get("/api/bulk/{identifier}")
    def job(request: Request, identifier: str, after: int = -1, limit: int = 200) -> dict:
        require_manager(request)
        return organization.bulk(identifier, after=after, limit=limit)

    return router
