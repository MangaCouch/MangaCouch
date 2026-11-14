from __future__ import annotations

import pytest
from helpers import edit, external, import_book

from mangacouch.files import RootUnavailable, set_thread_cancel
from mangacouch.limits import ImportCancelled
from mangacouch.removals import Removals


def conflicted(sync, tmp_path):
    book = import_book(sync.files, tmp_path)
    sync.sync_one(book.id)
    edit(sync, book.id, "local")
    external(sync, book.id, "external")
    assert sync.sync_one(book.id)["state"] == "conflict"
    return sync.library.get_book(book.id)


@pytest.mark.parametrize("stop", ["shutdown", "offline"])
def test_conflict_recheck_preserves_conflict_on_interruption(sync, tmp_path, stop):
    book = conflicted(sync, tmp_path)
    root = sync.root.path
    offline = root.with_name("offline")
    if stop == "shutdown":
        set_thread_cancel(lambda: True)
    else:
        root.rename(offline)
    try:
        with pytest.raises(ImportCancelled if stop == "shutdown" else RootUnavailable):
            sync.sync_pending()
    finally:
        set_thread_cancel(None)
        if stop == "offline":
            offline.rename(root)
    assert sync._state(book.id)["last_error"] == "external_conflict"
    assert sync.sync_pending() == 0


@pytest.mark.parametrize("change", ["deleted", "matching"])
def test_conflict_recheck_settles_missing_or_matching_sidecar(sync, tmp_path, change):
    book = conflicted(sync, tmp_path)
    if change == "deleted":
        sync.path(book).unlink()
    else:
        sync.path(book).write_bytes(sync.library.native_sidecar(book.id).canonical_bytes())
    sync.sync_pending()
    assert sync.conflicts() == []
    assert sync.path(book).exists()
    assert sync._state(book.id)["last_error"] is None


def test_unchanged_conflict_restores_its_error_without_busy_loop(sync, tmp_path):
    book = conflicted(sync, tmp_path)
    with sync.library.transaction() as db:
        db.execute("UPDATE sidecar_sync SET last_error='sidecar_invalid' WHERE book_id=?", (book.id,))
    assert sync.sync_pending() == 0
    assert sync._state(book.id)["last_error"] == "external_conflict"


def test_changed_archive_cannot_be_deleted_with_a_new_preview(files, tmp_path):
    book = import_book(files, tmp_path)
    path = files.root.resolve(book.content.relative_path)
    path.write_bytes(b"replacement file owned by the user")
    removals = Removals(files)
    preview = removals.preview(book.id)
    assert "archive_changed" in preview["delete_blocked"]
    with pytest.raises(ValueError):
        removals.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    assert path.read_bytes() == b"replacement file owned by the user"


def test_empty_history_cursor_and_dst_calendar_days(files, monkeypatch):
    from datetime import datetime

    from helpers import content, register_book

    from mangacouch.activity import Activity
    from mangacouch.cursors import encode_cursor
    from mangacouch.models import Metadata

    activity = Activity(files.library)
    assert activity.recent(after=encode_cursor(100, 1))["items"] == []
    one, _ = register_book(files.library, content("dst-one"), Metadata(language="en"))
    register_book(files.library, content("dst-two"), Metadata(language="English"))
    timestamps = [
        int(datetime.fromisoformat(value).timestamp())
        for value in ("2026-11-01T04:30:00+00:00", "2026-11-02T04:30:00+00:00")
    ]
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO reading_event(book_id,created_at,page,completed,kind) VALUES(?,?,0,0,'progress')",
            ((one.id, value) for value in timestamps),
        )
    monkeypatch.setattr("mangacouch.activity.time.time", lambda: timestamps[-1] + 12 * 3600)
    assert len(activity.recent(timezone="America/New_York")["items"]) == 1
    stats = activity.statistics(days=3, timezone="America/New_York")
    assert [row for row in stats["daily"] if row["updates"]] == [
        {"day": "2026-11-01", "books": 1, "updates": 2}
    ]
    assert stats["languages"] == [{"language": "english", "books": 2}]


def test_index_read_error_does_not_starve_other_books(files, tmp_path, monkeypatch):
    from helpers import make_zip

    from mangacouch.page_features import PageFeatures

    books = sorted(
        [files.import_file(make_zip(tmp_path / f"{i}.zip", [f"{i}.png"])).book for i in range(2)],
        key=lambda book: book.id,
    )
    read = files.page_bytes
    attempts = []

    def fault(identifier, page):
        attempts.append(identifier)
        if identifier == books[0].id:
            raise OSError("bad sector")
        return read(identifier, page)

    monkeypatch.setattr(files, "page_bytes", fault)
    worker = PageFeatures(files)
    assert worker.index()["processed"] == 1
    for _ in range(5):
        assert worker.index()["processed"] == 0
    assert attempts.count(books[0].id) == 1
    monkeypatch.setattr(files, "page_bytes", read)
    worker.retry.clock = lambda: float("inf")
    assert worker.index()["processed"] == 1
    assert worker.status() == {"indexed_pages": 2, "total_pages": 2}


