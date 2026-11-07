from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from helpers import make_zip, native_file, pending_sidecars

from mangacouch import ingest
from mangacouch.database import Library
from mangacouch.editions import Editions
from mangacouch.files import LibraryRoot
from mangacouch.ingest import FileLibrary
from mangacouch.models import (
    MAX_METADATA_BYTES,
    Metadata,
    PersonalState,
    Source,
    read_native,
)
from mangacouch.review import Review
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import create_snapshot, restore_snapshot


def test_copy_import_recovers_native_metadata_but_not_identity_or_review_state(
    files: FileLibrary, tmp_path: Path
):
    archive = make_zip(tmp_path / "携带信息.zip")
    path, native = native_file(archive)
    before = path.read_bytes(), archive.read_bytes()
    imported = files.import_file(archive)
    book = imported.book
    assert book.metadata == native.metadata and book.metadata_revision == 1
    assert book.personal == PersonalState() and book.review_state == "needs_review"
    assert files.library.search("本地") == [book.id]
    assert before == (path.read_bytes(), archive.read_bytes())
    sync = SidecarSync(files)
    assert not sync.path(book).exists()
    assert sync.sync_one(book.id)["state"] == "synced"
    assert sync.path(book).exists() and path.read_bytes() == before[0]
    preview = Review(files).preview(book.id)
    assert preview["import_sources"] == ["native"]
    assert "local_confirmation_missing" in preview["automatic"]["reasons"]
    assert preview["issues"] == ["source_unverified"]
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")


def test_index_import_records_baseline_without_rewriting_external_revision(files: FileLibrary):
    archive = make_zip(files.root.path / "原地.cbz")
    path, native = native_file(archive)
    before = path.read_bytes()
    book = files.import_file(archive, index_existing=True).book
    sync = SidecarSync(files)
    assert sync._state(book.id)["baseline_hash"] == native.semantic_hash()
    assert sync._state(book.id)["observed_hash"] == hashlib.sha256(before).hexdigest()
    assert sync.sync_one(book.id) == {"state": "unchanged", "revision": 1}
    assert path.read_bytes() == before and pending_sidecars(files.library) == []
    edited = book.metadata.model_copy(deep=True)
    edited.title = "Edited locally"
    Review(files).edit(book.id, edited, expected_revision=book.metadata_revision)
    assert sync.sync_one(book.id)["state"] == "synced"
    assert json.loads(path.read_bytes())["metadata"]["title"] == "Edited locally"


def test_native_is_initial_metadata_and_eze_is_only_a_partial_candidate(files: FileLibrary, tmp_path: Path):
    archive = make_zip(tmp_path / "both.zip")
    path, native = native_file(archive)
    eze = archive.with_suffix(".eze.json")
    eze.write_text(
        json.dumps({"gallery_info": {"title": "Eze title", "uploader": "Eze uploader", "translated": False}})
    )
    before = path.read_bytes(), eze.read_bytes()
    book = files.import_file(archive).book
    assert book.metadata == native.metadata
    service = Review(files)
    preview = service.preview(book.id)
    assert set(preview["candidates"]["eze:info"]["fields"]) == {"title", "uploader", "translated"}
    assert preview["candidates"]["eze:full"]["fields"] == []
    assert "candidates_unresolved" in preview["issues"]
    assert "language" not in preview["candidates"]["eze:info"]["fields"]
    chosen = service.candidate(
        book.id,
        "eze:info",
        preview["token"],
        {"title": "current", "uploader": "candidate", "translated": "current"},
    )
    assert chosen.metadata.title == native.metadata.title and chosen.metadata.uploader == "Eze uploader"
    assert chosen.metadata.translated and chosen.metadata.language == "中文"
    assert "uploader" in chosen.metadata.protected_fields
    assert before == (path.read_bytes(), eze.read_bytes())


def test_matching_sparse_eze_does_not_offer_to_clear_native_fields(files: FileLibrary, tmp_path: Path):
    archive = make_zip(tmp_path / "sparse.zip")
    _, native = native_file(archive)
    archive.with_suffix(".eze.json").write_text(
        json.dumps({"gallery_info": {"title": native.metadata.title}})
    )
    book = files.import_file(archive).book
    preview = Review(files).preview(book.id)
    assert all(not candidate["fields"] for candidate in preview["candidates"].values())
    assert "candidates_unresolved" not in preview["issues"]
    assert book.metadata.protected_fields == native.metadata.protected_fields


