from __future__ import annotations

import json
import sqlite3

from .database import Library
from .models import Metadata, Source, site_group

MAX_EDITIONS = 100
BOOKS = (
    "SELECT b.id,json_extract(b.metadata_json,'$.title') AS title,"
    "json_extract(b.metadata_json,'$.original_title') AS original_title FROM book b WHERE "
)


def sites(source: Source) -> tuple[str, ...]:
    return site_group(source.site)


def with_source(db: sqlite3.Connection, source: Source, limit: int = MAX_EDITIONS) -> list[sqlite3.Row]:
    """Books whose own source is this gallery, on any host of its site (book_source_identity)."""
    return db.execute(
        BOOKS + "json_extract(b.metadata_json,'$.source.gallery_id')=? AND "
        "json_extract(b.metadata_json,'$.source.site') IN (SELECT value FROM json_each(?)) "
        "ORDER BY b.id LIMIT ?",
        (source.gallery_id, json.dumps(sites(source)), limit),
    ).fetchall()


def listing_newer(db: sqlite3.Connection, source: Source, limit: int = MAX_EDITIONS) -> list[sqlite3.Row]:
    """Books that name this gallery among their newer versions (book_newer)."""
    return db.execute(
        BOOKS + "b.id IN (SELECT book_id FROM book_newer WHERE site_group=? AND gallery_id=?) "
        "ORDER BY b.id LIMIT ?",
        (sites(source)[0], source.gallery_id, limit),
    ).fetchall()


def previous_editions(
    db: sqlite3.Connection, metadata: Metadata, limit: int = MAX_EDITIONS
) -> list[sqlite3.Row]:
    """Earlier editions of a book: its parent gallery and books that list it as their newer version."""
    found: dict[str, sqlite3.Row] = {}
    if metadata.parent:
        found.update((row["id"], row) for row in with_source(db, metadata.parent, limit))
    if metadata.source:
        found.update((row["id"], row) for row in listing_newer(db, metadata.source, limit))
    return [found[key] for key in sorted(found)][:limit]


def display_title(row: sqlite3.Row) -> str:
    return row["title"] or row["original_title"] or row["id"]


class Editions:
    """Finds earlier editions of a book through its parent and newer_versions source links."""

    def __init__(self, library: Library):
        self.library = library

    def previous_many(self, identifiers: list[str]) -> dict[str, list[str]]:
        with self.library.connection() as db:
            db.execute("BEGIN")
            result: dict[str, list[str]] = {identifier: [] for identifier in identifiers}
            rows = db.execute(
                "WITH selected AS (SELECT b.id,b.metadata_json FROM book b JOIN json_each(?) j ON "
                "b.id=j.value),"
                "linked AS (SELECT s.id,n.book_id AS previous_id FROM selected s JOIN book_newer n ON "
                "n.gallery_id=json_extract(s.metadata_json,'$.source.gallery_id') AND n.site_group=CASE "
                "json_extract(s.metadata_json,'$.source.site') WHEN 'exhentai' THEN 'ehentai' "
                "ELSE json_extract(s.metadata_json,'$.source.site') END UNION "
                "SELECT s.id,b.id FROM selected s JOIN book b ON "
                "json_extract(b.metadata_json,'$.source.gallery_id')="
                "json_extract(s.metadata_json,'$.parent.gallery_id') "
                "AND CASE b.source_site_key WHEN 'exhentai' THEN 'ehentai' ELSE b.source_site_key END=CASE "
                "json_extract(s.metadata_json,'$.parent.site') WHEN 'exhentai' THEN 'ehentai' "
                "ELSE json_extract(s.metadata_json,'$.parent.site') END) "
                "SELECT id,previous_id FROM linked WHERE id!=previous_id ORDER BY id,previous_id",
                (json.dumps(identifiers),),
            )
            for identifier, previous in rows:
                if len(result[identifier]) < MAX_EDITIONS:
                    result[identifier].append(previous)
        return result

    def candidates(self, book_id: str) -> list[dict]:
        """Earlier editions of a book as {id, title}, by id."""
        with self.library.connection() as db:
            db.execute("BEGIN")
            book = self.library.get_book(book_id, db)
            rows = previous_editions(db, book.metadata, MAX_EDITIONS + 1)
        return [{"id": row["id"], "title": display_title(row)} for row in rows if row["id"] != book_id][
            :MAX_EDITIONS
        ]
