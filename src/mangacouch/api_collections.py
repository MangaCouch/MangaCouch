"""Collection routes."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import Field

from .models import Model
from .services import Services

Revision = Annotated[int, Query(ge=1)]


class CollectionCreate(Model):
    rule: dict | None = None
    name: Annotated[str, Field(min_length=1, max_length=256)]


class CollectionRule(Model):
    rule: dict


class CollectionRename(Model):
    name: Annotated[str, Field(min_length=1, max_length=256)]


class CollectionItem(Model):
    book_id: Annotated[str, Field(min_length=1, max_length=64)]


class CollectionMove(Model):
    before_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


def collections_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/collections")
    collections = services.collections

    @router.get("")
    def list_collections(
        limit: int = 50,
        after: str = "",
        kind: Literal["static", "dynamic"] | None = None,
        counts: bool = True,
    ) -> dict:
        return collections.list(limit=limit, after=after, kind=kind, counts=counts)

    @router.post("")
    def create_collection(data: CollectionCreate) -> dict:
        return collections.create(data.name, data.rule)

    @router.get("/{identifier}/context")
    def collection_context(identifier: str, book_id: str | None = None) -> dict:
        return collections.context(identifier, book_id)

    @router.get("/{identifier}")
    def get_collection(
        identifier: str,
        limit: int = 50,
        after: str = "",
        expected_revision: Annotated[int | None, Query(ge=1)] = None,
        expected_result: str | None = None,
    ) -> dict:
        result = collections.get(
            identifier,
            limit=limit,
            after=after,
            expected_revision=expected_revision,
            expected_result=expected_result,
        )
        books = {
            book["id"]: book for book in services.book_outputs([item["book_id"] for item in result["items"]])
        }
        result["items"] = [
            {**item, "book": books[item["book_id"]]} for item in result["items"] if item["book_id"] in books
        ]
        return result

    @router.put("/{identifier}")
    def rename_collection(identifier: str, expected_revision: Revision, data: CollectionRename) -> dict:
        return collections.rename(identifier, data.name, expected_revision)

    @router.put("/{identifier}/rule")
    def update_rule(identifier: str, expected_revision: Revision, data: CollectionRule) -> dict:
        return collections.update_rule(identifier, data.rule, expected_revision)

    @router.post("/{identifier}/freeze")
    def freeze_collection(identifier: str, expected_revision: Revision) -> dict:
        return collections.freeze(identifier, expected_revision)

    @router.delete("/{identifier}", status_code=204)
    def delete_collection(identifier: str, expected_revision: Revision) -> None:
        collections.delete(identifier, expected_revision)

    @router.post("/{identifier}/items")
    def add_collection_item(identifier: str, expected_revision: Revision, data: CollectionItem) -> dict:
        return collections.edit_item(identifier, data.book_id, expected_revision)

    @router.put("/{identifier}/items/{book_id}")
    def move_collection_item(
        identifier: str, book_id: str, expected_revision: Revision, data: CollectionMove
    ) -> dict:
        return collections.edit_item(
            identifier, book_id, expected_revision, action="move", before_id=data.before_id
        )

    @router.delete("/{identifier}/items/{book_id}")
    def remove_collection_item(identifier: str, book_id: str, expected_revision: Revision) -> dict:
        return collections.edit_item(identifier, book_id, expected_revision, action="remove")

    return router