@pytest.mark.parametrize("kind", ["wrong_hash", "schema", "unknown_personal", "broken", "oversize"])
def test_invalid_native_is_visible_but_does_not_hide_archive_or_store_rejected_values(
    files: FileLibrary, tmp_path: Path, kind: str
):
    archive = make_zip(tmp_path / "invalid.zip")
    path, native = native_file(archive)
    value = native.model_dump(mode="json", by_alias=True)
    value["metadata"]["title"] = "DO_NOT_STORE_REJECTED"
    if kind == "wrong_hash":
        value["content_sha256"] = "f" * 64
    if kind == "schema":
        value["schema"] = "mangacouch.native/unknown"
    if kind == "unknown_personal":
        value["personal"] = {"rating": 5, "completed": True}
    data = json.dumps(value).encode()
    if kind == "broken":
        data = b'{"title":"DO_NOT_STORE_REJECTED"'
    if kind == "oversize":
        data = b" " * (MAX_METADATA_BYTES + 4097) + data
    path.write_bytes(data)
    result = files.import_file(archive)
    assert result.book.metadata == Metadata() and result.book.review_state == "needs_review"
    assert "native_unreadable_or_invalid" in result.warnings
    preview = Review(files).preview(result.book.id)
    assert preview["warnings"] == ["native_unreadable_or_invalid"]
    assert preview["import_sources"] == []
    with files.library.connection() as db:
        assert "DO_NOT_STORE_REJECTED" not in "\n".join(db.iterdump())
    assert path.read_bytes() == data
    assert files.page_bytes(result.book.id, 0).startswith(b"\x89PNG")


def test_bad_native_falls_back_to_valid_eze_with_warning(files: FileLibrary, tmp_path: Path):
    archive = make_zip(tmp_path / "fallback.zip")
    path, _ = native_file(archive)
    path.write_text("broken")
    archive.with_suffix(".eze.json").write_text(
        json.dumps({"gallery_info": {"title": "Eze fallback", "language": "English"}})
    )
    result = files.import_file(archive)
    assert result.book.metadata.title == "Eze fallback"
    assert Review(files).preview(result.book.id)["warnings"] == ["native_unreadable_or_invalid"]
    assert path.read_text() == "broken"


def test_symlink_native_is_not_followed(files: FileLibrary, tmp_path: Path):
    archive = make_zip(tmp_path / "symlink.zip")
    path, _ = native_file(archive)
    target = tmp_path / "other.json"
    path.rename(target)
    path.symlink_to(target)
    result = files.import_file(archive)
    assert result.book.metadata == Metadata()
    assert result.warnings == ("native_unreadable_or_invalid",)
    assert path.is_symlink()


