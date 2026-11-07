from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .database import Book, Library
from .errors import RevisionConflict
from .models import Model

TitlePreference = Literal["title", "original_title"]


class DeviceDefaults(Model):
    reading_mode: Literal["single", "spread", "scroll"] = "single"
    fit: Literal["height", "width"] = "height"
    direction: Literal["ltr", "rtl"] = "ltr"
    cover_solo: bool = True
    autoplay_seconds: Literal[3, 5, 8, 10, 12, 15, 20, 30] = 5
    zoom: Literal[100, 125, 150, 200] = 100


class LibraryPreferences(Model):
    title_preference: TitlePreference = "title"
    device_defaults: DeviceDefaults = Field(default_factory=DeviceDefaults)


class PreferenceSnapshot(Model):
    revision: Annotated[int, Field(ge=0)]
    settings: LibraryPreferences


def _load(db) -> PreferenceSnapshot:
    row = db.execute("SELECT revision,settings_json FROM library_preferences WHERE singleton=1").fetchone()
    return PreferenceSnapshot(
        revision=row[0] if row else 0,
        settings=LibraryPreferences.model_validate_json(row[1]) if row else LibraryPreferences(),
    )


def load_preferences(library: Library) -> PreferenceSnapshot:
    with library.connection() as db:
        return _load(db)


def save_preferences(
    library: Library, settings: LibraryPreferences, *, expected_revision: int
) -> PreferenceSnapshot:
    with library.transaction() as db:
        current = _load(db)
        if current.revision != expected_revision:
            raise RevisionConflict("Preferences changed; reload before saving")
        if settings != current.settings:
            db.execute(
                "INSERT INTO library_preferences(singleton,settings_json,revision) VALUES(1,?,?) "
                "ON CONFLICT(singleton) DO UPDATE SET "
                "settings_json=excluded.settings_json,revision=excluded.revision",
                (settings.model_dump_json(), current.revision + 1),
            )
        return _load(db)


def preferred_title(book: Book, preference: TitlePreference) -> str:
    """Display title honouring the library's title preference, with fallbacks."""
    metadata = book.metadata
    return (
        getattr(metadata, preference)
        or metadata.title
        or metadata.original_title
        or book.content.original_filename
    )