def test_bulk_cancel_keeps_completed_changes_and_survives_restart(files):
    import uuid

    from helpers import content, register_books

    from mangacouch.organization import BulkAction, BulkSelectionRequest, Organization

    books = register_books(files.library, (content(f"cancel-{i}") for i in range(3)))
    service = Organization(files.library)
    job = service.create_bulk(
        BulkSelectionRequest(
            id=str(uuid.uuid4()),
            action=BulkAction(kind="rating", rating=4),
            book_ids=[book.id for book in books],
        )
    )
    service.advance(limit=1)
    cancelled = service.cancel(job["id"])
    assert cancelled["state"] == "cancelled"
    assert cancelled["counts"]["done"] == 1 and cancelled["counts"]["cancelled"] == 2
    restarted = Organization(files.library)
    assert not restarted.advance()
    assert restarted.cancel(job["id"]) == cancelled
    assert [files.library.get_book(book.id).personal.rating for book in books] == [4, None, None]


def test_snapshot_stops_pending_deletion_and_paid_recovery(files, tmp_path, monkeypatch):
    import json

    from mangacouch.snapshots import create_snapshot, restore_snapshot
    from mangacouch.source_config import SourceVault
    from mangacouch.source_http import SourceHTTP

    book = import_book(files, tmp_path)
    removals = Removals(files)
    preview = removals.preview(book.id)
    with monkeypatch.context() as patch:
        patch.setattr(removals, "finish_deletion", lambda identifier: None)
        receipt = removals.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    assert receipt["delete_pending"]
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO download_job(id,input_url,source_json,state,details_json,created_at,updated_at) "
            "VALUES('paid','https://e-hentai.org/g/100/token/',?, 'submitting',?,1,1)",
            (
                json.dumps({"site": "ehentai", "gallery_id": 100, "gallery_token": "token"}),
                json.dumps({"phase": "uncertain", "approved_cost": 10, "submission_uncertain": True}),
            ),
        )
    archive = create_snapshot(files, tmp_path / "stopped.zip")
    restored = restore_snapshot(archive, tmp_path / "restored")
    with restored.library.connection() as db:
        saved = json.loads(db.execute("SELECT receipt_json FROM removal").fetchone()[0])
        assert not saved["delete_pending"] and saved["deletion_stopped"] == "restored_from_backup"
        job = db.execute("SELECT state,details_json FROM download_job").fetchone()
        assert job[0] == "paused" and json.loads(job[1])["phase"] == "recover"
    settings, secrets, _ = SourceVault(restored.library).load()
    assert settings.online_paused
    http = SourceHTTP(settings, secrets)
    try:
        assert http.blocked == "source_restore_paused"
    finally:
        http.close()
    assert files.root.resolve(book.content.relative_path).exists()


@pytest.mark.parametrize("damage", ["missing", "mismatch"])
def test_snapshot_missing_source_key_reports_specific_error(files, tmp_path, damage):
    from cryptography.fernet import Fernet

    from mangacouch.snapshots import create_snapshot
    from mangacouch.source_config import SourceKeyLost, SourceSettings, SourceVault

    vault = SourceVault(files.library)
    vault.save(SourceSettings(), {"eh_cookie": "secret"}, 0)
    if damage == "missing":
        vault.path.unlink()
    else:
        vault.path.write_bytes(Fernet.generate_key())
    with pytest.raises(SourceKeyLost):
        create_snapshot(files, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_unknown_credentials_are_removed_from_storage_and_backup(files, tmp_path):
    import sqlite3
    import zipfile
    from contextlib import closing

    from mangacouch.snapshots import create_snapshot
    from mangacouch.source_config import SourceVault

    vault = SourceVault(files.library)
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO source_config VALUES(1,'{}',?,1)",
            (vault.encrypt({"old_cookie": "dropped", "eh_cookie": "kept"}),),
        )
    archive = create_snapshot(files, tmp_path / "clean.zip")
    assert vault.load()[1] == {"eh_cookie": "kept"}
    with zipfile.ZipFile(archive) as source:
        path = tmp_path / "copy.sqlite"
        path.write_bytes(source.read("library.sqlite"))
    with closing(sqlite3.connect(path)) as db:
        assert vault.decrypt(db.execute("SELECT secrets FROM source_config").fetchone()[0]) == {
            "eh_cookie": "kept"
        }


def test_search_rejects_unpaired_surrogates_and_accepts_valid_pairs(files):
    from mangacouch.errors import InvalidInput
    from mangacouch.searching import search_terms

    with pytest.raises(InvalidInput):
        files.library.search(r'"\ud800"')
    with pytest.raises(InvalidInput):
        files.library.search("\ud800")
    assert search_terms(r'"\ud83d\ude00"') == ["😀"]


