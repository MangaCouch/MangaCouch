from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CRASH_AFTER,
    CSRF,
    ORIGIN,
    PASSCODE,
    confirmed,
    content,
    edit,
    finish_scan,
    import_book,
    inbox,
    login,
    make_zip,
    native_file,
    pending_sidecars,
    python,
    register_book,
)

from mangacouch import snapshots
from mangacouch.auth import Auth, Unauthorized
from mangacouch.collections import Collections
from mangacouch.configuration import HTTPSettings, load_http_settings, save_http_settings
from mangacouch.database import Library
from mangacouch.files import RootUnavailable
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState, SourceRating, read_native
from mangacouch.preferences import LibraryPreferences, load_preferences, save_preferences
from mangacouch.review import Review
from mangacouch.scanning import Scanner
from mangacouch.searching import SearchOptions
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import InvalidSnapshot, create_snapshot, restore_snapshot
from mangacouch.source_config import SourceSettings, SourceVault


def test_snapshot_preserves_pending_edits_personal_and_jobs_with_offline_root(
    sync: SidecarSync, tmp_path: Path
):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "not yet synchronized")
    personal = PersonalState(rating=4.5, completed=True)
    sync.library.save_personal(book_id, personal, expected_revision=1)
    with sync.library.transaction() as db:
        db.execute("INSERT INTO import_job(id,state,kind) VALUES('unfinished','copying','copy')")
    sync.files.limits = replace(sync.files.limits, max_archive_bytes=4096, max_pages=123)
    sync.root.path.rename(tmp_path / "offline")
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(snapshot) as archive:
        assert set(archive.namelist()) == {"library.sqlite", "runtime.json", "manifest.json"}
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert restored.profile.limits == sync.files.limits
    assert restored.profile.library_root_hint == str(sync.root.path)
    assert restored.library.get_book(book_id).personal == personal
    assert restored.library.get_book(book_id).metadata.title == "not yet synchronized"
    assert pending_sidecars(restored.library) == [(book_id, 2)]
    with restored.library.connection() as db:
        assert db.execute("SELECT state FROM import_job WHERE id='unfinished'").fetchone()[0] == "copying"
    restored_files = FileLibrary(restored.library, sync.root.path, restored.profile.limits)
    with pytest.raises(RootUnavailable):
        restored_files.refresh_availability()
    (tmp_path / "offline").rename(sync.root.path)
    assert restored_files.page_bytes(book_id, 0).startswith(b"\x89PNG")


def test_restore_old_snapshot_does_not_overwrite_newer_sidecar(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "old")
    sync.sync_one(book_id)
    archive = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    edit(sync, book_id, "newer than backup")
    sync.sync_one(book_id)
    before = sync.path(sync.library.get_book(book_id)).read_bytes()
    restored = restore_snapshot(archive, tmp_path / "restored")
    recovered = SidecarSync(FileLibrary(restored.library, sync.root.path))
    assert recovered.sync_one(book_id)["state"] == "conflict"
    assert recovered.path(recovered.library.get_book(book_id)).read_bytes() == before
    assert recovered.library.get_book(book_id).metadata.title == "old"


def test_snapshot_taken_before_sidecar_record_recognizes_published_file(
    sync: SidecarSync, tmp_path: Path, monkeypatch
):
    book_id = import_book(sync.files, tmp_path).id
    sync.sync_one(book_id)
    edit(sync, book_id, "published before DB record")

    def stop_record(*_args):
        raise OSError("simulated record failure")

    with monkeypatch.context() as patch:
        patch.setattr(sync, "_record", stop_record)
        assert sync.sync_one(book_id)["state"] == "error"
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    sync.sync_one(book_id)
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    recovered = SidecarSync(FileLibrary(restored.library, sync.root.path))
    assert recovered.sync_one(book_id)["state"] == "unchanged"
    assert pending_sidecars(recovered.library) == []


def test_snapshot_completes_while_another_connection_keeps_writing(sync: SidecarSync, tmp_path: Path):
    import_book(sync.files, tmp_path)
    with sync.library.transaction() as db:
        db.executemany(
            "INSERT INTO tag_translation VALUES('filler',?,?,'')",
            ((str(index), "x" * 4096) for index in range(1500)),
        )
    stop = threading.Event()
    writing = threading.Event()
    writes = 0
    errors = []

    def writer():
        nonlocal writes
        try:
            with sync.library.connection() as db:
                while not stop.is_set():
                    db.execute(
                        "INSERT INTO maintenance_stamp VALUES('writer',?) "
                        "ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at",
                        (writes,),
                    )
                    writes += 1
                    writing.set()
                    stop.wait(0.002)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert writing.wait(10), "Writer did not commit before the snapshot"
        started = time.monotonic()
        snapshot = create_snapshot(sync.files, tmp_path / "busy.zip")
        elapsed = time.monotonic() - started
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()
    assert not errors
    assert elapsed < 20
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    with restored.library.connection() as db:
        assert db.execute("SELECT count(*) FROM tag_translation").fetchone()[0] == 1500
        assert (
            0
            <= db.execute("SELECT updated_at FROM maintenance_stamp WHERE name='writer'").fetchone()[0]
            < writes
        )


