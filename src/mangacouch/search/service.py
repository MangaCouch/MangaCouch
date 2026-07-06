"""Compile a parsed :class:`Query` + filters + sort into a SQL query over ``library.sqlite``.

Text terms are resolved through the FTS5 trigram index (§5.1); structured predicates (numeric,
category, the new/untagged/completed filters) and sorting run against the relational tables. The
two are combined by id-set intersection — correct and simple for a personal-scale library; the
``tantivy`` sidecar (§7) is the future upgrade path for very large collections.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Select, and_, exists, func, not_, select
from sqlalchemy.orm import Session, selectinload

from ..db.models import Archive, ArchiveTag, CategoryArchive, Progress, Tag
from .index import SearchIndex
from .query import BASIC_NAMESPACES, NumericTerm, Query

_READ_FRACTION = 0.85  # "read"/completed = progress/page_count > 85% (§5.7)
_OPS = {"<": "__lt__", "<=": "__le__", ">": "__gt__", ">=": "__ge__", "=": "__eq__"}


@dataclass(slots=True)
class SearchResult:
    items: list[Archive]
    total: int
    page: int
    per_page: int


def _apply_numeric(stmt: Select, term: NumericTerm) -> Select:
    op = _OPS.get(term.op, "__eq__")
    if term.field == "pages":
        col = Archive.page_count
    elif term.field == "read":
        col = func.coalesce(Progress.page, 0)
    else:
        return stmt
    cond = getattr(col, op)(term.value)
    return stmt.where(not_(cond) if term.negate else cond)


def _text_id_constraints(index: SearchIndex, query: Query) -> tuple[set[str] | None, set[str]]:
    """Return ``(positive_ids | None, negative_ids)`` from the FTS index.

    ``positive_ids is None`` means "no positive text constraint" (browse everything).
    """
    positive: set[str] | None = None
    negative: set[str] = set()
    for term in query.text_terms:
        ids = index.match_ids(term)
        if term.negate:
            negative |= ids
        else:
            positive = ids if positive is None else (positive & ids)
    return positive, negative


def search_archives(
    session: Session,
    index: SearchIndex,
    query: Query,
    *,
    static_category_id: int | None = None,
    sort: str = "date_added",
    sortdir: str = "desc",
    page: int = 1,
    per_page: int = 50,
    basic_namespaces: frozenset[str] = BASIC_NAMESPACES,
) -> SearchResult:
    page = max(1, page)
    per_page = max(1, min(per_page, 500))

    positive_ids, negative_ids = _text_id_constraints(index, query)

    stmt = select(Archive)
    needs_progress = any(t.field == "read" for t in query.numeric_terms) or query.filters.newonly \
        or query.filters.hidecompleted or sort == "lastread"
    if needs_progress:
        stmt = stmt.outerjoin(Progress, Progress.archive_id == Archive.id)

    if positive_ids is not None:
        if not positive_ids:
            return SearchResult(items=[], total=0, page=page, per_page=per_page)
        stmt = stmt.where(Archive.id.in_(positive_ids))
    if negative_ids:
        stmt = stmt.where(Archive.id.notin_(negative_ids))

    for term in query.numeric_terms:
        stmt = _apply_numeric(stmt, term)

    # Filters layered on top (§5.1).
    if query.filters.newonly:
        stmt = stmt.where(func.coalesce(Progress.page, 0) == 0)
    if query.filters.hidecompleted:
        stmt = stmt.where(
            func.coalesce(Progress.page, 0) <= _READ_FRACTION * func.max(Archive.page_count, 1)
        )
    if query.filters.untaggedonly:
        # No tag outside the "basic" namespaces counts as untagged.
        tagged = exists().where(
            and_(
                ArchiveTag.archive_id == Archive.id,
                ArchiveTag.tag_id == Tag.id,
                Tag.namespace.notin_(tuple(basic_namespaces)),
            )
        )
        stmt = stmt.where(not_(tagged))

    if static_category_id is not None:
        stmt = stmt.where(
            Archive.id.in_(
                select(CategoryArchive.archive_id).where(
                    CategoryArchive.category_id == static_category_id
                )
            )
        )

    # Total (count over the same predicates, without ordering/pagination).
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(session.execute(count_stmt).scalar_one())

    stmt = _apply_sort(stmt, sort, sortdir)
    stmt = stmt.limit(per_page).offset((page - 1) * per_page)
    # Eager-load what serialisation touches — without this, a 50-card page costs
    # hundreds of lazy-load queries (tags per card, tag per link, progress per card).
    stmt = stmt.options(
        selectinload(Archive.tags).selectinload(ArchiveTag.tag),
        selectinload(Archive.progress),
    )
    items = list(session.execute(stmt).scalars().unique().all())
    return SearchResult(items=items, total=total, page=page, per_page=per_page)


def _apply_sort(stmt: Select, sort: str, sortdir: str) -> Select:
    descending = sortdir.lower() != "asc"

    def d(col):
        return col.desc() if descending else col.asc()

    if sort == "title":
        return stmt.order_by(d(Archive.title.collate("NATURAL")))
    if sort == "lastread":
        return stmt.order_by(d(func.coalesce(Progress.updated_at, Archive.added_at)))
    if sort == "random":
        return stmt.order_by(func.random())
    if sort not in ("date_added", "", None):
        # Sort by an arbitrary tag namespace value (correlated subquery).
        sub = (
            select(func.min(Tag.value))
            .select_from(ArchiveTag)
            .join(Tag, Tag.id == ArchiveTag.tag_id)
            .where(and_(ArchiveTag.archive_id == Archive.id, Tag.namespace == sort))
            .scalar_subquery()
        )
        return stmt.order_by(d(sub), d(Archive.added_at))
    return stmt.order_by(d(Archive.added_at))
