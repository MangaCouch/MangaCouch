from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest
from helpers import content, pending_sidecars, register_book, save_metadata
from pydantic import ValidationError

from mangacouch.database import Library, RevisionConflict
from mangacouch.eze import MAX_EZE_BYTES, import_eze
from mangacouch.models import Content, Metadata, NativeSidecar, PersonalState, read_native

FIXTURE = Path(__file__).parent / "fixtures" / "eze.json"


def test_explicit_initialization_does_not_create_or_overwrite_existing_database(tmp_path: Path):
    path = tmp_path / "library.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        Library(path)
    assert not path.exists()
    Library.initialize(path)
    with pytest.raises(FileExistsError):
        Library.initialize(path)
    legacy = tmp_path / "legacy.sqlite"
    with closing(sqlite3.connect(legacy)) as db:
        db.execute("CREATE TABLE archive(id TEXT)")
        db.commit()
    before = legacy.read_bytes()
    with pytest.raises(ValueError, match="supported MangaCouch database"):
        Library(legacy)
    assert legacy.read_bytes() == before


def test_hash_deduplication_is_independent_of_filename_and_personal_state(library: Library):
    first, created = register_book(library, content())
    assert created and UUID(first.id).version == 4 and first.id != first.content.sha256
    first = library.save_personal(
        first.id, PersonalState(rating=4.5, resume_page=20, completed=True), expected_revision=1
    )
    duplicate, created = register_book(library, content(filename="different.cbz"))
    assert not created and duplicate.id == first.id and duplicate.personal == first.personal
    replacement, created = register_book(library, content("new version"))
    assert created and replacement.id != first.id and replacement.personal == PersonalState()
    assert library.get_book(first.id).personal.rating == 4.5


def test_concurrent_registration_has_one_record_and_separate_contexts_do_not_mix(tmp_path: Path):
    first = Library.initialize(tmp_path / "first.sqlite")
    other = Library.initialize(tmp_path / "other.sqlite")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(lambda i: register_book(Library(first.path), content(filename=f"{i}.zip")), range(10))
        )
    assert sum(created for _, created in results) == 1
    assert len({book.id for book, _ in results}) == 1
    assert other.search("") == []
    assert len(first.search("")) == 1


def test_edit_restart_and_search_share_committed_state(library: Library):
    book, _ = register_book(library, content())
    book = save_metadata(
        library, book.id, Metadata(title="ＡＢＣ 中文 心海", language="Chinese"), expected_revision=1
    )
    book = save_metadata(
        library, book.id, Metadata(title="新版 心海", language="Chinese"), expected_revision=2
    )
    reopened = Library(library.path)
    assert reopened.get_book(book.id).metadata.title == "新版 心海"
    assert pending_sidecars(reopened) == [(book.id, 3)]
    assert reopened.search("新版 心海") == [book.id]
    assert reopened.search("abc") == []
    with pytest.raises(RevisionConflict):
        save_metadata(reopened, book.id, Metadata(title="stale"), expected_revision=2)
    same = save_metadata(reopened, book.id, book.metadata, expected_revision=3)
    assert same.metadata_revision == 3 and pending_sidecars(reopened) == [(book.id, 3)]


