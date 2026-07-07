"""Category routes — static (explicit membership) + dynamic (saved-search) categories (§5.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...db.models import Category, CategoryArchive
from ..deps import get_db, require_owner, require_reader

router = APIRouter(prefix="/api/categories", tags=["categories"])


class CategoryBody(BaseModel):
    name: str
    type: str = "static"  # "static" | "dynamic"
    predicate: str = ""
    pinned: bool = False


def _serialize(db: Session, cat: Category) -> dict:
    count = int(
        db.scalar(
            select(func.count())
            .select_from(CategoryArchive)
            .where(CategoryArchive.category_id == cat.id)
        )
        or 0
    )
    return {
        "id": cat.id,
        "name": cat.name,
        "type": cat.type,
        "predicate": cat.predicate,
        "pinned": cat.pinned,
        "count": count if cat.type == "static" else None,
    }


@router.get("")
def list_categories(_: object = Depends(require_reader), db: Session = Depends(get_db)) -> dict:
    cats = db.scalars(select(Category).order_by(Category.pinned.desc(), Category.name)).all()
    return {"categories": [_serialize(db, c) for c in cats]}


@router.post("")
def create_category(
    body: CategoryBody, _: object = Depends(require_owner), db: Session = Depends(get_db)
) -> dict:
    if body.type not in ("static", "dynamic"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type must be static|dynamic")
    cat = Category(
        name=body.name, type=body.type, predicate=body.predicate, pinned=body.pinned
    )
    db.add(cat)
    db.flush()
    return _serialize(db, cat)


@router.put("/{category_id}")
def update_category(
    category_id: int,
    body: CategoryBody,
    _: object = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    if body.type not in ("static", "dynamic"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "type must be static|dynamic")
    if cat.type == "static" and body.type == "dynamic":
        # Members are meaningless on a dynamic category — drop them instead of stranding rows.
        db.query(CategoryArchive).filter(CategoryArchive.category_id == cat.id).delete()
    cat.name = body.name
    cat.type = body.type
    cat.predicate = body.predicate
    cat.pinned = body.pinned
    db.flush()
    return _serialize(db, cat)


@router.delete("/{category_id}")
def delete_category(
    category_id: int, _: object = Depends(require_owner), db: Session = Depends(get_db)
) -> dict:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    db.delete(cat)
    return {"deleted": category_id}


@router.put("/{category_id}/{archive_id}")
def add_member(
    category_id: int,
    archive_id: str,
    _: object = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    cat = db.get(Category, category_id)
    if cat is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "category not found")
    if cat.type != "static":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot add members to a dynamic category")
    from ...db.models import Archive

    if db.get(Archive, archive_id) is None:  # FK violation would 500 otherwise
        raise HTTPException(status.HTTP_404_NOT_FOUND, "archive not found")
    exists = db.get(CategoryArchive, {"category_id": category_id, "archive_id": archive_id})
    if exists is None:
        db.add(CategoryArchive(category_id=category_id, archive_id=archive_id))
    return {"category_id": category_id, "archive_id": archive_id, "member": True}


@router.delete("/{category_id}/{archive_id}")
def remove_member(
    category_id: int,
    archive_id: str,
    _: object = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    link = db.get(CategoryArchive, {"category_id": category_id, "archive_id": archive_id})
    if link is not None:
        db.delete(link)
    return {"category_id": category_id, "archive_id": archive_id, "member": False}