def test_native_changed_while_reading_is_not_applied(
    files: FileLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    archive = make_zip(tmp_path / "racing.zip")
    path, _ = native_file(archive)
    original = ingest.file_stamp
    reads = 0

    def changed(target: Path):
        nonlocal reads
        stamp = original(target)
        if target == path:
            reads += 1
            if reads > 1:
                return (*stamp[:3], stamp[3] + 1)
        return stamp

    monkeypatch.setattr(ingest, "file_stamp", changed)
    result = files.import_file(archive)
    assert result.book.metadata == Metadata()
    assert result.warnings == ("native_changed_during_read",)


def test_duplicate_import_never_replaces_unsynced_metadata_personal_or_proof(
    files: FileLibrary, tmp_path: Path
):
    archive = make_zip(tmp_path / "duplicate.zip")
    path, native = native_file(archive)
    book = files.import_file(archive).book
    service = Review(files)
    edited = native.metadata.model_copy(update={"title": "User edit"})
    book = service.edit(book.id, edited, expected_revision=book.metadata_revision)
    preview = service.preview(book.id)
    service.complete(book.id, preview["token"], preview["issues"])
    state = PersonalState(rating=4.5, completed=True, bookmarks=[1])
    files.library.save_personal(book.id, state, expected_revision=1)
    token = service.preview(book.id)["token"]
    native.metadata.title = "External replacement"
    path.write_bytes(native.canonical_bytes())
    duplicate = files.import_file(archive)
    assert not duplicate.created and duplicate.book.metadata.title == "User edit"
    assert duplicate.book.personal == state and duplicate.book.review_state == "confirmed"
    assert service.preview(book.id)["token"] == token
    assert "existing_book_metadata_preserved" in duplicate.warnings
    assert pending_sidecars(files.library) == [(book.id, book.metadata_revision)]


def test_ready_job_recovery_keeps_staged_metadata_and_detects_later_sidecar_changes(files: FileLibrary):
    archive = make_zip(files.root.path / "recover.cbz")
    path, native = native_file(archive)
    original_title = native.metadata.title
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_manifest BEFORE INSERT ON archive_manifest "
            "BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        files.import_file(archive, index_existing=True)
    assert files.library.search("") == []
    with files.library.transaction() as db:
        db.execute("DROP TRIGGER fail_manifest")
    native.metadata.title = "Changed after ready checkpoint"
    path.write_bytes(native.canonical_bytes())
    assert set(files.recover().values()) == {"recovered"}
    book = files.library.get_book(files.library.search("")[0])
    assert book.metadata.title == original_title
    assert SidecarSync(files).sync_one(book.id)["state"] == "conflict"
    assert json.loads(path.read_bytes())["metadata"]["title"] == native.metadata.title


def test_scanning_initial_native_then_snapshot_restores_baseline(files: FileLibrary, tmp_path: Path):
    (files.root.path / "import").mkdir()
    archive = make_zip(files.root.path / "import" / "scanned.cbz")
    path, native = native_file(archive)
    scanner = Scanner(files)
    identifier = scanner.start()["id"]
    while scanner.get(identifier)["state"] == "active":
        scanner.step(identifier, limit=10)
    book = files.library.get_book(files.library.search("")[0])
    assert book.metadata == native.metadata
    snapshot = create_snapshot(files, tmp_path / "native.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    service = FileLibrary(restored.library, files.root.path)
    assert SidecarSync(service).sync_one(book.id)["state"] == "unchanged"
    saved = read_native(SidecarSync(files).path(book).read_bytes(), book.content.sha256)
    assert saved.metadata == native.metadata


def test_portable_native_does_not_transfer_library_confirmation_or_personal_data(
    files: FileLibrary, tmp_path: Path
):
    archive = make_zip(tmp_path / "portable.cbz")
    native_file(archive)
    book = files.import_file(archive).book
    review = Review(files)
    preview = review.preview(book.id)
    review.complete(book.id, preview["token"], preview["issues"])
    files.library.save_personal(book.id, PersonalState(rating=5, completed=True), expected_revision=1)
    SidecarSync(files).sync_one(book.id)
    other_root = tmp_path / "other-root"
    other_root.mkdir()
    other = Library.initialize(tmp_path / "other.sqlite")
    LibraryRoot.bind(other, other_root)
    second_files = FileLibrary(other, other_root)
    second = second_files.import_file(files.root.resolve(book.content.relative_path)).book
    assert second.id != book.id and second.metadata == book.metadata
    assert second.personal == PersonalState() and second.review_state == "needs_review"
    assert "local_confirmation_missing" in Review(second_files).preview(second.id)["automatic"]["reasons"]


def test_edition_candidates_follow_parent_and_reverse_newer_relations(files, tmp_path):
    old_source = Source(site="exhentai", gallery_id=10, gallery_token="abc")
    new_source = Source(site="ehentai", gallery_id=11, gallery_token="abc")
    old_path = make_zip(tmp_path / "old.zip", ["1.png"])
    native_file(old_path, Metadata(title="Old", source=old_source, newer_versions=[new_source]))
    old = files.import_file(old_path).book
    new_path = make_zip(tmp_path / "new.zip", ["1.png", "2.png"])
    native_file(new_path, Metadata(title="New", source=new_source))
    new = files.import_file(new_path).book
    editions = Editions(files.library)
    assert editions.candidates(new.id)["items"] == [{"id": old.id, "title": "Old"}]
    assert editions.candidates(old.id)["items"] == []
    edit = new.metadata.model_copy(update={"parent": old_source.model_copy(update={"site": "ehentai"})})
    Review(files).edit(new.id, edit, expected_revision=new.metadata_revision)
    assert [item["id"] for item in editions.candidates(new.id)["items"]] == [old.id]
    assert editions.candidates(new.id, after=old.id)["items"] == []
    assert files.library.get_book(old.id).personal == old.personal
