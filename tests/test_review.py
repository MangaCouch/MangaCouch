from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    confirmed,
    hash_outside_operation_lock,
    import_book,
    login,
    make_zip,
    pending_sidecars,
    save_metadata,
)

from mangacouch import ingest
from mangacouch.auth import (
    Auth,
)
from mangacouch.database import RevisionConflict
from mangacouch.files import ContentChanged
from mangacouch.ingest import FileLibrary, image_bytes
from mangacouch.models import Metadata, PersonalState, Source, canonical_json
from mangacouch.review import EVENT_RETENTION, LOCAL_RULE, Review
from mangacouch.sidecars import SidecarSync


def pending(files: FileLibrary, identifier: str):
    with files.library.transaction() as db:
        db.execute("UPDATE book SET review_state='needs_review' WHERE id=?", (identifier,))


def run(service: Review, identifier: str):
    return service.automatic(identifier, service.preview(identifier)["token"])


def test_manual_confirmation_binds_content_and_all_fields_without_verifying_source(
    files: FileLibrary, tmp_path: Path
):
    service, book = confirmed(files, tmp_path)
    preview = service.preview(book.id)
    proof = preview["confirmation"]
    assert proof["action"] == "manual_confirm"
    assert proof["evidence"]["rule"] == LOCAL_RULE
    assert proof["evidence"]["content_sha256"] == book.content.sha256
    assert proof["evidence"]["page_count"] == book.content.page_count
    assert proof["checked_at"].endswith("+00:00")
    assert proof["acknowledged_issues"] == preview["issues"] == ["source_absent"]
    assert preview["automatic"]["eligible"]


def test_reverting_all_fields_reuses_exact_confirmation_and_keeps_personal_state(
    files: FileLibrary, tmp_path: Path
):
    service, book = confirmed(files, tmp_path)
    files.library.save_personal(book.id, PersonalState(rating=4.5, completed=True), expected_revision=1)
    changed = book.metadata.model_copy(deep=True)
    changed.title = "Changed title"
    edited = service.edit(book.id, changed, expected_revision=book.metadata_revision)
    assert edited.review_state == "needs_review"
    assert "local_confirmation_missing" in service.preview(book.id)["automatic"]["reasons"]
    restored = service.edit(book.id, book.metadata, expected_revision=edited.metadata_revision)
    assert restored.review_state == "confirmed"
    assert restored.personal.completed and restored.personal.rating == 4.5
    assert "title" in restored.metadata.protected_fields
    preview = service.preview(book.id)
    assert preview["confirmation"]["evidence_event_id"] == preview["automatic"]["evidence_event_id"]
    assert preview["confirmation"]["acknowledged_issues"] == ["source_absent"]
    token = preview["token"]
    assert service.automatic(book.id, token)["outcome"] == "already_confirmed"
    assert service.preview(book.id)["token"] == token


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "known title"},
        {"language": "中文"},
        {"uploader": "another"},
        {"declared_page_count": 100},
        {"source": Source(site="exhentai", gallery_id=123, gallery_token="abc")},
    ],
)
def test_any_semantic_field_change_invalidates_existing_proof(
    files: FileLibrary, tmp_path: Path, changes: dict
):
    service, book = confirmed(files, tmp_path)
    metadata = book.metadata.model_copy(update=changes)
    edited = service.edit(book.id, metadata, expected_revision=book.metadata_revision)
    result = run(service, book.id)
    assert edited.review_state == "needs_review" and result["outcome"] == "needs_review"
    assert "local_confirmation_missing" in result["reasons"]
    assert files.library.get_book(book.id).metadata == edited.metadata


@pytest.mark.parametrize(
    "metadata,reason",
    [
        (Metadata(language="English"), "title_missing"),
        (Metadata(title="Known", language="unknown"), "language_unknown"),
        (
            Metadata(title="Known", language="English", declared_page_count=999),
            "declared_page_count_mismatch",
        ),
        (Metadata(title="Known", language="English", declared_byte_size=1), "declared_byte_size_mismatch"),
    ],
)
def test_manual_acceptance_of_gaps_does_not_make_automatic_pass(
    files: FileLibrary, tmp_path: Path, metadata: Metadata, reason: str
):
    service, book = confirmed(files, tmp_path, metadata)
    assert book.review_state == "confirmed"
    pending(files, book.id)
    result = run(service, book.id)
    assert result["outcome"] == "needs_review" and reason in result["reasons"]
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")


