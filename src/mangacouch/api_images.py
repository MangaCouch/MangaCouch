"""Cover, thumbnail and page image routes for the web client."""

from __future__ import annotations

import anyio
from fastapi import APIRouter, Request, Response

from .archives import image_media_type
from .services import IMMUTABLE, REVALIDATE, Services, image_etag, image_response, image_version


def images_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/books/{book_id}")
    files, cache = services.files, services.thumbnail_cache

    async def thumbnail_response(request: Request, book_id: str, index: int, kind: str) -> Response:
        book = await anyio.to_thread.run_sync(lambda: files.library.get_book(book_id))
        # Versioned URLs (?v=<sha256[:16]>) never change content, so clients may keep them.
        immutable = request.query_params.get("v") == image_version(book)
        return await image_response(
            request,
            lambda: files.cover_bytes(book_id, cache, index),
            media_type="image/webp",
            limiter=services.cover_limiter,
            cache_control=IMMUTABLE if immutable else REVALIDATE,
            etag=image_etag(files, book, index, kind),
        )

    @router.get("/cover")
    async def cover(request: Request, book_id: str) -> Response:
        return await thumbnail_response(request, book_id, 0, "cover")

    @router.get("/thumbnails/{index}")
    async def thumbnail(request: Request, book_id: str, index: int) -> Response:
        return await thumbnail_response(request, book_id, index, "thumbnail")

    @router.get("/pages/{index}")
    async def page(request: Request, book_id: str, index: int) -> Response:
        book = await anyio.to_thread.run_sync(lambda: files.library.get_book(book_id))
        return await image_response(
            request,
            lambda: files.page_bytes(book_id, index),
            media_type=image_media_type,
            limiter=services.file_limiter,
            cache_control=IMMUTABLE if request.query_params.get("v") == image_version(book) else REVALIDATE,
            etag=image_etag(files, book, index, "page"),
        )

    return router
