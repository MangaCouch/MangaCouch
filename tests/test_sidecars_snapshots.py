from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from helpers import add, edit, external, make_zip, pending_sidecars, ready
from helpers import finish_download as finish

from mangacouch import snapshots
from mangacouch.database import Library, RevisionConflict
from mangacouch.eze import eze_path
from mangacouch.eze_cache import EzeCache
from mangacouch.files import RootUnavailable
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState, read_native
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import InvalidSnapshot, create_snapshot, restore_snapshot


def test_sync_coalesces_edits_and_does_not_export_personal_state(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "first")
    edit(sync, book_id, "latest")
    personal = PersonalState(rating=4.5, completed=True, resume_page=0, bookmarks=[0])
    sync.library.save_personal(book_id, personal, expected_revision=1)
    assert sync.batch()["books"][book_id]["state"] == "synced"
    path = sync.path(sync.library.get_book(book_id))
    data = path.read_bytes()
    value = json.loads(data)
    assert value["metadata"]["title"] == "latest" and value["revision"] == 3
    assert set(value) == {"schema", "content_sha256", "revision", "metadata"}
    assert book_id.encode() not in data and b'"bookmarks"' not in data and b'"completed"' not in data
    assert pending_sidecars(sync.library) == []
    before = path.stat().st_mtime_ns
    assert sync.sync_one(book_id)["state"] == "unchanged"
    assert path.stat().st_mtime_ns == before and path.read_bytes() == data
    assert sync.library.get_book(book_id).personal == personal


def test_format_only_external_change_is_observed_without_rewrite(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    data = json.dumps(json.loads(path.read_bytes()), indent=4).encode()
    path.write_bytes(data)
    before = path.stat().st_mtime_ns
    assert sync.sync_one(book_id)["state"] == "unchanged"
    assert path.read_bytes() == data and path.stat().st_mtime_ns == before and sync.conflicts() == []
    assert sync._state(book_id)["observed_hash"] == hashlib.sha256(data).hexdigest()


def test_new_db_edit_during_publish_remains_pending(sync: SidecarSync, tmp_path: Path, monkeypatch):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "published")
    publish = sync._publish

    def edit_while_publishing(target, data):
        publish(target, data)
        edit(sync, book_id, "newer DB edit")

    with monkeypatch.context() as patch:
        patch.setattr(sync, "_publish", edit_while_publishing)
        assert sync.sync_one(book_id) == {"state": "pending", "revision": 2}
    assert pending_sidecars(sync.library) == [(book_id, 3)]
    assert sync.sync_one(book_id) == {"state": "synced", "revision": 3}
    assert (
        json.loads(sync.path(sync.library.get_book(book_id)).read_bytes())["metadata"]["title"]
        == "newer DB edit"
    )


@pytest.mark.parametrize("choice", ["library", "file", "fields"])
def test_external_edits_require_explicit_resolution(sync: SidecarSync, tmp_path: Path, choice: str):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "baseline")
    sync.sync_one(book_id)
    edit(sync, book_id, "local")
    original = external(sync, book_id, "external")
    result = sync.sync_one(book_id)
    assert result["state"] == "conflict" and len(sync.conflicts()) == 1
    assert sync.sync_one(book_id) == result
    conflict = sync.conflicts()[0]
    assert conflict["fields"] == ["title", "language"]
    assert conflict["library"]["metadata"]["title"] == "local"
    assert conflict["file"]["metadata"]["title"] == "external"
    assert sync.path(sync.library.get_book(book_id)).read_bytes() == original
    assert sync.library.get_book(book_id).metadata.title == "local"
    if choice == "fields":
        book = sync.resolve(result["conflict_id"], fields={"title": "file", "language": "library"})
        assert book.metadata.title == "external" and book.metadata.language == "en"
        assert book.metadata.protected_fields == ["language", "title"]
        assert book.metadata.field_sources == {"title": "manual", "language": "manual"}
    else:
        book = sync.resolve(result["conflict_id"], choice=choice)  # type: ignore[arg-type]
        assert book.metadata.title == {"library": "local", "file": "external"}[choice]
    assert sync.path(book).read_bytes() == original
    assert sync.conflicts() == [] and pending_sidecars(sync.library)
    assert sync.sync_one(book_id)["state"] in {"synced", "unchanged"}
    assert pending_sidecars(sync.library) == []
    written = json.loads(sync.path(book).read_bytes())
    assert written["metadata"]["title"] == book.metadata.title
    assert sync.library.search(book.metadata.title or "") == [book_id]


