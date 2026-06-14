"""Multi-list favorites (§5.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import Favorite, FavoriteList
from ..deps import get_db, require_owner, require_reader

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


class FavoriteListBody(BaseModel):
    name: str
    position: int = 0


def _serialize(db: Session, fl: FavoriteList) -> dict:
    archive_ids = db.scalars(
        select(Favorite.archive_id)
        .where(Favorite.list_id == fl.id)
        .order_by(Favorite.added_at.desc())
    ).all()
    return {
        "id": fl.id,
        "name": fl.name,
        "position": fl.position,
        "count": len(archive_ids),
        "archive_ids": list(archive_ids),
    }


@router.get("/lists")
def list_lists(_: object = Depends(require_reader), db: Session = Depends(get_db)) -> dict:
    lists = db.scalars(select(FavoriteList).order_by(FavoriteList.position, FavoriteList.id)).all()
    return {"lists": [_serialize(db, fl) for fl in lists]}


@router.post("/lists")
def create_list(
    body: FavoriteListBody, _: object = Depends(require_owner), db: Session = Depends(get_db)
) -> dict:
    max_pos = db.scalar(select(func.max(FavoriteList.position))) or 0
    fl = FavoriteList(name=body.name, position=body.position or (max_pos + 1))
    db.add(fl)
    db.flush()
    return _serialize(db, fl)


@router.delete("/lists/{list_id}")
def delete_list(
    list_id: int, _: object = Depends(require_owner), db: Session = Depends(get_db)
) -> dict:
    fl = db.get(FavoriteList, list_id)
    if fl is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "list not found")
    db.delete(fl)
    return {"deleted": list_id}


@router.put("/{list_id}/{archive_id}")
def add_favorite(
    list_id: int,
    archive_id: str,
    _: object = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    if db.get(FavoriteList, list_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "list not found")
    if db.get(Favorite, {"list_id": list_id, "archive_id": archive_id}) is None:
        db.add(Favorite(list_id=list_id, archive_id=archive_id))
    return {"list_id": list_id, "archive_id": archive_id, "favorited": True}


@router.delete("/{list_id}/{archive_id}")
def remove_favorite(
    list_id: int,
    archive_id: str,
    _: object = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    link = db.get(Favorite, {"list_id": list_id, "archive_id": archive_id})
    if link is not None:
        db.delete(link)
    return {"list_id": list_id, "archive_id": archive_id, "favorited": False}
