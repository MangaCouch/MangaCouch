from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import (
    content,
    register_book,
)

from mangacouch.database import Library
from mangacouch.eze import MAX_EZE_BYTES, import_eze

FIXTURE = Path(__file__).parent / "fixtures" / "eze.json"


def test_eze_whitelist_conflicts_and_source_values_survive_restart(library: Library):
    raw = FIXTURE.read_bytes()
    result = import_eze(raw)
    assert result.conflicts == ("title",)
    assert result.metadata.title == "Test translation"
    assert result.metadata.original_title == "試験作品"
    assert result.metadata.uploaded_at == "2026-01-01T00:00:00Z"
    assert result.metadata.declared_page_count == 30
    assert result.metadata.source_rating and result.metadata.source_rating.average == 4.25
    assert result.metadata.parent and result.metadata.parent.gallery_id == 122
    assert result.metadata.newer_versions[0].gallery_id == 124
    assert result.metadata.source and result.metadata.source.url == "https://exhentai.org/g/123/abc123def0/"
    assert len(result.metadata.tags) == 2
    assert "gallery_info.favorite_category" in result.ignored_fields
    assert "gallery_info.source.image_api_key" in result.ignored_fields
    book, _ = register_book(library, content(), source_import=result)
    reopened = Library(library.path)
    with reopened.connection() as db:
        saved = json.loads(
            db.execute("SELECT report_json FROM metadata_import WHERE book_id=?", (book.id,)).fetchone()[0]
        )
        dump = "\n".join(db.iterdump())
    assert "conflicts" not in saved
    assert saved["candidates"]["eze:full"]["title"] == "Different source title"
    assert "DO_NOT_PERSIST" not in dump
    assert "DO_NOT_PERSIST" not in reopened.native_sidecar(book.id).canonical_bytes().decode()
    assert reopened.get_book(book.id).review_state == "needs_review"
    assert reopened.get_book(book.id).personal.rating is None
    assert FIXTURE.read_bytes() == raw


def test_eze_invalid_fields_are_reported_without_values_and_no_timezone_is_invented():
    raw = json.loads(FIXTURE.read_bytes())
    del raw["gallery_info_full"]["date_uploaded"]
    raw["gallery_info"]["translated"] = "DO_NOT_PERSIST_INVALID"
    raw["gallery_info_full"]["rating"]["average"] = "DO_NOT_PERSIST_INVALID"
    result = import_eze(json.dumps(raw).encode())
    assert result.metadata.uploaded_at is None
    assert "gallery_info.upload_date" not in result.ignored_fields
    assert len(result.warnings) == 2
    assert "DO_NOT_PERSIST" not in repr(result)
    raw["gallery_info_full"]["date_uploaded"] = 1767225600
    result = import_eze(json.dumps(raw).encode())
    assert any("date_uploaded" in warning for warning in result.warnings)


def test_eze_source_conflict_does_not_fill_fields_from_another_gallery():
    raw = json.loads(FIXTURE.read_bytes())
    raw["gallery_info_full"]["gallery"]["gid"] = 999
    raw["gallery_info_full"]["parent"]["archiver_key"] = "DO_NOT_PERSIST_NESTED"
    result = import_eze(json.dumps(raw).encode())
    assert "source" in result.conflicts
    assert result.metadata.uploader is None
    assert result.metadata.source_rating is None
    assert "uploader" in result.conflicts
    assert "gallery_info_full.parent.archiver_key" in result.ignored_fields
    assert "DO_NOT_PERSIST" not in repr(result)


@pytest.mark.parametrize("data", [b"{", b'{"archives": []}', b"[]", b"x" * (MAX_EZE_BYTES + 1)])
def test_eze_rejects_wrong_format_and_excessive_input(data: bytes):
    with pytest.raises(ValueError):
        import_eze(data)