def test_sidecar_queue_failure_rolls_back_metadata_and_search(library: Library):
    book, _ = register_book(library, content(), Metadata(title="before"))
    with library.connection() as db:
        db.execute(
            "CREATE TRIGGER inject_failure BEFORE UPDATE ON sidecar_sync "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        save_metadata(library, book.id, Metadata(title="after"), expected_revision=1)
    reopened = Library(library.path)
    assert reopened.get_book(book.id).metadata.title == "before"
    assert reopened.search("after") == [] and reopened.search("before") == [book.id]
    assert pending_sidecars(reopened) == [(book.id, 1)]


def test_personal_state_is_separate_and_revision_checked(library: Library):
    book, _ = register_book(library, content())
    before = library.native_sidecar(book.id).canonical_bytes()
    state = PersonalState(rating=1.5, resume_page=29, completed=True, bookmarks=[2, 8])
    library.save_personal(book.id, state, expected_revision=1)
    state.resume_page = 3
    library.save_personal(book.id, state, expected_revision=2)
    with pytest.raises(RevisionConflict):
        library.save_personal(book.id, PersonalState(), expected_revision=1)
    assert library.native_sidecar(book.id).canonical_bytes() == before
    assert Library(library.path).get_book(book.id).personal.completed
    with pytest.raises(ValueError, match="outside this book"):
        library.save_personal(book.id, PersonalState(bookmarks=[30]), expected_revision=3)


@pytest.mark.parametrize("rating", [-0.5, 1.25, 5.5, True, "4", float("nan")])
def test_invalid_personal_ratings_are_rejected(rating):
    with pytest.raises(ValidationError):
        PersonalState(rating=rating)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/book.zip",
        "../book.zip",
        "C:/book.zip",
        "C:book.zip",
        "folder/../book.zip",
        "folder\\book.zip",
        "./book.zip",
        "book\x00.zip",
    ],
)
def test_relative_paths_cannot_escape_the_library(path: str):
    data = content().model_dump()
    data["relative_path"] = path
    with pytest.raises(ValidationError):
        Content.model_validate(data)


def test_managed_name_does_not_depend_on_the_original_name():
    item = content()
    item.original_filename = "CON/原名 😀:<>?\x00.zip" + "长" * 300
    assert item.managed_path == f"{item.sha256[:2]}/{item.sha256}.cbz"
    assert "😀" in item.original_filename
    assert Content.model_validate_json(item.model_dump_json()).original_filename == item.original_filename


def test_native_has_bound_content_and_semantic_hash_without_private_data(library: Library):
    book, _ = register_book(
        library,
        content(),
        Metadata(title="书", protected_fields=["title"], field_sources={"title": "manual"}),
    )
    native = library.native_sidecar(book.id)
    data = native.canonical_bytes()
    reserialized = json.dumps(json.loads(data), ensure_ascii=False, indent=4).encode()
    assert read_native(reserialized, book.content.sha256).semantic_hash() == native.semantic_hash()
    assert book.id.encode() not in data
    assert read_native(data, book.content.sha256).metadata.protected_fields == ["title"]
    with pytest.raises(ValueError, match="different file content"):
        read_native(data, "f" * 64)
    for field in ["user_rating", "progress", "favorites", "extensions", "archive_id"]:
        invalid = json.loads(data)
        invalid[field] = "forbidden"
        with pytest.raises(ValidationError):
            read_native(json.dumps(invalid).encode(), book.content.sha256)
    invalid = json.loads(data)
    invalid["schema"] = "mangacouch.sidecar/3"
    with pytest.raises(ValidationError):
        read_native(json.dumps(invalid).encode(), book.content.sha256)
    del invalid["schema"]
    with pytest.raises(ValidationError):
        read_native(json.dumps(invalid).encode(), book.content.sha256)


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
    assert saved["conflicts"] == ["title"]
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


def test_cjk_and_unicode_search_includes_original_names_without_requiring_metadata(library: Library):
    first, _ = register_book(library, content("one", "[C89] 心海.zip"))
    second, _ = register_book(
        library, content("two"), Metadata(title="ＡＢＣ 明日方舟 カタカナ 한글 cafe\u0301")
    )
    expectations = {
        "心": [first.id],
        "心海": [first.id],
        "c89 心海": [first.id],
        "abc": [second.id],
        "方舟": [second.id],
        "カ": [second.id],
        "한": [second.id],
        "café": [second.id],
        "不存在": [],
        "%": [],
    }
    for query, expected in expectations.items():
        assert library.search(query) == expected


def test_large_match_set_uses_stable_pagination(library: Library):
    expected = {register_book(library, content(str(i)), Metadata(title="共同"))[0].id for i in range(1001)}
    found: list[str] = []
    while page := library.search("共同", limit=200, after_id=found[-1] if found else ""):
        found.extend(page)
    assert len(found) == len(expected) and set(found) == expected
    assert found == sorted(found)


def test_published_native_sidecar_schema_matches_the_model():
    path = Path(__file__).parents[1] / "schema" / "native-sidecar.schema.json"
    published = json.loads(path.read_text(encoding="utf-8"))
    assert published.pop("$comment")
    assert published == NativeSidecar.model_json_schema(by_alias=True)
