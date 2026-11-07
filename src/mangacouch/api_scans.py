"""Import-folder scans and archive upload routes."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import anyio
from fastapi import APIRouter, Request

from .configuration import effective_upload_limit
from .services import Services, require_manager
from .uploads import receive_upload


def scans_router(services: Services) -> APIRouter:
    router = APIRouter()
    files, scanner = services.files, services.scanner

    @router.get("/api/scans")
    def current_scan(request: Request) -> dict:
        require_manager(request)
        return {"scan": scanner.latest(), "root": str(files.root.path / "import")}

    @router.post("/api/scans")
    async def start_scan(keep_source: bool = False) -> dict:
        result = await services.bounded(lambda: scanner.start(keep_source=keep_source))
        services.maintenance.wake()
        return result

    @router.get("/api/scans/{identifier}")
    def scan_status(request: Request, identifier: str, after: int = 0, limit: int = 50) -> dict:
        require_manager(request)
        return scanner.get(identifier, after=after, limit=limit)

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
        async with services.upload_slots:
            directory = Path(
                await anyio.to_thread.run_sync(
                    lambda: tempfile.mkdtemp(
                        prefix=f"mangacouch-upload-{files.library.path.name}-", dir=files.library.path.parent
                    )
                )
            )
            try:
                path = await receive_upload(request, directory, filename, maximum)

                def finish() -> dict:
                    imported = files.import_file(path, original_filename=filename, consume=True)
                    previous = services.editions.candidates(imported.book.id)
                    return {
                        "book": services.book_output(imported.book),
                        "created": imported.created,
                        "warnings": imported.warnings,
                        "previous_books": [item["id"] for item in previous["items"]],
                    }

                result = await services.bounded(finish)
            finally:
                await anyio.to_thread.run_sync(lambda: shutil.rmtree(directory, ignore_errors=True))
        services.downloads.wake.set()
        services.duplicates.poke()
        services.maintenance.wake()
        return result

    return router
