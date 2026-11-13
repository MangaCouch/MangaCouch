"""Import-folder scans and archive upload routes."""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from pathlib import Path

import anyio
from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder

from .configuration import effective_upload_limit
from .services import Services, require_manager
from .uploads import receive_upload

LOGGER = logging.getLogger(__name__)


def scans_router(services: Services) -> APIRouter:
    router = APIRouter()
    files, scanner = services.files, services.scanner

    @router.get("/api/scans")
    def current_scan(request: Request, state: str = "", limit: int = 50) -> dict:
        require_manager(request)
        return {"scan": scanner.latest(state=state, limit=limit), "root": str(files.root.path / "import")}

    @router.post("/api/scans")
    async def start_scan(keep_source: bool = False) -> dict:
        result = await services.bounded(lambda: scanner.start(keep_source=keep_source))
        services.maintenance.wake()
        return result

    @router.get("/api/scans/{identifier}")
    def scan_status(
        request: Request, identifier: str, after: str = "", limit: int = 50, state: str = ""
    ) -> dict:
        require_manager(request)
        return scanner.get(identifier, after=after, limit=limit, state=state)

    @router.get("/api/scans/{identifier}/items/{item_id}")
    def scan_item(request: Request, identifier: str, item_id: int) -> dict:
        require_manager(request)
        return scanner.item(identifier, item_id)

    @router.post("/api/scans/{identifier}/pause")
    def pause_scan(identifier: str) -> dict:
        return scanner.pause(identifier)

    @router.post("/api/scans/{identifier}/resume")
    async def resume_scan(identifier: str) -> dict:
        result = await services.bounded(lambda: scanner.resume(identifier))
        services.maintenance.wake()
        return result

    @router.post("/api/import")
    async def upload(request: Request, filename: str) -> dict:
        maximum = effective_upload_limit(services.settings, files.limits)
        directory = Path(
            await anyio.to_thread.run_sync(
                lambda: tempfile.mkdtemp(
                    prefix=f"mangacouch-upload-{files.library.path.name}-", dir=files.library.path.parent
                )
            )
        )
        try:
            async with services.upload_slots:
                path = await receive_upload(request, directory, filename, maximum)

            def finish() -> dict:
                imported = files.import_file(path, original_filename=filename, consume=True)
                published = services.sync.publish(imported.book.id)
                warnings = list(imported.warnings)
                try:
                    previous = services.editions.candidates(imported.book.id)
                except (OSError, sqlite3.Error):
                    previous = []
                    warnings.append("previous_books_unavailable")
                    LOGGER.warning(
                        "Book %s imported; previous editions unavailable", imported.book.id, exc_info=True
                    )
                try:
                    book = services.book_output(imported.book)
                except (OSError, sqlite3.Error):
                    book = jsonable_encoder(imported.book)
                    warnings.append("book_details_unavailable")
                    LOGGER.warning(
                        "Book %s imported; extra details unavailable", imported.book.id, exc_info=True
                    )
                return {
                    "book": book,
                    "sidecar_sync": published,
                    "created": imported.created,
                    "warnings": warnings,
                    "previous_books": [item["id"] for item in previous],
                }

            result = await services.bounded(finish, timeout=1800)
        finally:
            await anyio.to_thread.run_sync(lambda: shutil.rmtree(directory, ignore_errors=True))
        services.downloads.wake.set()
        services.duplicates.poke()
        services.maintenance.wake()
        return result

    return router
