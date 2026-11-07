from __future__ import annotations

import hashlib
import re
import sqlite3
import sys
import threading
from bisect import bisect_right
from collections import OrderedDict
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .errors import InvalidInput
from .models import TAG_NAMESPACES, Model, normalized

# A quoted tag (ns:"value"), a quoted phrase, or a bare word; stray quotes are dropped.
TOKEN = re.compile(r'([^\s":]+):"([^"]*)"|"([^"]*)"|([^\s"]+)')


class SearchOptions(Model):
    language: Annotated[str, Field(max_length=128)] = ""
    source: Literal["", "ehentai", "exhentai", "eh", "nhentai", "none"] = ""
    work_type: Annotated[str, Field(max_length=128)] = ""
    reading: Literal["all", "unfinished", "unread", "completed", "in_progress"] = "all"
    review: Literal["all", "needs_review", "confirmed"] = "all"
    rating: Literal["all", "unrated", "rated"] = "all"
    min_rating: Annotated[float, Field(ge=0, le=5, multiple_of=0.5, allow_inf_nan=False)] | None = None
    source_rating_min: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)] | None = None
    order: Literal["default", "random", "recent"] = "default"
    seed: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.order == "random" and self.seed is None:
            raise ValueError("Random order requires a seed")
        if self.min_rating is not None and self.rating != "rated":
            raise ValueError("Minimum rating requires rated books")
        return self


def random_key(seed: str, identifier: str) -> str:
    return hashlib.sha256(f"{seed}:{identifier}".encode()).hexdigest()


class _RandomResults:
    """Bounded, process-local ordered IDs; database tokens identify exact search snapshots."""

    def __init__(self, budget: int = 32 * 1024**2, capacity: int = 16):
        self.budget, self.capacity = budget, capacity
        self.entries: OrderedDict[tuple[str, ...], tuple[tuple[str, ...], int]] = OrderedDict()
        self.bytes = 0
        self.lock = threading.Lock()

    def get(self, key: tuple[str, ...]) -> tuple[str, ...] | None:
        with self.lock:
            if key not in self.entries:
                return None
            self.entries.move_to_end(key)
            return self.entries[key][0]

    def put(self, key: tuple[str, ...], identifiers: tuple[str, ...]) -> None:
        size = sys.getsizeof(key) + sum(sys.getsizeof(part) for part in key)
        size += sys.getsizeof(identifiers) + sum(sys.getsizeof(identifier) for identifier in identifiers)
        if size > self.budget:
            return
        with self.lock:
            previous = self.entries.pop(key, None)
            self.bytes -= previous[1] if previous else 0
            self.entries[key] = (identifiers, size)
            self.bytes += size
            while self.bytes > self.budget or len(self.entries) > self.capacity:
                _, (_, removed) = self.entries.popitem(last=False)
                self.bytes -= removed


_random_results = _RandomResults()


