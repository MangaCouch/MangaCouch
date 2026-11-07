"""Settings, diagnostics, preferences, translations and snapshot routes."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Request
from pydantic import Field
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .configuration import effective_upload_limit
from .diagnostics import library_status
from .models import Model
from .preferences import LibraryPreferences, load_preferences, save_preferences
from .services import Services, require_manager
from .sidecars import SYNC_STATE
from .snapshots import create_snapshot
from .translations import Translations


class PreferencesInput(Model):
    settings: LibraryPreferences
    expected_revision: Annotated[int, Field(ge=0)]


def settings_router(services: Services) -> APIRouter:
    router = APIRouter()
    files, library = services.files, services.files.library

    @router.get("/api/diagnostics")
    def diagnostics(request: Request) -> dict:
        require_manager(request)
        return library_status(files)

    @router.get("/api/settings/summary")
    def settings_summary(request: Request) -> dict:
        require_manager(request)
        now = int(time.time())
        with library.connection() as db:
            db.execute("BEGIN")
            sync = dict(
                db.execute(
                    f"SELECT {SYNC_STATE} AS state,count(*) FROM sidecar_sync GROUP BY state"
                ).fetchall()
            )
            updated = db.execute("SELECT updated_at FROM translation_update").fetchone()
            return {
                "review": db.execute(
                    "SELECT count(*) FROM book WHERE review_state='needs_review'"
                ).fetchone()[0],
                "sync": sync,
                **services.duplicates.features.status(),
                "translations": db.execute("SELECT count(*) FROM tag_alias").fetchone()[0],
                "translations_updated_at": updated[0] if updated else None,
                "devices": db.execute(
                    "SELECT count(*) FROM credential WHERE kind='session' AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at>?)",
                    (now,),
                ).fetchone()[0],
                "recycle": db.execute(
                    "SELECT count(*) FROM removal WHERE created_at>?", (now - 7 * 86400,)
                ).fetchone()[0],
            }

    @router.get("/api/preferences")
    def preferences() -> dict:
        return load_preferences(library).model_dump()

    @router.put("/api/preferences")
    def preferences_save(data: PreferencesInput) -> dict:
        return save_preferences(library, data.settings, expected_revision=data.expected_revision).model_dump()

    @router.get("/api/settings")
    def limits() -> dict:
        return {"max_upload_bytes": effective_upload_limit(services.settings, files.limits)}

    @router.get("/api/translations")
    def translation_status() -> dict:
        return Translations(library).status()

    @router.post("/api/translations/update")
    async def update_translations() -> dict:
        return await services.bounded(lambda: Translations(library).update())

    @router.get("/api/snapshots")
    async def snapshot(request: Request) -> FileResponse:
        require_manager(request)
        temporary = tempfile.TemporaryDirectory(prefix="mangacouch-http-snapshot-")
        try:
            path = await services.bounded(
                lambda: create_snapshot(files, Path(temporary.name) / "snapshot.zip")
            )
        except BaseException:
            temporary.cleanup()
            raise
        return FileResponse(
            path,
            media_type="application/zip",
            filename="mangacouch-snapshot.zip",
            background=BackgroundTask(temporary.cleanup),
        )

    return router
