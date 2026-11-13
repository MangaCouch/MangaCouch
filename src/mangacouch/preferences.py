from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field

from .database import Book, Library
from .errors import RevisionConflict
from .models import Metadata, Model

TitlePreference = Literal["title", "original_title"]


class LibraryPreferences(Model):
    """Preferences shared by every device; reader settings stay with each device."""

    title_preference: TitlePreference = "title"


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


def preferred_title(book: Book, preference: TitlePreference = "title") -> str:
    """Display title honouring the library's title preference, with fallbacks."""
    return display_title(book.metadata, book.content.original_filename, preference)


def display_title(metadata: Metadata, filename: str, preference: TitlePreference = "title") -> str:
    """The preferred title of `metadata`, else its other title, else the archive's file name."""
    return getattr(metadata, preference) or metadata.title or metadata.original_title or filename