def test_snapshot_validation_failure_leaves_no_database_copy(sync: SidecarSync, tmp_path: Path, monkeypatch):
    import_book(sync.files, tmp_path)

    def invalid(_path):
        raise InvalidSnapshot("simulated validation failure")

    monkeypatch.setattr(snapshots, "_validate_database", invalid)
    with pytest.raises(InvalidSnapshot):
        create_snapshot(sync.files, tmp_path / "invalid.zip")
    assert not (tmp_path / "invalid.zip").exists()
    assert not list(tmp_path.glob(".mangacouch-snapshot-*"))


def test_snapshot_failed_import_cannot_publish_after_restore(sync: SidecarSync, tmp_path: Path, monkeypatch):
    def failed_commit(*_args, **_kwargs):
        error = sqlite3.OperationalError("simulated commit failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    with monkeypatch.context() as patch:
        patch.setattr(sync.files, "_commit_import", failed_commit)
        with pytest.raises(sqlite3.OperationalError):
            sync.files.import_file(make_zip(tmp_path / "ready.zip"))
    assert sync.library.search("") == []
    with sync.library.connection() as db:
        job = db.execute("SELECT id,state FROM import_job").fetchone()
        assert job["state"] == "failed"
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert restored.library.search("") == []
    recovered_files = FileLibrary(restored.library, sync.root.path)
    assert recovered_files.recover() == {}
    assert restored.library.search("") == []


def test_sqlite_backup_includes_wal_and_excludes_uncommitted_edit(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id
    with sync.library.connection() as writer:
        edit(sync, book_id, "committed in WAL")
        assert sync.library.path.with_name(sync.library.path.name + "-wal").stat().st_size > 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE book SET search_text='uncommitted' WHERE id=?", (book_id,))
        snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
        writer.execute("ROLLBACK")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert restored.library.search("committed") == [book_id]
    assert restored.library.search("uncommitted") == []


@pytest.mark.parametrize(
    "damage",
    [
        "truncate",
        "checksum",
        "extra",
        "duplicate",
        "schema",
        "sql_trigger",
        "foreign_key",
        "config",
        "config_extra",
        "config_null",
    ],
)
def test_damaged_snapshot_cannot_create_or_replace_library(sync: SidecarSync, tmp_path: Path, damage: str):
    import_book(sync.files, tmp_path)
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(snapshot) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    if damage == "truncate":
        snapshot.write_bytes(snapshot.read_bytes()[:100])
    else:
        if damage == "checksum":
            members["runtime.json"] += b" "
        elif damage == "extra":
            members["../escape"] = b"bad"
        elif damage in {"schema", "sql_trigger", "foreign_key"}:
            database = tmp_path / "tampered.sqlite"
            database.write_bytes(members["library.sqlite"])
            with closing(sqlite3.connect(database)) as db:
                if damage == "schema":
                    db.execute("PRAGMA user_version=999")
                elif damage == "sql_trigger":
                    db.execute("CREATE TRIGGER surprise AFTER UPDATE ON book BEGIN DELETE FROM personal; END")
                else:
                    db.execute("DELETE FROM book")
                db.commit()
            members["library.sqlite"] = database.read_bytes()
        elif damage.startswith("config"):
            config = json.loads(members["runtime.json"])
            if damage == "config_extra":
                config["limits"]["unknown"] = 12
            else:
                config["limits"]["max_pages"] = None if damage == "config_null" else 0
            members["runtime.json"] = json.dumps(config).encode()
        if damage not in {"checksum", "extra", "duplicate"}:
            manifest = json.loads(members["manifest.json"])
            for name in manifest["files"]:
                manifest["files"][name] = {
                    "sha256": hashlib.sha256(members[name]).hexdigest(),
                    "size": len(members[name]),
                }
            members["manifest.json"] = json.dumps(manifest).encode()
        with zipfile.ZipFile(snapshot, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)
            if damage == "duplicate":
                with pytest.warns(UserWarning):
                    archive.writestr("runtime.json", members["runtime.json"])
    destination = tmp_path / "restored"
    with pytest.raises(InvalidSnapshot):
        restore_snapshot(snapshot, destination)
    assert not destination.exists() and not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob(".mangacouch-restore-*"))
    assert len(sync.library.search("")) == 1


def test_snapshot_existing_destinations_preserve_prior_files(sync: SidecarSync, tmp_path: Path):
    import_book(sync.files, tmp_path)
    output = tmp_path / "snapshot.zip"
    create_snapshot(sync.files, output)
    assert not list(tmp_path.glob(".mangacouch-snapshot-*"))
    previous = output.read_bytes()
    with pytest.raises(FileExistsError):
        create_snapshot(sync.files, output)
    with pytest.raises(FileExistsError):
        restore_snapshot(output, sync.root.path)
    assert output.read_bytes() == previous and len(sync.library.search("")) == 1


def test_snapshot_disk_error_does_not_publish_partial_zip(sync: SidecarSync, tmp_path: Path, monkeypatch):
    import_book(sync.files, tmp_path)
    previous = create_snapshot(sync.files, tmp_path / "previous.zip")
    original = previous.read_bytes()

    def disk_full(*_args, **_kwargs):
        raise OSError("simulated full disk")

    monkeypatch.setattr(zipfile.ZipFile, "write", disk_full)
    with pytest.raises(OSError):
        create_snapshot(sync.files, tmp_path / "failed.zip")
    assert not (tmp_path / "failed.zip").exists()
    assert not list(tmp_path.glob(".mangacouch-snapshot-*"))
    assert previous.read_bytes() == original


def test_interrupted_restore_is_never_opened_as_complete(sync: SidecarSync, tmp_path: Path):
    import_book(sync.files, tmp_path)
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    destination = tmp_path / "interrupted"
    code = (
        CRASH_AFTER
        + """
import sys
from pathlib import Path
from mangacouch.snapshots import restore_snapshot
crash_after(os, "link", 74)
restore_snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    )
    process = python("-c", code, snapshot, destination)
    assert process.returncode == 74, process.stderr
    assert (destination / "library.sqlite.restore-pending").exists()
    with pytest.raises(ValueError, match="incomplete"):
        Library(destination / "library.sqlite")
    restored = restore_snapshot(snapshot, tmp_path / "completed")
    assert len(restored.library.search("")) == 1


def test_restore_io_failure_leaves_original_and_existing_db_untouched(
    sync: SidecarSync, tmp_path: Path, monkeypatch
):
    import_book(sync.files, tmp_path)
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    original = snapshot.read_bytes()
    link = os.link

    def fail_config(source, target, **kwargs):
        if str(target).endswith("runtime.json"):
            raise OSError("simulated full disk")
        link(source, target, **kwargs)

    monkeypatch.setattr(snapshots.os, "link", fail_config)
    with pytest.raises(OSError):
        restore_snapshot(snapshot, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    assert snapshot.read_bytes() == original and len(sync.library.search("")) == 1


@pytest.mark.usefixtures("without_maintenance")
def test_http_snapshot_offline_restore_preserves_library_and_defers_external_sidecar(client, files, tmp_path):
    login(client)

    def request(connection, method, path, **kwargs):
        response = connection.request(method, path, headers=CSRF, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    uploaded = request(
        client,
        "POST",
        "/api/import",
        params={"filename": "原件.cbz"},
        content=make_zip(tmp_path / "sample.zip").read_bytes(),
    )["book"]
    identifier = uploaded["id"]
    base = f"/api/books/{identifier}"
    request(client, "POST", "/api/sidecar/sync", json={"book_id": identifier})
    metadata = {**uploaded["metadata"], "title": "Saved before snapshot"}
    request(client, "PUT", base + "/metadata", params={"expected_revision": 1}, json={"metadata": metadata})
    state = {**uploaded["personal"], "rating": 4.5, "resume_page": 1}
    request(client, "PUT", base + "/personal", params={"expected_revision": 1}, json={"state": state})
    collection = request(client, "POST", "/api/collections", json={"name": "Weekend"})
    collection_path = f"/api/collections/{collection['id']}"
    request(
        client,
        "POST",
        collection_path + "/items",
        params={"expected_revision": 1},
        json={"book_id": identifier},
    )
    expected_collection = request(client, "GET", collection_path)
    preferences = request(client, "GET", "/api/preferences")
    preferences["settings"]["title_preference"] = "original_title"
    expected_preferences = request(
        client,
        "PUT",
        "/api/preferences",
        params={"expected_revision": preferences["revision"]},
        json={"settings": preferences["settings"]},
    )
    key = request(client, "POST", "/api/auth/keys", json={"kind": "readonly", "label": "Reader"})["key"]
    saved = request(client, "GET", base)
    old_cookies = dict(client.cookies)
    prepared = client.post("/api/snapshots", headers=CSRF).json()
    snapshot = client.get(f"/api/snapshots/{prepared['id']}")
    assert snapshot.status_code == 200 and snapshot.headers["content-type"] == "application/zip"
    assert "attachment" in snapshot.headers["content-disposition"]
    archive = tmp_path / "snapshot.zip"
    archive.write_bytes(snapshot.content)
    sync = SidecarSync(files)
    sidecar = sync.path(files.library.get_book(identifier))
    external = json.loads(sidecar.read_text())
    external["metadata"]["title"] = "Changed after snapshot"
    sidecar.write_text(json.dumps(external))
    external_bytes = sidecar.read_bytes()
    offline = files.root.path.with_name("offline")
    files.root.path.rename(offline)
    restored = restore_snapshot(archive, tmp_path / "restored")
    assert load_http_settings(restored.library) == load_http_settings(files.library)
    recovered = FileLibrary(restored.library, files.root.path, restored.profile.limits)
    with TestClient(create_app(recovered), base_url=ORIGIN) as fresh:
        fresh.cookies.update(old_cookies)
        assert fresh.get("/api/auth/me").status_code == 401
        fresh.cookies.clear()
        login(fresh)
        diagnosis = request(fresh, "GET", "/api/diagnostics")
        assert not diagnosis["ready"] and diagnosis["books"] == 1
        # The edit was written to the sidecar when it was saved, before the snapshot.
        assert diagnosis["sidecars"]["pending"] == 0
        assert fresh.get(base).status_code == 503
        assert recovered.library.get_book(identifier).personal.model_dump() == state
        assert request(fresh, "GET", "/api/preferences") == expected_preferences
        offline.rename(files.root.path)
        assert request(fresh, "GET", "/api/diagnostics")["ready"]
        assert request(fresh, "GET", base) == saved
        assert request(fresh, "GET", collection_path) == expected_collection
        assert fresh.get(base + "/pages/1", headers={"Authorization": f"Bearer {key}"}).content.startswith(
            b"\x89PNG"
        )
        conflict = request(fresh, "POST", "/api/sidecar/sync", json={"book_id": identifier})
        assert conflict["state"] == "conflict" and sidecar.read_bytes() == external_bytes
        assert request(fresh, "GET", base)["metadata"]["title"] == "Saved before snapshot"
        resolved = request(
            fresh,
            "POST",
            f"/api/sidecar/conflicts/{conflict['conflict_id']}/resolve",
            json={"choice": "library"},
        )
        assert resolved["sidecar_sync"]["state"] in {"synced", "unchanged"}  # or maintenance wrote it
        assert json.loads(sidecar.read_text())["metadata"]["title"] == "Saved before snapshot"
        again = request(fresh, "POST", "/api/sidecar/sync", json={"book_id": identifier})
        assert again["state"] == "unchanged"
        assert request(fresh, "GET", base)["personal"] == state


def collection_order(files: FileLibrary, auth: Auth, tmp_path: Path):
    service = Collections(files.library)
    collection = service.create("周末阅读")
    ids = [register_book(files.library, content(str(i), f"{i}.zip"))[0].id for i in range(3)]
    for identifier in ids:
        collection = service.edit_item(collection["id"], identifier, collection["revision"])
    collection = service.edit_item(collection["id"], ids[0], collection["revision"], action="move")
    files.library.save_personal(ids[1], PersonalState(completed=True), expected_revision=1)

    def verify(restored: FileLibrary):
        assert Collections(restored.library).get(collection["id"]) == collection
        assert Collections(restored.library).context(collection["id"])["continue_id"] == ids[2]

    return verify


def global_preferences(files: FileLibrary, auth: Auth, tmp_path: Path):
    saved = save_preferences(
        files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
    )

    def verify(restored: FileLibrary):
        assert load_preferences(restored.library) == saved

    return verify


def server_settings_and_credentials(files: FileLibrary, auth: Auth, tmp_path: Path):
    settings = HTTPSettings(public_origin="https://library.example", max_upload_bytes=12345)
    save_http_settings(files.library, settings)
    _, key = auth.create_key("readonly", "retained reader")
    _, session = auth.login(PASSCODE, "old browser")

    def verify(restored: FileLibrary):
        assert load_http_settings(restored.library) == settings
        restored_auth = Auth(restored.library)
        assert restored_auth.authenticate(key, session=False).kind == "readonly"
        # Browser sessions are not carried over: a restored library needs a fresh sign-in.
        with pytest.raises(Unauthorized):
            restored_auth.authenticate(session, session=True)
        session_id, fresh = restored_auth.login(PASSCODE, "fresh browser")
        assert restored_auth.authenticate(fresh, session=True).id == session_id

    return verify


def source_settings_without_credentials(files: FileLibrary, auth: Auth, tmp_path: Path):
    vault = SourceVault(files.library)
    vault.save(SourceSettings(comments=False), {}, 0)
    assert not vault.path.exists()

    def verify(restored: FileLibrary):
        restored_vault = SourceVault(restored.library)
        assert restored_vault.load()[0].comments is False
        assert not restored_vault.path.exists()

    return verify


def confirmation_evidence(files: FileLibrary, auth: Auth, tmp_path: Path):
    _, book = confirmed(files, tmp_path)
    with files.library.transaction() as db:
        db.execute("UPDATE book SET review_state='needs_review' WHERE id=?", (book.id,))

    def verify(restored: FileLibrary):
        review = Review(restored)
        assert review.automatic(book.id, review.preview(book.id)["token"])["outcome"] == "confirmed"
        assert review.preview(book.id)["issues"] == ["source_absent"]

    return verify


def imported_native_baseline(files: FileLibrary, auth: Auth, tmp_path: Path):
    _, native = native_file(make_zip(inbox(files) / "scanned.cbz"))
    scanner = Scanner(files)
    finish_scan(scanner, scanner.start()["id"])
    book = files.library.get_book(files.library.search("")[0])
    assert book.metadata == native.metadata

    def verify(restored: FileLibrary):
        sync = SidecarSync(restored)
        assert sync.sync_one(book.id)["state"] == "unchanged"
        assert read_native(sync.path(book).read_bytes(), book.content.sha256).metadata == native.metadata

    return verify


def paused_scan_and_its_receipts(files: FileLibrary, auth: Auth, tmp_path: Path):
    folder = inbox(files)
    for i in range(55):
        (folder / f"{i:03}.cbz").write_bytes(b"broken")
    scanner = Scanner(files)
    identifier = scanner.start(keep_source=True)["id"]
    scanner.step(identifier, limit=3)
    scanner.pause(identifier)

    def verify(restored: FileLibrary):
        service = Scanner(restored)
        assert service.get(identifier)["keep_source"]
        service.resume(identifier)
        assert finish_scan(service, identifier)["counts"]["error"] == 55
        first = service.get(identifier)
        assert len(first["items"]) == 50 and len(service.get(identifier, after=first["after"])["items"]) == 5

    return verify


def search_filters(files: FileLibrary, auth: Auth, tmp_path: Path):
    rated, _ = register_book(
        files.library, content("rated"), Metadata(source_rating=SourceRating(average=4.8))
    )
    register_book(files.library, content("unrated"))
    done, _ = register_book(files.library, content("done"), Metadata(source_rating=SourceRating(average=1.0)))
    with files.library.transaction() as db:
        db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (done.id,))
    options = SearchOptions(review="needs_review", source_rating_min=0.0)
    assert files.library.search("", options=options) == [rated.id]
    pending = pending_sidecars(files.library)

    def verify(restored: FileLibrary):
        assert restored.library.search("", options=options) == [rated.id]
        assert pending_sidecars(restored.library) == pending

    return verify


def running_source_check(files: FileLibrary, auth: Auth, tmp_path: Path):
    book_id = import_book(files, tmp_path).id
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO source_check(book_id,state,generation,updated_at) VALUES(?,'running',3,0)",
            (book_id,),
        )

    def verify(restored: FileLibrary):
        with restored.library.connection() as db:
            row = db.execute(
                "SELECT state,generation FROM source_check WHERE book_id=?", (book_id,)
            ).fetchone()
        # The snapshot interrupted the check, so it runs again under a new generation.
        assert tuple(row) == ("queued", 4)

    return verify


@pytest.mark.parametrize(
    "state",
    [
        collection_order,
        global_preferences,
        server_settings_and_credentials,
        source_settings_without_credentials,
        confirmation_evidence,
        imported_native_baseline,
        paused_scan_and_its_receipts,
        search_filters,
        running_source_check,
    ],
    ids=lambda state: state.__name__,
)
def test_state_survives_snapshot_restore(files: FileLibrary, auth: Auth, tmp_path: Path, state):
    verify = state(files, auth, tmp_path)
    restored = restore_snapshot(create_snapshot(files, tmp_path / "snapshot.zip"), tmp_path / "restored")
    verify(FileLibrary(restored.library, files.root.path, restored.profile.limits))
