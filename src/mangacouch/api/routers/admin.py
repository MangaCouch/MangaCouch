"""Admin routes — config, library scan, thumbnail regen, stats, history (§6.1, §6.2)."""

from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import config as config_mod
from ...db.models import Archive, ArchiveTag, DownloadJob, History, Tag
from ...state import AppContext
from ..deps import get_context, get_db, require_owner, require_reader

router = APIRouter(prefix="/api", tags=["admin"])


def _config_payload(ctx: AppContext) -> dict[str, Any]:
    payload = config_mod.to_toml_dict(ctx.config)
    payload["paths"]["_resolved"] = {
        "database": str(ctx.config.database_root),
        "cache": str(ctx.config.cache_root),
        "manga": str(ctx.config.manga_root),
    }
    return payload


@router.get("/config")
def get_config(_: object = Depends(require_owner), ctx: AppContext = Depends(get_context)) -> dict:
    return _config_payload(ctx)


class ConfigUpdate(BaseModel):
    server: dict[str, Any] | None = None
    acquisition: dict[str, Any] | None = None
    reader: dict[str, Any] | None = None
    thumbnails: dict[str, Any] | None = None
    auth: dict[str, Any] | None = None


@router.put("/config")
def put_config(
    body: ConfigUpdate, _: object = Depends(require_owner), ctx: AppContext = Depends(get_context)
) -> dict:
    cfg = ctx.config
    if body.server:
        _apply(cfg.server, body.server)
    if body.acquisition:
        _apply(cfg.acquisition, body.acquisition)
    if body.reader:
        _apply(cfg.reader, body.reader)
    if body.thumbnails:
        _apply(cfg.thumbnails, body.thumbnails)
    if body.auth:
        _apply(cfg.auth, body.auth)

    # Apply runtime-affecting changes immediately.
    ctx.rate_limiter.configure(
        cfg.acquisition.rate_limit_interval_seconds, cfg.acquisition.rate_limit_concurrency
    )
    worker = ctx.download_worker
    worker.proxy = cfg.acquisition.proxy or None
    worker.proxy_scope = cfg.acquisition.proxy_scope
    worker.gp_short_behavior = cfg.acquisition.gp_short_behavior
    worker.user_agent = cfg.acquisition.user_agent
    worker.invalidate_sessions()

    config_mod.save_config(cfg)
    return _config_payload(ctx)


def _apply(target: object, updates: dict[str, Any]) -> None:
    """Apply updates onto a config dataclass, coercing/validating against the current field
    types — a bad value must 422 here, not brick the next startup via config.toml."""
    for key, value in updates.items():
        if not hasattr(target, key) or key.startswith("_"):
            continue
        try:
            setattr(target, key, _coerce(value, getattr(target, key)))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"invalid config value for {key!r}: {exc}"
            ) from exc


def _coerce(value: Any, current: Any) -> Any:
    if isinstance(current, bool):
        if isinstance(value, bool):
            return value
        raise ValueError("expected a boolean")
    if isinstance(current, int):
        if isinstance(value, bool):
            raise ValueError("expected an integer")
        return int(value)
    if isinstance(current, float):
        if isinstance(value, bool):
            raise ValueError("expected a number")
        return float(value)
    if isinstance(current, str):
        if isinstance(value, str):
            return value
        raise ValueError("expected a string")
    return value  # None / untyped: accept as-is


@router.post("/library/scan")
def scan(_: object = Depends(require_owner), ctx: AppContext = Depends(get_context)) -> dict:
    def _run() -> None:
        ctx.ingestor.scan()

    threading.Thread(target=_run, name="mc-scan", daemon=True).start()
    return {"started": True}


@router.post("/thumbnails/regen")
def regen_thumbnails(
    _: object = Depends(require_owner), ctx: AppContext = Depends(get_context)
) -> dict:
    def _run() -> None:
        from ...core.archives import open_archive
        from ...core.thumbnails import generate_cover

        ctx.thumbs.clear()
        cfg = ctx.config.thumbnails
        from ...db.base import session_scope

        with session_scope() as session:
            rows = session.execute(select(Archive.id, Archive.rel_path)).all()
        for archive_id, rel_path in rows:
            path = ctx.config.manga_root / rel_path
            if not path.exists():
                continue
            try:
                with open_archive(path) as reader:
                    generate_cover(
                        ctx.thumbs, archive_id, reader, size=cfg.cover_size, quality=cfg.quality
                    )
            except Exception:  # noqa: BLE001
                continue

    threading.Thread(target=_run, name="mc-thumb-regen", daemon=True).start()
    return {"started": True}


@router.get("/stats")
def stats(_: object = Depends(require_reader), db: Session = Depends(get_db)) -> dict:
    total = int(db.scalar(select(func.count()).select_from(Archive)) or 0)
    pages = int(db.scalar(select(func.coalesce(func.sum(Archive.page_count), 0))) or 0)
    size = int(db.scalar(select(func.coalesce(func.sum(Archive.size), 0))) or 0)
    tags = int(db.scalar(select(func.count()).select_from(Tag)) or 0)
    by_format = {
        fmt: int(count)
        for fmt, count in db.execute(
            select(Archive.format, func.count()).group_by(Archive.format)
        ).all()
    }
    top_artists = [
        {"value": value, "count": count}
        for value, count in db.execute(
            select(Tag.value, func.count(ArchiveTag.archive_id))
            .join(ArchiveTag, ArchiveTag.tag_id == Tag.id)
            .where(Tag.namespace == "artist")
            .group_by(Tag.id)
            .order_by(func.count(ArchiveTag.archive_id).desc())
            .limit(10)
        ).all()
    ]
    jobs = {
        state: int(count)
        for state, count in db.execute(
            select(DownloadJob.state, func.count()).group_by(DownloadJob.state)
        ).all()
    }
    return {
        "archives": total,
        "pages": pages,
        "bytes": size,
        "tags": tags,
        "by_format": by_format,
        "top_artists": top_artists,
        "download_jobs": jobs,
    }


@router.get("/history")
def history(
    limit: int = 50, _: object = Depends(require_reader), db: Session = Depends(get_db)
) -> dict:
    rows = db.execute(
        select(History.archive_id, func.max(History.opened_at), Archive.title)
        .join(Archive, Archive.id == History.archive_id)
        .group_by(History.archive_id)
        .order_by(func.max(History.opened_at).desc())
        .limit(limit)
    ).all()
    return {
        "history": [
            {"archive_id": aid, "opened_at": opened.isoformat() if opened else None, "title": title}
            for aid, opened, title in rows
        ]
    }