def test_concurrent_deletion_executors_converge(files, tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor

    book = import_book(files, tmp_path)
    removals = Removals(files)
    preview = removals.preview(book.id)
    with monkeypatch.context() as patch:
        patch.setattr(removals, "finish_deletion", lambda identifier: None)
        removals.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    barrier = threading.Barrier(2)
    verify = removals._verified

    def together(item):
        result = verify(item)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(removals, "_verified", together)
    with ThreadPoolExecutor(2) as workers:
        list(workers.map(removals.finish_deletion, [preview["id"], preview["id"]]))
    receipt = removals.list()["items"][0]
    assert not receipt["delete_pending"] and not receipt.get("deletion_stopped")
    assert all(item["file"] == "deleted" for item in receipt["items"])


def test_removal_retry_after_commit_reuses_receipt_and_finishes(files, tmp_path, monkeypatch):
    book = import_book(files, tmp_path)
    removals = Removals(files)
    preview = removals.preview_selected([book.id])
    with monkeypatch.context() as patch:

        def interrupted(identifier):
            raise ImportCancelled("deadline")

        patch.setattr(removals, "finish_deletion", interrupted)
        with pytest.raises(ImportCancelled):
            removals.remove_selected(
                preview["id"], [book.id], preview["token"], confirmed=True, delete_file=True
            )
    assert files.library.search("") == []
    receipt = removals.remove_selected(
        preview["id"], [book.id], preview["token"], confirmed=True, delete_file=True
    )
    assert not receipt["delete_pending"] and receipt["items"][0]["file"] == "deleted"


def test_restoring_missing_book_rechecks_availability_and_resyncs_sidecar(sync, tmp_path):
    book = import_book(sync.files, tmp_path)
    sync.sync_one(book.id)
    with sync.library.transaction() as db:
        db.execute("UPDATE book SET availability='missing' WHERE id=?", (book.id,))
    removals = Removals(sync.files)
    preview = removals.preview(book.id)
    removals.remove(preview["id"], book.id, preview["token"], confirmed=True)
    removals.restore(preview["id"])
    assert sync.library.get_book(book.id).availability == "available"
    assert sync._state(book.id)["pending_revision"] > sync._state(book.id)["synced_revision"]


def test_backup_has_long_work_deadline(api, monkeypatch):
    import time

    from mangacouch import api_settings, services
    from mangacouch.files import check_cancel, thread_cancelled

    create = api_settings.create_snapshot

    def slow(*args):
        time.sleep(0.03)
        check_cancel(thread_cancelled())
        return create(*args)

    monkeypatch.setattr(services, "MUTATION_TIMEOUT", 0.001)
    monkeypatch.setattr(api_settings, "create_snapshot", slow)
    response = api.post("/api/snapshots")
    assert response.status_code == 200
    archive = api.get(f"/api/snapshots/{response.json()['id']}")
    assert archive.status_code == 200 and archive.content.startswith(b"PK")


@pytest.mark.parametrize("wrapped", [False, True])
def test_old_snapshot_upgrade_preserves_root_unavailable_error(files, tmp_path, monkeypatch, wrapped):
    from mangacouch import snapshots
    from mangacouch.errors import InvalidInput

    archive = snapshots.create_snapshot(files, tmp_path / "before.zip")

    def offline(*args, **kwargs):
        if wrapped:
            raise InvalidInput("disconnected", code="root_unavailable")
        raise RootUnavailable("disconnected")

    monkeypatch.setattr(snapshots, "upgrade", offline)
    with pytest.raises(InvalidInput if wrapped else RootUnavailable) as error:
        snapshots.restore_snapshot(archive, tmp_path / "restored")
    if wrapped:
        assert isinstance(error.value, InvalidInput)
        assert error.value.code == "root_unavailable"
    assert not (tmp_path / "restored").exists()


def test_wal_shrinks_after_large_checkpointed_transaction(files):
    with files.library.connection() as db:
        db.execute("CREATE TABLE wal_pressure(value BLOB)")
        db.execute("INSERT INTO wal_pressure VALUES(zeroblob(?))", (70 * 1024**2,))
        wal = files.library.path.with_name(files.library.path.name + "-wal")
        assert wal.stat().st_size > 64 * 1024**2
        db.execute("INSERT INTO maintenance_stamp VALUES('wal-test',1)")
        assert wal.stat().st_size <= 64 * 1024**2


def test_missing_check_resyncs_returning_book(sync, tmp_path):
    book = import_book(sync.files, tmp_path)
    sync.sync_one(book.id)
    with sync.library.transaction() as db:
        db.execute("UPDATE book SET availability='missing' WHERE id=?", (book.id,))
    Removals(sync.files).check_page()
    assert sync.library.get_book(book.id).availability == "available"
    assert sync._state(book.id)["pending_revision"] > sync._state(book.id)["synced_revision"]
