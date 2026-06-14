"""Tag routes — the tag cloud and on-demand translation (§6.1)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import ArchiveTag, Tag
from ...state import AppContext
from ..deps import get_context, get_db, require_reader

router = APIRouter(prefix="/api/tags", tags=["tags"])


@router.get("/stats")
def tag_stats(
    namespace: str | None = None,
    limit: int = Query(200, ge=1, le=2000),
    _: object = Depends(require_reader),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict:
    stmt = (
        select(Tag.namespace, Tag.value, func.count(ArchiveTag.archive_id).label("count"))
        .join(ArchiveTag, ArchiveTag.tag_id == Tag.id)
        .group_by(Tag.id)
        .order_by(func.count(ArchiveTag.archive_id).desc())
        .limit(limit)
    )
    if namespace is not None:
        stmt = stmt.where(Tag.namespace == namespace)
    rows = db.execute(stmt).all()
    return {
        "tags": [
            {
                "namespace": ns,
                "value": value,
                "translated": ctx.translator.translate(ns, value),
                "count": count,
            }
            for ns, value, count in rows
        ]
    }


@router.get("/translate")
def translate(
    ns: str,
    value: str,
    _: object = Depends(require_reader),
    ctx: AppContext = Depends(get_context),
) -> dict:
    return {"namespace": ns, "value": value, "translated": ctx.translator.display(ns, value)}
