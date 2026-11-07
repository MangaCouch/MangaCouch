"""Collection routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from pydantic import Field

from .models import Model
from .services import Services


class CollectionCreate(Model):
    rule: dict | None = None
    name: Annotated[str, Field(min_length=1, max_length=256)]


class CollectionRename(CollectionCreate):
    expected_revision: Annotated[int, Field(ge=1)]


class CollectionItem(Model):
    book_id: Annotated[str, Field(min_length=1, max_length=64)]
    expected_revision: Annotated[int, Field(ge=1)]


class CollectionMove(Model):
    expected_revision: Annotated[int, Field(ge=1)]
    before_id: Annotated[str, Field(min_length=1, max_length=64)] | None = None


def collections_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/collections")
    collections = services.collections

    @router.get("")
    def list_collections(limit: int = 50, after: str = "") -> dict:
        return collections.list(limit=limit, after=after)

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
        after_position: int = -1,
        expected_revision: int | None = None,
        expected_result: str | None = None,
    ) -> dict:
        result = collections.get(
            identifier,
            limit=limit,
            after_position=after_position,
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
    def rename_collection(identifier: str, data: CollectionRename) -> dict:
        return collections.rename(identifier, data.name, data.expected_revision)

    @router.post("/{identifier}/freeze")
    def freeze_collection(identifier: str, data: CollectionMove) -> dict:
        return collections.freeze(identifier, data.expected_revision)

    @router.delete("/{identifier}")
    def delete_collection(identifier: str, expected_revision: int) -> dict:
        collections.delete(identifier, expected_revision)
        return {"deleted": True}

    @router.post("/{identifier}/items")
    def add_collection_item(identifier: str, data: CollectionItem) -> dict:
        return collections.edit_item(identifier, data.book_id, data.expected_revision)

    @router.put("/{identifier}/items/{book_id}")
    def move_collection_item(identifier: str, book_id: str, data: CollectionMove) -> dict:
        return collections.edit_item(
            identifier, book_id, data.expected_revision, action="move", before_id=data.before_id
        )

    @router.delete("/{identifier}/items/{book_id}")
    def remove_collection_item(identifier: str, book_id: str, expected_revision: int) -> dict:
        return collections.edit_item(identifier, book_id, expected_revision, action="remove")

    return router