@pytest.mark.parametrize("change", ["db", "external"])
def test_conflict_preview_cannot_be_applied_after_a_new_change(
    sync: SidecarSync, tmp_path: Path, change: str
):
    book_id = add(sync, tmp_path)
    sync.sync_one(book_id)
    external(sync, book_id, "external")
    conflict_id = sync.sync_one(book_id)["conflict_id"]
    if change == "db":
        edit(sync, book_id, "new local")
    else:
        external(sync, book_id, "new external")
    original = sync.path(sync.library.get_book(book_id)).read_bytes()
    with pytest.raises(RevisionConflict):
        sync.resolve(conflict_id, choice="file")
    assert sync.path(sync.library.get_book(book_id)).read_bytes() == original
    assert sync.sync_one(book_id)["conflict_id"] != conflict_id


@pytest.mark.parametrize("damage", ["truncated", "schema", "binding", "private"])
def test_invalid_sidecar_is_preserved_and_not_imported(sync: SidecarSync, tmp_path: Path, damage: str):
    book_id = add(sync, tmp_path)
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    value = json.loads(path.read_bytes())
    if damage == "schema":
        value["schema"] = "unknown/9"
    elif damage == "binding":
        value["content_sha256"] = "0" * 64
    elif damage == "private":
        value["metadata"]["archiver_key"] = "DO_NOT_SAVE"
    data = b'{"broken"' if damage == "truncated" else json.dumps(value).encode()
    path.write_bytes(data)
    assert sync.sync_one(book_id) == {"state": "error", "error": "ValueError"}
    assert path.read_bytes() == data and sync.library.get_book(book_id).metadata == Metadata()
    with sync.library.connection() as db:
        assert "DO_NOT_SAVE" not in "\n".join(db.iterdump())


def test_deleted_sidecar_is_rewritten_from_the_library(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "library title")
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    path.unlink()
    assert sync.sync_one(book_id)["state"] == "synced"
    assert json.loads(path.read_bytes())["metadata"]["title"] == "library title"
    assert sync.conflicts() == []


def test_eze_timestamp_and_source_fields_survive_sync_and_field_selection(sync: SidecarSync, tmp_path: Path):
    source = make_zip(tmp_path / "eze.zip")
    source.with_suffix(".eze.json").write_bytes((Path(__file__).parent / "fixtures/eze.json").read_bytes())
    book = sync.files.import_file(source).book
    assert sync.sync_one(book.id)["state"] == "synced"
    path = sync.path(book)
    native = read_native(path.read_bytes(), book.content.sha256)
    assert native.metadata.uploaded_at is not None
    assert native.metadata.source_rating == book.metadata.source_rating
    native.metadata.title = "External title"
    path.write_bytes(native.canonical_bytes())
    result = sync.sync_one(book.id)
    resolved = sync.resolve(result["conflict_id"], fields={"title": "file"})
    assert resolved.metadata.uploaded_at == book.metadata.uploaded_at
    assert (
        resolved.metadata.source == book.metadata.source and resolved.metadata.parent == book.metadata.parent
    )
    assert resolved.metadata.title == "External title"
    assert sync.sync_one(book.id)["state"] == "synced"


