from __future__ import annotations

import json
from collections import defaultdict

from .database import Library
from .models import Metadata, canonical_json


def sites(source) -> tuple[str, ...]:
    return ("ehentai", "exhentai") if source.site in {"ehentai", "exhentai"} else (source.site,)


class Editions:
    """Finds earlier editions of a book through its parent and newer_versions source links."""

    def __init__(self, library: Library):
        self.library = library

    def previous_many(self, identifiers: list[str]) -> dict[str, list[str]]:
        parents, newer = defaultdict(list), defaultdict(list)
        result = {identifier: [] for identifier in identifiers}
        with self.library.connection() as db:
            db.execute("BEGIN")
            for book in self.library.get_books(identifiers, db):
                for reference, mapping in ((book.metadata.parent, parents), (book.metadata.source, newer)):
                    if reference:
                        for site in sites(reference):
                            mapping[(site, reference.gallery_id)].append(book.id)
            if not parents and not newer:
                return result
            for row in db.execute(
                "SELECT id,json_extract(metadata_json,'$.source.site') AS site,"
                "json_extract(metadata_json,'$.source.gallery_id') AS gallery_id,"
                "json_extract(metadata_json,'$.newer_versions') AS newer FROM book ORDER BY id"
            ):
                matches = set(parents.get((row["site"], row["gallery_id"]), []))
                for reference in json.loads(row["newer"] or "[]"):
                    matches.update(newer.get((reference["site"], reference["gallery_id"]), []))
                for identifier in matches:
                    if row["id"] != identifier and len(result[identifier]) < 100:
                        result[identifier].append(row["id"])
        return result

    def candidates(self, new_id: str, after: str = "", limit: int = 100) -> dict:
        new = self.library.get_book(new_id)
        conditions, params = [], []
        if new.metadata.parent:
            parent = new.metadata.parent
            conditions.append(
                "(json_extract(b.metadata_json,'$.source.gallery_id')=? AND "
                "json_extract(b.metadata_json,'$.source.site') IN (SELECT value FROM json_each(?)))"
            )
            params.extend([parent.gallery_id, canonical_json(sites(parent))])
        if new.metadata.source:
            source = new.metadata.source
            conditions.append(
                "EXISTS(SELECT 1 FROM json_each(b.metadata_json,'$.newer_versions') n "
                "WHERE json_extract(n.value,'$.gallery_id')=? AND "
                "json_extract(n.value,'$.site') IN (SELECT value FROM json_each(?)))"
            )
            params.extend([source.gallery_id, canonical_json(sites(source))])
        if not conditions:
            return {"items": [], "after": None}
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT b.id,b.metadata_json FROM book b WHERE b.id>? AND b.id!=? AND ("
                + " OR ".join(conditions)
                + ") ORDER BY b.id LIMIT ?",
                (after, new_id, *params, limit),
            ).fetchall()
        items = []
        for row in rows:
            metadata = Metadata.model_validate_json(row["metadata_json"])
            items.append({"id": row["id"], "title": metadata.title or metadata.original_title or row["id"]})
        return {"items": items, "after": rows[-1]["id"] if len(rows) == limit else None}