def test_external_native_and_legacy_events_do_not_supply_local_proof(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    SidecarSync(files).sync_one(book.id)
    with files.library.transaction() as db:
        db.execute(
            "UPDATE review_event SET details_json=? WHERE action='manual_confirm'",
            (canonical_json({"acknowledged_issues": ["source_absent"]}),),
        )
    pending(files, book.id)
    assert run(service, book.id)["reasons"] == ["local_confirmation_missing"]
    assert SidecarSync(files).path(book).exists()


def test_eze_candidates_and_source_urls_alone_never_auto_confirm(files: FileLibrary, tmp_path: Path):
    identifier = import_book(files, tmp_path, eze=True).id
    service = Review(files)
    result = run(service, identifier)
    assert result["outcome"] == "needs_review"
    assert "local_confirmation_missing" in result["reasons"]
    assert "candidates_unresolved" in result["reasons"]


def test_candidate_decision_is_required_and_recorded_before_reuse(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    candidate = book.metadata.model_copy(update={"title": "Unwanted candidate"})
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO metadata_import(book_id,report_json) VALUES(?,?)",
            (book.id, canonical_json({"candidates": {"test": candidate.model_dump(mode="json")}})),
        )
    pending(files, book.id)
    assert "candidates_unresolved" in run(service, book.id)["reasons"]
    preview = service.preview(book.id)
    restored = service.candidate(book.id, "test", preview["token"], None)
    assert restored.review_state == "confirmed"
    assert service.preview(book.id)["confirmation"]["candidates"] == {"test": "skipped"}


def test_sidecar_conflict_blocks_matching_evidence(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    sync = SidecarSync(files)
    sync.sync_one(book.id)
    native = files.library.native_sidecar(book.id)
    native.metadata.title = "External title"
    sync.path(book).write_bytes(native.canonical_bytes())
    sync.sync_one(book.id)
    pending(files, book.id)
    assert "sidecar_conflicts" in run(service, book.id)["reasons"]


def test_auto_and_manual_confirmation_rehash_when_the_file_stamp_changes(
    files: FileLibrary, tmp_path: Path, monkeypatch
):
    service, book = confirmed(files, tmp_path)
    pending(files, book.id)
    path = files.root.resolve(book.content.relative_path)
    hashed = []
    original = ingest.hash_file
    monkeypatch.setattr(ingest, "hash_file", lambda *a, **kw: hashed.append(a[0]) or original(*a, **kw))
    assert run(service, book.id)["outcome"] == "confirmed"
    assert hashed == [], "an unchanged stamp must not rehash the archive"
    pending(files, book.id)
    data = path.read_bytes()
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    assert run(service, book.id)["reasons"] == ["archive_unavailable"]
    assert hashed
    preview = service.preview(book.id)
    with pytest.raises(ContentChanged):
        service.complete(book.id, preview["token"], preview["issues"])
    assert files.library.get_book(book.id).review_state == "needs_review"


def test_missing_manifest_and_file_prevent_auto_confirmation(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    pending(files, book.id)
    path = files.root.resolve(book.content.relative_path)
    path.unlink()
    assert run(service, book.id)["reasons"] == ["archive_unavailable"]
    with files.library.transaction() as db:
        db.execute("DELETE FROM archive_manifest WHERE book_id=?", (book.id,))
    assert "page_manifest_missing" in run(service, book.id)["reasons"]


def test_auto_preview_is_current_and_bound_to_one_book(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    pending(files, book.id)
    preview = service.preview(book.id)
    second = files.import_file(make_zip(tmp_path / "different.zip", ["different.png"])).book
    with pytest.raises(RevisionConflict):
        service.automatic(second.id, preview["token"])
    run(service, book.id)
    with pytest.raises(RevisionConflict):
        service.automatic(book.id, preview["token"])


def test_auto_event_failure_rolls_back_confirmation_and_metadata(files: FileLibrary, tmp_path: Path):
    service, book = confirmed(files, tmp_path)
    changed = service.edit(
        book.id, Metadata(title="temporary", language="English"), expected_revision=book.metadata_revision
    )
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_auto BEFORE INSERT ON review_event WHEN NEW.action='auto_confirm' "
            "BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        service.edit(book.id, book.metadata, expected_revision=changed.metadata_revision)
    after = files.library.get_book(book.id)
    assert after.metadata == changed.metadata and after.metadata_revision == changed.metadata_revision
    assert after.review_state == "needs_review"


def test_auto_endpoint_requires_management_csrf_and_current_token(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    service, book = confirmed(files, tmp_path)
    pending(files, book.id)
    endpoint = f"/api/books/{book.id}/review/automatic"
    data = {"token": service.preview(book.id)["token"]}
    _, key = auth.create_key("readonly", "readonly fixture")
    assert client.post(endpoint, json=data, headers={"Authorization": f"Bearer {key}"}).status_code == 403
    login(client)
    assert client.post(endpoint, json=data).status_code == 403
    response = client.post(endpoint, json=data, headers=CSRF)
    assert response.status_code == 200 and response.json()["outcome"] == "confirmed"
    assert response.json()["book"]["review_state"] == "confirmed"
    assert client.post(endpoint, json=data, headers=CSRF).status_code == 409
    preview = client.get(f"/api/books/{book.id}/review").json()
    assert preview["confirmation"]["acknowledged_issues"] == ["source_absent"]
    with files.library.connection() as db:
        detail = json.loads(
            db.execute("SELECT details_json FROM review_event WHERE action='auto_confirm'").fetchone()[0]
        )
    assert detail["rule"] == LOCAL_RULE and detail["evidence_event_id"]


def test_manual_edit_protects_fields_and_rejects_forged_provenance(files: FileLibrary, tmp_path: Path):
    identifier = import_book(files, tmp_path).id
    review = Review(files)
    changed = review.edit(
        identifier, Metadata(title="人工标题", field_sources={"title": "native"}), expected_revision=1
    )
    assert changed.metadata.field_sources == {"title": "manual"}
    assert changed.metadata.protected_fields == ["title"]
    assert files.library.search("人工") == [identifier]
    assert pending_sidecars(files.library) == [(identifier, 2)]
    with pytest.raises(RevisionConflict):
        review.edit(identifier, Metadata(title="stale"), expected_revision=1)
    assert files.library.get_book(identifier).metadata.title == "人工标题"


def test_eze_field_choices_and_skip_keep_archive_readable(files: FileLibrary, tmp_path: Path):
    identifier = import_book(files, tmp_path, eze=True).id
    review = Review(files)
    preview = review.preview(identifier)
    assert "candidates_unresolved" in preview["issues"]
    original = files.library.get_book(identifier)
    name = next(name for name in preview["candidates"] if "full" in name)
    candidate = preview["candidates"][name]
    choices = {field: "candidate" if field == "title" else "current" for field in candidate["fields"]}
    selected = review.candidate(identifier, name, preview["token"], choices)
    assert selected.metadata.title == "Different source title"
    assert selected.metadata.uploaded_at == original.metadata.uploaded_at
    assert selected.metadata.source_rating == original.metadata.source_rating
    assert selected.personal == PersonalState() and selected.content == original.content
    assert (
        selected.metadata.field_sources["title"] == "manual" and "title" in selected.metadata.protected_fields
    )
    for name in review.preview(identifier)["candidates"]:
        current = review.preview(identifier)
        if current["candidates"][name]["decision"] is None:
            review.candidate(identifier, name, current["token"], None)
    current = review.preview(identifier)
    assert "candidates_unresolved" not in current["issues"]
    assert "declared_page_count_mismatch" in current["issues"]
    assert review.complete(identifier, current["token"], current["issues"]).review_state == "confirmed"
    assert files.page_bytes(identifier, 0).startswith(b"\x89PNG")


def test_review_requires_current_preview_and_explicit_unknown_acknowledgement(
    files: FileLibrary, tmp_path: Path
):
    identifier = import_book(files, tmp_path).id
    review = Review(files)
    preview = review.preview(identifier)
    assert set(preview["issues"]) == {"title_missing", "language_unknown", "source_absent"}
    with pytest.raises(ValueError, match="acknowledge"):
        review.complete(identifier, preview["token"], [])
    assert review.complete(identifier, preview["token"], preview["issues"]).review_state == "confirmed"
    assert review.preview(identifier)["confirmation"]["acknowledged_issues"] == preview["issues"]
    with pytest.raises(RevisionConflict):
        review.complete(identifier, preview["token"], preview["issues"])
    edited = review.edit(identifier, Metadata(title="new"), expected_revision=1)
    assert edited.review_state == "needs_review"
    assert review.preview(identifier)["confirmation"] is None
    assert files.page_bytes(identifier, 0).startswith(b"\x89PNG")


def test_pending_candidates_and_sidecar_conflicts_cannot_be_acknowledged_away(
    files: FileLibrary, tmp_path: Path
):
    identifier = import_book(files, tmp_path, eze=True).id
    review = Review(files)
    preview = review.preview(identifier)
    with pytest.raises(ValueError, match="pending candidates"):
        review.complete(identifier, preview["token"], preview["issues"])
    name = next(name for name, value in preview["candidates"].items() if value["fields"])
    with pytest.raises(ValueError, match="every differing field"):
        review.candidate(identifier, name, preview["token"], {})
    sync = SidecarSync(files)
    sync.sync_one(identifier)
    native = files.library.native_sidecar(identifier)
    native.metadata.title = "external conflict"
    sync.path(files.library.get_book(identifier)).write_bytes(native.canonical_bytes())
    sync.sync_one(identifier)
    with pytest.raises(RevisionConflict):
        review.candidate(identifier, name, preview["token"], None)
    assert "sidecar_conflicts" in review.preview(identifier)["issues"]


def test_review_event_failure_rolls_back_metadata_and_search(files: FileLibrary, tmp_path: Path):
    identifier = import_book(files, tmp_path).id
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_review BEFORE INSERT ON review_event BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        Review(files).edit(identifier, Metadata(title="must roll back"), expected_revision=1)
    assert files.library.get_book(identifier).metadata == Metadata()
    assert files.library.search("roll") == [] and pending_sidecars(files.library) == [(identifier, 1)]


def test_changed_archive_cannot_be_confirmed(files: FileLibrary, tmp_path: Path):
    identifier = import_book(files, tmp_path).id
    review = Review(files)
    preview = review.preview(identifier)
    files.root.resolve(files.library.get_book(identifier).content.relative_path).write_bytes(b"changed")
    with pytest.raises(ContentChanged):
        review.complete(identifier, preview["token"], preview["issues"])
    assert files.library.get_book(identifier).review_state == "needs_review"


def test_review_preview_is_bound_to_one_book(files: FileLibrary, tmp_path: Path):
    first = import_book(files, tmp_path).id
    second = files.import_file(make_zip(tmp_path / "second.zip", ["different.png"])).book.id
    review = Review(files)
    preview = review.preview(first)
    with pytest.raises(RevisionConflict):
        review.complete(second, preview["token"], preview["issues"])
    assert files.library.get_book(second).review_state == "needs_review"


def test_review_never_nests_write_transactions_after_a_remount(files: FileLibrary, tmp_path, monkeypatch):
    path = make_zip(tmp_path / "eh.zip")
    book = files.import_file(path).book
    source = Source(site="ehentai", gallery_id=1, gallery_token="abc")
    metadata = Metadata(title="t", language="English", source=source, declared_byte_size=1)
    save_metadata(files.library, book.id, metadata, expected_revision=1)
    with files.library.transaction() as db:
        observation = {"content_sha256": book.content.sha256}
        db.execute(
            "INSERT INTO source_observation VALUES(?,?,?)", (book.id, source.key, canonical_json(observation))
        )
        report = {"candidates": {"online:ehentai": metadata.model_dump(mode="json")}}
        db.execute(
            "INSERT INTO metadata_import(book_id,report_json) VALUES(?,?)", (book.id, canonical_json(report))
        )
        # A remount changes st_dev, so the recorded stamp no longer matches.
        db.execute("UPDATE archive_manifest SET file_stamp='[0,0,0,0]'")
    depth = threading.local()
    original = files.library.transaction

    @contextlib.contextmanager
    def tracked():
        assert getattr(depth, "value", 0) == 0, "nested write transaction"
        depth.value = 1
        try:
            with original() as db:
                yield db
        finally:
            depth.value = 0

    monkeypatch.setattr(files.library, "transaction", tracked)
    service = Review(files)
    preview = service.preview(book.id)
    with files.library.connection() as db:
        assert preview["byte_size_basis"] == "images" and preview["compared_byte_size"] == image_bytes(
            db, book.id
        )
    service.edit(book.id, metadata.model_copy(update={"title": "new"}), expected_revision=2)
    with files.library.connection() as db:
        assert db.execute("SELECT file_stamp FROM archive_manifest").fetchone()[0] != "[0,0,0,0]"


def test_routine_review_events_expire_but_decisions_and_the_latest_event_stay(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "events.cbz")).book
    old = int(time.time()) - EVENT_RETENTION - 60
    old_iso = datetime.fromtimestamp(old, UTC).isoformat()
    with files.library.transaction() as db:
        db.execute("DELETE FROM review_event")
        expired = [
            event(db, book.id, "source_refresh", {"fetched_at": old}),
            event(db, book.id, "auto_check", {"checked_at": old_iso}),
        ]
        kept = [
            event(db, book.id, "manual_confirm", {"checked_at": old_iso}),
            event(db, book.id, "source_refresh", {"fetched_at": int(time.time())}),
            event(db, book.id, "auto_check", {"checked_at": old_iso}),  # the book's latest event
        ]
    assert Review(files).purge() == len(expired)
    with files.library.connection() as db:
        assert [row[0] for row in db.execute("SELECT id FROM review_event ORDER BY id")] == kept


def event(db: sqlite3.Connection, book_id: str, action: str, details: dict) -> int:
    return (
        db.execute(
            "INSERT INTO review_event(book_id,metadata_revision,action,details_json) VALUES(?,1,?,?)",
            (book_id, action, json.dumps(details)),
        ).lastrowid
        or 0
    )


def test_confirming_a_review_rehashes_the_archive_before_the_operation_lock(files, tmp_path, monkeypatch):
    identifier = import_book(files, tmp_path).id
    review = Review(files)
    preview = review.preview(identifier)
    hashed = hash_outside_operation_lock(files, monkeypatch)
    assert review.complete(identifier, preview["token"], preview["issues"]).review_state == "confirmed"
    assert hashed
