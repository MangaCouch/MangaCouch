"""Acquisition routes — enqueue a download, inspect jobs, and the GP balance calculator (§5.3)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...acquisition.ehentai import (
    EHentaiError,
    NotLoggedInError,
    fetch_archiver_page,
    parse_gallery_url,
)
from ...db.models import DownloadJob
from ...state import AppContext
from ..deps import get_context, get_db, require_owner, require_reader

router = APIRouter(prefix="/api", tags=["downloads"])

_LOGIN_NAMESPACE = "ehentai_login"


class DownloadRequest(BaseModel):
    url: str
    catid: int | None = None
    dltype: str | None = None
    priority: int = 0


def _serialize_job(job: DownloadJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "url": job.url,
        "gid": job.gid,
        "token": job.token,
        "domain": job.domain,
        "dltype": job.dltype,
        "state": job.state,
        "priority": job.priority,
        "progress": job.progress,
        "gp_cost": job.gp_cost,
        "gp_balance": job.gp_balance,
        "archive_id": job.archive_id,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
    }


@router.post("/download")
def create_download(
    body: DownloadRequest,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    if ctx.registry.find_download_plugin(body.url) is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "no download plugin handles this URL")
    # Dedup-on-download: if we already have this source, surface it (§5.2).
    from ...db.models import Archive

    existing = db.scalar(select(Archive).where(Archive.source_url == body.url))
    dltype = body.dltype or ctx.config.acquisition.default_dltype
    job = ctx.download_worker.enqueue(
        db, body.url, dltype=dltype, catid=body.catid, priority=body.priority
    )
    db.flush()
    ctx.download_worker.notify()
    data = _serialize_job(job)
    if existing is not None:
        data["already_have"] = existing.id
    return data


@router.get("/jobs")
def list_jobs(
    state: str | None = None,
    limit: int = 100,
    _: object = Depends(require_reader),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    stmt = select(DownloadJob).order_by(DownloadJob.created_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(DownloadJob.state == state)
    return {"jobs": [_serialize_job(j) for j in db.scalars(stmt).all()]}


@router.get("/job/{job_id}")
def get_job(
    job_id: int, _: object = Depends(require_reader), db: Session = Depends(get_db)
) -> dict[str, Any]:
    job = db.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return _serialize_job(job)


class PriorityBody(BaseModel):
    priority: int


@router.post("/job/{job_id}/priority")
def set_priority(
    job_id: int,
    body: PriorityBody,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    job = db.get(DownloadJob, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    job.priority = body.priority
    db.flush()
    ctx.download_worker.notify()
    return _serialize_job(job)


@router.get("/ehentai/balance")
def gp_balance(
    url: str,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    """The GP balance calculator — parse the live Original/Resample cost and current GP."""
    try:
        ref = parse_gallery_url(url)
    except EHentaiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    session = ctx.download_worker.login_session(_LOGIN_NAMESPACE)
    try:
        with ctx.rate_limiter.slot(ref.domain):
            page = fetch_archiver_page(session, ref)
    except NotLoggedInError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except EHentaiError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    enough = page.current_gp is None or page.original_cost is None or (
        page.current_gp >= page.original_cost
    )
    return {
        "gid": ref.gid,
        "token": ref.token,
        "domain": ref.domain,
        "current_gp": page.current_gp,
        "credits": page.credits,
        "original_cost": page.original_cost,
        "resample_cost": page.resample_cost,
        "sufficient_for_original": enough,
    }
