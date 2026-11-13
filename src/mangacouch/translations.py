from __future__ import annotations

import json
import time

import httpx

from .database import Library
from .errors import InvalidInput
from .models import display_label, label_key, normalized
from .source_config import SourceVault

DATABASE_URL = "https://github.com/EhTagTranslation/Database/releases/latest/download/db.text.json"
UNSUPPORTED = "Unsupported EhTagTranslation database"


class Translations:
    def __init__(self, library: Library):
        self.library = library

    def status(self) -> dict:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM translation_update").fetchone()
            return {
                "updated_at": row["updated_at"] if row else None,
                "revision": row["revision"] if row else None,
                "count": db.execute("SELECT count(*) FROM tag_translation").fetchone()[0],
            }

    def fetch(self) -> dict:
        """Download the current EhTagTranslation database."""
        with httpx.stream(
            "GET",
            DATABASE_URL,
            follow_redirects=True,
            timeout=30,
            trust_env=False,
            proxy=SourceVault(self.library).load()[1].get("proxy") or None,
        ) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > 16 * 1024**2:
                    raise InvalidInput("Translation database is too large", code="translation_unsupported")
        try:
            data = json.loads(content)
        except ValueError:
            raise InvalidInput(UNSUPPORTED, code="translation_unsupported") from None
        if not isinstance(data, dict):
            raise InvalidInput(UNSUPPORTED, code="translation_unsupported")
        return data

    def import_data(self, data) -> None:
        try:
            rows = _rows(data)
            revision = str(data.get("head", {}).get("sha", ""))[:128]
        except (KeyError, TypeError, AttributeError):
            raise InvalidInput(UNSUPPORTED, code="translation_unsupported") from None
        with self.library.transaction() as db:
            db.execute("DELETE FROM tag_translation")
            db.executemany("INSERT INTO tag_translation VALUES(?,?,?,?)", rows)
            db.execute("UPDATE search_revision SET token=lower(hex(randomblob(16))) WHERE singleton=1")
            db.execute(
                "INSERT INTO translation_update VALUES(1,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "updated_at=excluded.updated_at,revision=excluded.revision",
                (int(time.time()), revision),
            )


def _rows(data) -> list[tuple[str, str, str, str]]:
    if data.get("version") != 7 or not isinstance(data.get("data"), list):
        raise InvalidInput(UNSUPPORTED, code="translation_unsupported")
    rows = []
    for section in data["data"]:
        namespace = section["namespace"]
        if namespace == "rows":
            continue
        if not isinstance(namespace, str) or len(namespace) > 128:
            raise InvalidInput("Invalid translation namespace", code="translation_unsupported")
        for key, value in section["data"].items():
            label = value["name"]
            if not isinstance(label, str) or not 0 < len(label) <= 512 or not 0 < len(key) <= 512:
                raise InvalidInput("Invalid translation", code="translation_unsupported")
            label = display_label(label)
            if label:  # a name that was only an icon has nothing to show
                rows.append((normalized(namespace), normalized(key), label, label_key(label)))
    if not rows:
        raise InvalidInput("Empty translation database", code="translation_unsupported")
    return rows
