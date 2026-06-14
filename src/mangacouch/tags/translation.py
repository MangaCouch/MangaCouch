"""EhTagTranslation — raw English tags → localised names (Appendix A).

``db.full.json`` (regenerated multiple times daily, ~4 MB) is pulled from the stable
``releases/latest/download/`` URL. Shape::

    { "data": [ { "namespace": "female",
                  "data": { "lolicon": { "name": "萝莉", "intro": "…", "links": "" } } } ] }

To translate ``female:lolicon``: find the ``data[]`` entry with ``namespace == "female"``, then
``.data["lolicon"].name``. We store raw tags and translate at display time. License CC BY-NC-SA 3.0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db.models import TagTranslation

if TYPE_CHECKING:
    import httpx

TAGDB_URL = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.full.json"

# e-hentai single-letter namespace prefixes → full namespace (used when parsing raw "f:tag" tags).
_NS_ALIASES = {
    "a": "artist",
    "c": "character",
    "g": "group",
    "l": "language",
    "p": "parody",
    "f": "female",
    "m": "male",
    "o": "other",
    "r": "reclass",
    "x": "mixed",
    "cos": "cosplayer",
}


def parse_tag(raw: str) -> tuple[str, str]:
    """Split a raw tag into ``(namespace, value)``; bare tags get an empty namespace."""
    raw = raw.strip()
    if ":" in raw:
        ns, _, value = raw.partition(":")
        ns = ns.strip().lower()
        return _NS_ALIASES.get(ns, ns), value.strip()
    return "", raw


async def fetch_tagdb(client: httpx.AsyncClient, url: str = TAGDB_URL) -> dict:
    resp = await client.get(url, follow_redirects=True, timeout=60.0)
    resp.raise_for_status()
    return resp.json()


def load_tagdb_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ingest_tagdb(session: Session, raw: dict) -> int:
    """Replace the ``tag_translation`` table from a parsed ``db.full.json``. Returns row count."""
    session.execute(delete(TagTranslation))
    count = 0
    for group in raw.get("data", []):
        namespace = group.get("namespace", "")
        entries = group.get("data", {})
        if not isinstance(entries, dict):
            continue
        for rawname, payload in entries.items():
            name = (payload or {}).get("name") or rawname
            intro = (payload or {}).get("intro") or ""
            session.add(
                TagTranslation(
                    namespace=namespace,
                    raw=rawname,
                    translated=name,
                    intro=intro[:2000],
                )
            )
            count += 1
    session.flush()
    return count


class TagTranslator:
    """In-memory ``(namespace, raw) → translated`` map, loaded from the DB and refreshed on demand."""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}

    def load(self, session: Session) -> int:
        rows = session.execute(
            select(TagTranslation.namespace, TagTranslation.raw, TagTranslation.translated)
        ).all()
        self._map = {(ns, raw): translated for ns, raw, translated in rows}
        return len(self._map)

    def load_safe(self) -> int:
        """Load from a fresh session, swallowing errors (e.g. before the table is populated)."""
        from ..db.base import session_scope

        try:
            with session_scope() as session:
                return self.load(session)
        except Exception:  # noqa: BLE001
            return 0

    def translate(self, namespace: str, value: str) -> str | None:
        return self._map.get((namespace, value)) or self._map.get(("", value))

    def display(self, namespace: str, value: str) -> str:
        """Localised value if known, else the raw value."""
        return self.translate(namespace, value) or value

    @property
    def size(self) -> int:
        return len(self._map)