@pytest.mark.parametrize("method", ["_publish", "_record"])
def test_process_exit_at_sidecar_boundary_recovers(sync: SidecarSync, tmp_path: Path, method: str):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "baseline")
    sync.sync_one(book_id)
    edit(sync, book_id, "durable new value")
    code = """
import os, sys
from pathlib import Path
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary
from mangacouch.sidecars import SidecarSync
sync = SidecarSync(FileLibrary(Library(Path(sys.argv[1])), Path(sys.argv[2])))
original = getattr(sync, sys.argv[4])
def stop(*args):
    original(*args)
    os._exit(73)
setattr(sync, sys.argv[4], stop)
sync.sync_one(sys.argv[3])
"""
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.run(
        [sys.executable, "-B", "-c", code, str(sync.library.path), str(sync.root.path), book_id, method],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 73, process.stderr
    reopened = SidecarSync(FileLibrary(Library(sync.library.path), sync.root.path))
    assert reopened.sync_one(book_id)["state"] in {"synced", "unchanged"}
    assert pending_sidecars(reopened.library) == [] and reopened.conflicts() == []
    assert not list(sync.root.path.rglob(".mangacouch-sidecar-*"))
    native = read_native(
        sync.path(sync.library.get_book(book_id)).read_bytes(), sync.library.get_book(book_id).content.sha256
    )
    assert native.metadata.title == "durable new value"


def test_failed_replace_leaves_no_temporary_file_and_is_retried(
    sync: SidecarSync, tmp_path: Path, monkeypatch
):
    import mangacouch.sidecars as sidecars

    book_id = add(sync, tmp_path)
    replace_file = sidecars.os.replace

    def fail(*_args):
        raise OSError("simulated disk error")

    with monkeypatch.context() as patch:
        patch.setattr(sidecars.os, "replace", fail)
        assert sync.sync_one(book_id) == {"state": "error", "error": "OSError"}
    assert sidecars.os.replace is replace_file
    assert not list(sync.root.path.rglob(".mangacouch-sidecar-*"))
    assert pending_sidecars(sync.library) == [(book_id, 1)]
    assert sync.sync_one(book_id)["state"] == "synced"


def test_batch_isolates_bad_sidecar_and_stops_on_offline_root(sync: SidecarSync, tmp_path: Path):
    first, second = add(sync, tmp_path, "first"), add(sync, tmp_path, "second")
    sync.path(sync.library.get_book(first)).write_bytes(b"invalid")
    result = sync.batch()["books"]
    assert result[first]["state"] == "error" and result[second]["state"] == "synced"
    first_page = sync.batch(limit=1)
    second_page = sync.batch(limit=1, after_id=first_page["after_id"])
    assert set(first_page["books"]) | set(second_page["books"]) == {first, second}
    sync.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        sync.batch()
    assert set(sync.library.search("")) == {first, second}


def test_snapshot_preserves_pending_edits_personal_and_jobs_with_offline_root(
    sync: SidecarSync, tmp_path: Path
):
    book_id = add(sync, tmp_path)
    edit(sync, book_id, "not yet synchronized")
    personal = PersonalState(rating=4.5, completed=True, bookmarks=[0])
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
    book_id = add(sync, tmp_path)
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
    book_id = add(sync, tmp_path)
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
    add(sync, tmp_path)
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
    add(sync, tmp_path)

    def invalid(_path):
        raise InvalidSnapshot("simulated validation failure")

    monkeypatch.setattr(snapshots, "_validate_database", invalid)
    with pytest.raises(InvalidSnapshot):
        create_snapshot(sync.files, tmp_path / "invalid.zip")
    assert not (tmp_path / "invalid.zip").exists()
    assert not list(tmp_path.glob(".mangacouch-snapshot-*"))


def test_snapshot_ready_import_requires_explicit_resume(sync: SidecarSync, tmp_path: Path, monkeypatch):
    def failed_commit(*_args):
        raise sqlite3.OperationalError("simulated commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(sync.files, "_commit_import", failed_commit)
        with pytest.raises(sqlite3.OperationalError):
            sync.files.import_file(make_zip(tmp_path / "ready.zip"))
    assert sync.library.search("") == []
    with sync.library.connection() as db:
        job = db.execute("SELECT id,state FROM import_job").fetchone()
        assert job["state"] == "ready"
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert restored.library.search("") == []
    recovered_files = FileLibrary(restored.library, sync.root.path)
    assert recovered_files.recover() == {job["id"]: "recovered"}
    assert recovered_files.page_bytes(restored.library.search("")[0], 0).startswith(b"\x89PNG")


def test_sqlite_backup_includes_wal_and_excludes_uncommitted_edit(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
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
    add(sync, tmp_path)
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
    add(sync, tmp_path)
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
    add(sync, tmp_path)
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
    add(sync, tmp_path)
    snapshot = create_snapshot(sync.files, tmp_path / "snapshot.zip")
    destination = tmp_path / "interrupted"
    code = """
import os, sys
from pathlib import Path
from mangacouch.snapshots import restore_snapshot
original = os.link
def stop(source, target, **kwargs):
    original(source, target, **kwargs)
    os._exit(74)
os.link = stop
restore_snapshot(Path(sys.argv[1]), Path(sys.argv[2]))
"""
    process = subprocess.run(
        [sys.executable, "-B", "-c", code, str(snapshot), str(destination)],
        env={
            **os.environ,
            "PYTHONPATH": str(Path(__file__).parents[1] / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert process.returncode == 74, process.stderr
    assert (destination / "library.sqlite.restore-pending").exists()
    with pytest.raises(ValueError, match="incomplete"):
        Library(destination / "library.sqlite")
    restored = restore_snapshot(snapshot, tmp_path / "completed")
    assert len(restored.library.search("")) == 1


def test_restore_io_failure_leaves_original_and_existing_db_untouched(
    sync: SidecarSync, tmp_path: Path, monkeypatch
):
    add(sync, tmp_path)
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


def test_cli_sync_conflict_resolution_and_restore_loads_config(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}

    def run(arguments, status=0):
        result = subprocess.run(
            [sys.executable, "-B", "-m", "mangacouch", *map(str, arguments)],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        assert result.returncode == status, result.stdout + result.stderr
        return json.loads(result.stdout)

    context = ["--db", sync.library.path, "--root", sync.root.path]
    assert run([*context, "sync"])["books"][book_id]["state"] == "synced"
    external(sync, book_id, "CLI external")
    conflict_id = run([*context, "sync", "--book-id", book_id])["conflict_id"]
    assert run([*context, "conflicts"])["conflicts"][0]["id"] == conflict_id
    run([*context, "resolve", conflict_id, "--choice", "file"])
    snapshot = tmp_path / "snapshot.zip"
    run([*context, "--max-bytes", 10, "snapshot", snapshot])
    restored = tmp_path / "restored"
    assert run(["restore", snapshot, restored])["restored"] is True
    new_context = ["--db", restored / "library.sqlite"]
    assert run([*new_context, "list"])["books"][0]["title"] == "CLI external"
    run([*new_context, "import", tmp_path / "sample.zip"], status=1)
    sync.root.path.rename(tmp_path / "offline")
    assert run([*new_context, "doctor"], status=2)["error"] == "root_unavailable"
    assert len(run([*new_context, "list"])["books"]) == 1


def test_snapshot_without_credentials_needs_no_source_key(files, tmp_path):
    from mangacouch.snapshots import create_snapshot, restore_snapshot
    from mangacouch.source_config import SourceSettings, SourceVault

    vault = SourceVault(files.library)
    vault.save(SourceSettings(comments=False), {}, 0)
    assert not vault.path.exists()
    snapshot = create_snapshot(files, tmp_path / "no-secrets.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    restored_vault = SourceVault(restored.library)
    assert restored_vault.load()[0].comments is False
    assert not restored_vault.path.exists()


def test_eze_refresh_mirrors_source_but_retains_native_manual_fields_and_diff(queue, files):
    service, remote = queue
    done = finish(service, ready(service))
    book = files.library.get_book(done["book_id"])
    eze = eze_path(files.root.resolve(book.content.relative_path))
    native = SidecarSync(files).path(book)
    changed = book.metadata.model_copy(deep=True)
    changed.title = "Hand edited"
    book = Review(files).edit(book.id, changed, expected_revision=book.metadata_revision)
    SidecarSync(files).sync_one(book.id)
    before = native.read_bytes()
    remote.title = "Fresh source title"
    observation = service.sources.fetch(book.metadata.source, comments=False)
    service.online.apply(book.id, observation, book.metadata_revision)
    assert native.read_bytes() == before
    assert json.loads(eze.read_bytes())["gallery_info"]["title"] == "Fresh source title"
    assert json.loads(eze.read_bytes())["gdata"]["title"] == "Fresh source title"
    current = files.library.get_book(book.id)
    assert current.metadata.title == "Hand edited"
    assert "title" in current.metadata.protected_fields
    candidate = Review(files).preview(book.id)["candidates"]["online:ehentai"]
    assert candidate["metadata"]["title"] == "Fresh source title"
    SidecarSync(files).sync_one(book.id)
    assert json.loads(native.read_bytes())["metadata"]["title"] == "Hand edited"


def test_eze_write_failure_remains_pending_and_recovers_without_touching_native(queue, files, monkeypatch):
    service, remote = queue
    done = finish(service, ready(service))
    book = files.library.get_book(done["book_id"])
    native = SidecarSync(files).path(book).read_bytes()
    remote.title = "Next source"
    observed = service.sources.fetch(book.metadata.source, comments=False)
    import mangacouch.eze_cache as cache

    replace = cache.os.replace
    monkeypatch.setattr(cache.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write failure")))
    service.online.apply(book.id, observed, book.metadata_revision)
    with files.library.connection() as db:
        assert db.execute("SELECT synced_hash FROM eze_cache").fetchone()[0] is None
    assert SidecarSync(files).path(book).read_bytes() == native
    # A write that keeps failing is no progress: the maintenance worker must idle, not spin.
    assert EzeCache(files).sync_pending() == 0
    monkeypatch.setattr(cache.os, "replace", replace)
    assert EzeCache(files).sync_pending() == 1
    assert (
        json.loads(eze_path(files.root.resolve(book.content.relative_path)).read_bytes())["gallery_info"][
            "title"
        ]
        == "Next source"
    )


def test_restored_snapshot_requeues_running_source_checks(sync: SidecarSync, tmp_path: Path):
    book_id = add(sync, tmp_path)
    with sync.library.transaction() as db:
        db.execute("UPDATE source_check SET state='running',generation=3 WHERE book_id=?", (book_id,))
    restored = restore_snapshot(create_snapshot(sync.files, tmp_path / "running.zip"), tmp_path / "restored")
    with restored.library.connection() as db:
        row = db.execute("SELECT state,generation FROM source_check WHERE book_id=?", (book_id,)).fetchone()
    assert tuple(row) == ("queued", 4)
