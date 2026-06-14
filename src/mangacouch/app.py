"""The FastAPI application factory (§3).

Builds the single-process app: the REST surface (auto-generated OpenAPI), the in-process
subsystems wired through :class:`AppContext`, and the bundled React PWA served as static assets.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api.routers import (
    admin,
    archives,
    auth,
    categories,
    downloads,
    favorites,
    opds,
    plugins,
    tags,
    upload,
)
from .config import Config, load_config
from .state import AppContext, build_context

log = logging.getLogger("mangacouch")

WEB_DIR = Path(__file__).parent / "web"


def create_app(config: Config | None = None, *, context: AppContext | None = None) -> FastAPI:
    cfg = config or (context.config if context else load_config())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = context or build_context(cfg)
        app.state.context = ctx
        ctx.startup()
        log.info("MangaCouch %s ready — manga root: %s", __version__, cfg.manga_root)
        try:
            yield
        finally:
            ctx.shutdown()

    app = FastAPI(
        title="MangaCouch",
        version=__version__,
        summary="Self-hosted manga library + e-hentai archiver.",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.server.cors_origins,
        # The browser extension's origin is chrome-extension:// or moz-extension://.
        allow_origin_regex=r"^(chrome-extension|moz-extension|safari-web-extension)://.*$",
        allow_credentials=False,  # we authenticate with Bearer tokens, not cookies
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for module in (
        auth,
        archives,
        tags,
        categories,
        favorites,
        downloads,
        upload,
        plugins,
        admin,
        opds,
    ):
        app.include_router(module.router)

    @app.get("/api/health", tags=["meta"])
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    _mount_spa(app)
    return app


def _mount_spa(app: FastAPI) -> None:
    """Serve the built PWA, falling back to ``index.html`` for client-side routes."""
    assets = WEB_DIR / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = WEB_DIR / "index.html"

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str, request: Request):
        if full_path.startswith(("api/", "assets/")):
            return JSONResponse({"detail": "not found"}, status_code=404)
        candidate = WEB_DIR / full_path
        if full_path and candidate.is_file() and candidate.resolve().is_relative_to(WEB_DIR.resolve()):
            return FileResponse(candidate)
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(
            {
                "detail": "MangaCouch API is running, but the web UI has not been built.",
                "hint": "Build the frontend (cd frontend && npm install && npm run build) "
                "and copy frontend/dist into src/mangacouch/web, or use the API directly.",
                "docs": "/docs",
            },
            status_code=200,
        )