def _random_ids(
    db: sqlite3.Connection,
    query: str,
    options: SearchOptions,
    conditions: list[str],
    values: list[str | int | float],
    limit: int,
    after_id: str,
    offset: int,
) -> list[str]:
    seed = options.seed
    assert seed is not None
    # The revision and the result must come from the same SQLite snapshot, including
    # when a caller has already opened a transaction containing uncommitted edits.
    own_snapshot = not db.in_transaction
    if own_snapshot:
        db.execute("BEGIN")
    try:
        path = next(row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main")
        token = db.execute("SELECT token FROM search_revision WHERE singleton=1").fetchone()[0]
        key = (path, token, query, options.model_dump_json())
        identifiers = _random_results.get(key)
        if identifiers is None:
            where = " AND ".join(conditions) or "1"
            # Bound a cache build too, not just retained entries. Larger result sets
            # keep SQLite's bounded LIMIT sorter instead of materializing all IDs.
            max_rows = _random_results.budget // (sys.getsizeof("0" * 36) + 8)
            candidates = [
                row[0]
                for row in db.execute(
                    f"SELECT b.id FROM book b JOIN personal p ON p.book_id=b.id WHERE {where} LIMIT ?",
                    (*values, max_rows + 1),
                )
            ]
            if len(candidates) > max_rows:
                db.create_function(
                    "mc_random_order", 1, lambda identifier: random_key(seed, identifier), deterministic=True
                )
                if after_id:
                    where += " AND (mc_random_order(b.id),b.id)>(?,?)"
                    values = [*values, random_key(seed, after_id), after_id]
                return [
                    row[0]
                    for row in db.execute(
                        f"SELECT b.id FROM book b JOIN personal p ON p.book_id=b.id WHERE {where} "
                        "ORDER BY mc_random_order(b.id),b.id LIMIT ? OFFSET ?",
                        (*values, limit, offset),
                    )
                ]
            identifiers = tuple(sorted(candidates, key=lambda value: (random_key(seed, value), value)))
            _random_results.put(key, identifiers)
        start = offset
        if after_id:
            start += bisect_right(
                identifiers,
                (random_key(seed, after_id), after_id),
                key=lambda value: (random_key(seed, value), value),
            )
        return list(identifiers[start : start + limit])
    finally:
        if own_snapshot:
            db.execute("ROLLBACK")


def search_ids(
    db: sqlite3.Connection,
    query: str,
    *,
    limit: int,
    after_id: str,
    options: SearchOptions | None,
    offset: int = 0,
) -> list[str]:
    if not 1 <= limit <= 200 or len(query) > 1024 or len(after_id) > 128 or not 0 <= offset <= 10_000_000:
        raise InvalidInput("Search bounds exceeded")
    options = SearchOptions.model_validate(options or SearchOptions())
    conditions, values = rule_conditions(query, options)
    order, source = "b.id", "book b JOIN personal p ON p.book_id=b.id"
    if options.order == "random" and options.seed is not None:
        return _random_ids(db, query, options, conditions, values, limit, after_id, offset)
    elif options.order == "recent":
        order = "b.recent_order,b.id"
        if after_id:
            row = db.execute("SELECT recent_order FROM book WHERE id=?", (after_id,)).fetchone()
            stamp = row[0] if row else 0
            conditions.append("(b.recent_order,b.id)>(?,?)")
            values.extend([stamp, after_id])
    else:
        conditions.append("b.id>?")
        values.append(after_id)
    where = " AND ".join(conditions) or "1"
    rows = db.execute(
        f"SELECT b.id FROM {source} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*values, limit, offset),
    ).fetchall()
    return [row["id"] for row in rows]


def search_terms(query: str) -> list[tuple[str, str] | str]:
    """Split a query into tag filters (namespace, value) and free-text terms.

    `ns:value` filters on a tag only for a known namespace or a quoted value (`ns:"value"`,
    as the search box inserts tags), and only with a non-empty value; any other colon
    (`Re:Zero`, full-width `：` in CJK titles, `Title: Sub`) stays plain text.
    """
    terms: list[tuple[str, str] | str] = []
    for match in TOKEN.finditer(normalized(query)):
        namespace, value, quoted = match[1], match[2], True
        if namespace is None:
            word = match[3] if match[3] is not None else match[4]
            namespace, separator, value = word.partition(":")
            quoted = False
            if not separator or match[3] is not None:
                namespace = None
        if namespace is not None and value.strip() and (quoted or namespace in TAG_NAMESPACES):
            terms.append((namespace, value.strip()))
        else:
            text = (match[0].strip('"') if match[3] is None else match[3]).strip()
            if text:
                terms.append(text)
    return list(dict.fromkeys(terms))


def rule_conditions(query: str, options: SearchOptions) -> tuple[list[str], list[str | int | float]]:
    if len(query) > 1024:
        raise InvalidInput("Search bounds exceeded")
    terms = search_terms(query)
    if len(terms) > 32:
        raise InvalidInput("Too many search terms")
    conditions = []
    values: list[str | int | float] = []
    for term in terms:
        if isinstance(term, tuple):
            conditions.append("b.id IN (SELECT book_id FROM book_tag WHERE namespace=? AND value=?)")
            values.extend(term)
            continue
        conditions.append(
            "(instr(b.search_text,?)>0 OR b.id IN (SELECT t.book_id FROM effective_tag_alias a "
            "JOIN book_tag t ON t.namespace=a.namespace AND t.value=a.value "
            "WHERE instr(a.search_text,?)>0))"
        )
        values.extend([term, term])
    if options.language:
        language = "b.language_key"
        aliases = {
            "english": ("english", "en", "eng"),
            "japanese": ("japanese", "ja", "jp", "日语", "日本語"),
            "chinese": ("chinese", "zh", "cn", "中文", "汉语"),
        }
        wanted = normalized(options.language)
        if wanted == "unknown":
            conditions.append(f"{language} IN ('','unknown')")
        elif wanted == "other":
            known = ("", "unknown", *(v for group in aliases.values() for v in group))
            conditions.append(f"{language} NOT IN ({','.join('?' for _ in known)})")
            values.extend(known)
        else:
            group = aliases.get(wanted, (wanted,))
            conditions.append(f"{language} IN ({','.join('?' for _ in group)})")
            values.extend(group)
    if options.work_type:
        conditions.append("b.work_type_key=?")
        values.append(normalized(options.work_type))
    if options.source:
        if options.source == "none":
            conditions.append("json_extract(b.metadata_json,'$.source') IS NULL")
        elif options.source == "eh":
            conditions.append("json_extract(b.metadata_json,'$.source.site') IN ('ehentai','exhentai')")
        else:
            conditions.append("json_extract(b.metadata_json,'$.source.site')=?")
            values.append(options.source)
    if options.reading != "all":
        conditions.append("json_extract(p.state_json,'$.completed')=?")
        values.append(int(options.reading == "completed"))
    if options.reading == "in_progress":
        conditions.append("json_extract(p.state_json,'$.resume_page') IS NOT NULL")
    if options.reading == "unread":
        conditions.append("json_extract(p.state_json,'$.resume_page') IS NULL")
    if options.review != "all":
        conditions.append("b.review_state=?")
        values.append(options.review)
    if options.rating != "all":
        conditions.append(
            "json_extract(p.state_json,'$.rating') IS "
            + ("NULL" if options.rating == "unrated" else "NOT NULL")
        )
    if options.min_rating is not None:
        conditions.append("json_extract(p.state_json,'$.rating')>=?")
        values.append(options.min_rating)
    if options.source_rating_min is not None:
        conditions.append("json_extract(b.metadata_json,'$.source_rating.average')>=?")
        values.append(options.source_rating_min)
    return conditions, values


def search_count(db: sqlite3.Connection, query: str, options: SearchOptions) -> int:
    conditions, values = rule_conditions(query, options)
    where = " AND ".join(conditions) or "1"
    return db.execute(
        f"SELECT count(*) FROM book b JOIN personal p ON p.book_id=b.id WHERE {where}",
        values,
    ).fetchone()[0]


def tag_suggestions(db: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    if len(query) > 512 or not 1 <= limit <= 20:
        raise InvalidInput("Invalid suggestion bounds")
    query = normalized(query).replace('"', "").strip()
    if not query:
        return []
    namespace, separator, fragment = query.partition(":")
    needle = fragment if separator else query
    scope = namespace if separator else ""
    # Narrow to matching tags first, then count only those.
    rows = db.execute(
        "WITH matched AS (SELECT DISTINCT namespace,value FROM book_tag "
        "WHERE (?1='' OR namespace=?1) AND (instr(value,?2)>0 OR instr(namespace||':'||value,?3)>0) "
        "UNION SELECT a.namespace,a.value FROM effective_tag_alias a "
        "WHERE (?1='' OR a.namespace=?1) AND instr(a.search_text,?2)>0), "
        "counts AS (SELECT m.namespace,m.value,(SELECT count(*) FROM book_tag t "
        "WHERE t.namespace=m.namespace AND t.value=m.value) AS count FROM matched m) "
        "SELECT c.*,a.translation FROM counts c LEFT JOIN effective_tag_alias a "
        "ON c.namespace=a.namespace AND c.value=a.value WHERE c.count>0 "
        "ORDER BY (c.value=?2 OR a.search_text=?2 OR c.namespace||':'||c.value=?3) DESC, "
        "c.count DESC,c.namespace,c.value LIMIT ?4",
        (scope, needle, query, limit),
    )
    return [dict(row) for row in rows]
