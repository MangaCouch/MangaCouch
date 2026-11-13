"""Settings, diagnostics, preferences, translations and snapshot routes."""

from __future__ import annotations

import asyncio
import secrets
import tempfile
import time
from pathlib import Path
from typing import Annotated

import anyio
from fastapi import APIRouter, Query, Request
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .configuration import effective_upload_limit
from .diagnostics import library_status
from .errors import NotFound
from .models import Model
from .preferences import LibraryPreferences, load_preferences, save_preferences
from .removals import RETENTION_SECONDS
from .services import Services, require_manager
from .sidecars import SYNC_STATE
from .snapshots import create_snapshot, snapshot_directory
from .translations import Translations
from .version import current_version
from .warming import WarmKind

PREPARED_SECONDS = 600
"""How long a prepared backup waits for its download before it is deleted."""


class PreferencesInput(Model):
    settings: LibraryPreferences


def settings_router(services: Services) -> APIRouter:
    router = APIRouter()
    files, library = services.files, services.files.library

    def cache_status() -> dict:
        cache = services.thumbnail_cache
        return {
            "cache": {"budget_bytes": cache.disk_bytes, "used_bytes": cache.used_bytes()},
            **{kind: warmer.summary() for kind, warmer in services.warmers.items()},
        }

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
            return {
                "version": current_version(),
                "review": db.execute(
                    "SELECT count(*) FROM book WHERE review_state='needs_review'"
                ).fetchone()[0],
                "sync": sync,
                **services.duplicates.features.status(),
                "translations": db.execute("SELECT count(*) FROM tag_alias").fetchone()[0],
                "devices": db.execute(
                    "SELECT count(*) FROM credential WHERE kind='session' AND revoked_at IS NULL "
                    "AND (expires_at IS NULL OR expires_at>?)",
                    (now,),
                ).fetchone()[0],
                "recycle": db.execute(
                    "SELECT count(*) FROM removal WHERE created_at>?", (now - RETENTION_SECONDS,)
                ).fetchone()[0],
                "cache": cache_status(),
            }

    @router.get("/api/preferences")
    def preferences() -> dict:
        return load_preferences(library).model_dump()

    @router.put("/api/preferences")
    def preferences_save(expected_revision: Annotated[int, Query(ge=0)], data: PreferencesInput) -> dict:
        return save_preferences(library, data.settings, expected_revision=expected_revision).model_dump()

    @router.get("/api/settings")
    def limits() -> dict:
        return {"max_upload_bytes": effective_upload_limit(services.settings, files.limits)}

    @router.get("/api/cache")
    def cache(request: Request) -> dict:
        require_manager(request)
        return cache_status()

    @router.post("/api/cache/{kind}/warm")
    def warm_cache(request: Request, kind: WarmKind) -> dict:
        """Render every cover (or page thumbnail) the cache lacks, even when nothing changed."""
        require_manager(request)
        services.warmers[kind].request()
        services.maintenance.wake()
        return cache_status()

    @router.get("/api/translations")
    def translation_status() -> dict:
        return Translations(library).status()

    @router.post("/api/translations/update")
    async def update_translations() -> dict:
        translations = Translations(library)
        data = await anyio.to_thread.run_sync(translations.fetch)
        await services.bounded(lambda: translations.import_data(data))
        return translations.status()

    prepared: dict[str, tuple[tempfile.TemporaryDirectory, Path]] = {}

    async def build_snapshot() -> tuple[tempfile.TemporaryDirectory, Path]:
        def prepare() -> tempfile.TemporaryDirectory:
            directory = snapshot_directory(library)
            directory.mkdir(mode=0o700, exist_ok=True)
            return tempfile.TemporaryDirectory(prefix="prepared-", dir=directory, ignore_cleanup_errors=True)

        temporary = await anyio.to_thread.run_sync(prepare)
        try:
            path = await services.bounded(
                lambda: create_snapshot(files, Path(temporary.name) / "snapshot.zip"), timeout=1800
            )
        except BaseException:
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(temporary.cleanup)
            raise
        return temporary, path

    def send(temporary: tempfile.TemporaryDirectory, path: Path) -> FileResponse:
        return FileResponse(
            path,
            media_type="application/zip",
            filename="mangacouch-snapshot.zip",
            background=BackgroundTask(temporary.cleanup),
        )

    async def discard(identifier: str) -> None:
        entry = prepared.pop(identifier, None)
        if entry:
            await anyio.to_thread.run_sync(entry[0].cleanup)

    @router.post("/api/snapshots")
    async def prepare_snapshot(request: Request) -> dict:
        """Build a backup for the browser to download natively from /api/snapshots/{id}: the
        build's failures reach the page as errors, and the download only streams a finished file.
        Each prepared backup is downloaded once, or deleted after PREPARED_SECONDS."""
        require_manager(request)
        temporary, path = await build_snapshot()
        size = await anyio.to_thread.run_sync(lambda: path.stat().st_size)
        identifier = secrets.token_urlsafe(18)
        for old in list(prepared):
            await discard(old)
        prepared[identifier] = (temporary, path)
        asyncio.get_running_loop().call_later(
            PREPARED_SECONDS, lambda: asyncio.create_task(discard(identifier))
        )
        return {"id": identifier, "size": size}

    @router.get("/api/snapshots/{identifier}")
    def prepared_snapshot(request: Request, identifier: str) -> FileResponse:
        require_manager(request)
        entry = prepared.pop(identifier, None)
        if entry is None:
            raise NotFound("This backup is no longer available; create it again")
        return send(*entry)

    return router
