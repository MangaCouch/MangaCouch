"""The FTS5 **trigram** index (``cache/search.sqlite``) — CJK-capable substring matching (R6).

The trigram tokenizer ships in CPython 3.14's bundled SQLite on all three platforms with no
extension loading. The index is rebuildable from the authoritative DB, so it lives in ``cache/``.
A ``LIKE`` fallback covers 1–2 character queries (trigram needs ≥3 chars) and wildcard/exact terms.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable
from pathlib import Path

from .query import MatchType, TextTerm

_MIN_TRIGRAM = 3

_SCHEMA = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5("
    "archive_id UNINDEXED, title, tags_text, tokenize='trigram')"
)


def build_tags_text(tags: Iterable[str], title: str = "") -> str:
    """Space-padded token blob holding both ``ns:value`` and bare ``value`` tokens.

    Padding with leading/trailing spaces lets exact (token-boundary) matches use ``LIKE '% tok %'``.
    """
    tokens: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        tokens.append(tag)
        if ":" in tag:
            tokens.append(tag.split(":", 1)[1].strip())
    return " " + " ".join(t for t in tokens if t) + " "


def _phrase(value: str) -> str:
    """A safe FTS5 phrase (double-quote wrapped, internal quotes doubled)."""
    return '"' + value.replace('"', '""') + '"'


def _wildcard_to_like(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch in ("*", "%"):
            out.append("%")
        elif ch in ("?", "_"):
            out.append("_")
        elif ch == "\\":
            out.append("\\\\")
        else:
            out.append(ch)
    return "".join(out)


def _like_escape(value: str) -> str:
    """Escape LIKE metacharacters in a *literal* value (paired with ``ESCAPE '\\'``)."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SearchIndex:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=TRUNCATE")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=8000")
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(self, archive_id: str, title: str, tags: Iterable[str]) -> None:
        tags_text = build_tags_text(tags, title)
        with self._lock:
            self._conn.execute("DELETE FROM archive_fts WHERE archive_id=?", (archive_id,))
            self._conn.execute(
                "INSERT INTO archive_fts (archive_id, title, tags_text) VALUES (?,?,?)",
                (archive_id, title or "", tags_text),
            )
            self._conn.commit()

    def delete(self, archive_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM archive_fts WHERE archive_id=?", (archive_id,))
            self._conn.commit()

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM archive_fts")
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT count(*) FROM archive_fts").fetchone()
        return int(row[0]) if row else 0

    def rebuild(self, rows: Iterable[tuple[str, str, list[str]]]) -> int:
        count = 0
        with self._lock:
            self._conn.execute("DELETE FROM archive_fts")
            for archive_id, title, tags in rows:
                self._conn.execute(
                    "INSERT INTO archive_fts (archive_id, title, tags_text) VALUES (?,?,?)",
                    (archive_id, title or "", build_tags_text(tags, title or "")),
                )
                count += 1
            self._conn.commit()
        return count

    def match_ids(self, term: TextTerm) -> set[str]:
        """Return the archive ids matching one positive text term (caller handles negation)."""
        value = term.value
        ns = term.namespace
        with self._lock:
            if term.match is MatchType.WILDCARD:
                pat = _wildcard_to_like(value)
                if ns:
                    sql = (
                        "SELECT archive_id FROM archive_fts "
                        "WHERE tags_text LIKE ? ESCAPE '\\'"
                    )
                    params: tuple = (f"%{_like_escape(ns)}:{pat}%",)
                else:
                    sql = (
                        "SELECT archive_id FROM archive_fts "
                        "WHERE title LIKE ? ESCAPE '\\' OR tags_text LIKE ? ESCAPE '\\'"
                    )
                    params = (f"%{pat}%", f"%{pat}%")
                return {r[0] for r in self._conn.execute(sql, params)}

            if term.match is MatchType.EXACT:
                # Literal values: escape %/_ so "100%" doesn't turn into a wildcard.
                if ns:
                    sql = (
                        "SELECT archive_id FROM archive_fts "
                        "WHERE tags_text LIKE ? ESCAPE '\\'"
                    )
                    params = (f"% {_like_escape(ns)}:{_like_escape(value)} %",)
                else:
                    sql = (
                        "SELECT archive_id FROM archive_fts "
                        "WHERE tags_text LIKE ? ESCAPE '\\' OR lower(title)=lower(?)"
                    )
                    params = (f"% {_like_escape(value)} %", value)
                return {r[0] for r in self._conn.execute(sql, params)}

            # SUBSTRING
            search_str = f"{ns}:{value}" if ns else value
            if len(search_str) >= _MIN_TRIGRAM:
                col = "tags_text" if ns else "{title tags_text}"
                match_expr = f"{col} : {_phrase(search_str)}"
                sql = "SELECT archive_id FROM archive_fts WHERE archive_fts MATCH ?"
                try:
                    return {r[0] for r in self._conn.execute(sql, (match_expr,))}
                except sqlite3.OperationalError:
                    pass  # fall through to LIKE on any malformed FTS expression
            # LIKE fallback for 1–2 char queries (trigram needs ≥3 chars).
            if ns:
                sql = "SELECT archive_id FROM archive_fts WHERE tags_text LIKE ? ESCAPE '\\'"
                params = (f"%{_like_escape(ns)}:{_like_escape(value)}%",)
            else:
                sql = (
                    "SELECT archive_id FROM archive_fts "
                    "WHERE title LIKE ? ESCAPE '\\' OR tags_text LIKE ? ESCAPE '\\'"
                )
                params = (f"%{_like_escape(value)}%", f"%{_like_escape(value)}%")
            return {r[0] for r in self._conn.execute(sql, params)}
