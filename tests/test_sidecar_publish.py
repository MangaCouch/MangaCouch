"""Every committed metadata change publishes the native sidecar; failed writes are retried."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from helpers import (
    CSRF,
    edit,
    external,
    import_book,
    login,
    make_zip,
    parts,
    pending_sidecars,
    ready,
    save_metadata,
)
from helpers import finish_download as finish

from mangacouch import maintenance
from mangacouch.eze import eze_path
from mangacouch.eze_cache import EzeCache
from mangacouch.ingest import FileLibrary
from mangacouch.maintenance import Retry
from mangacouch.removals import Removals
from mangacouch.sidecars import SidecarSync

# Written by the request, or already by background maintenance between its commit and write.
WRITTEN = {"synced", "unchanged"}


def written(files: FileLibrary, book_id: str) -> dict:
    book = files.library.get_book(book_id)
    return json.loads(SidecarSync(files).path(book).read_bytes())["metadata"]


def settled(files: FileLibrary, book_id: str) -> bool:
    return all(book != book_id for book, _ in pending_sidecars(files.library))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def failing_writes(monkeypatch):
    """Make sidecar publication fail (a read-only folder, a full disk) until `restore()`."""
    publish = SidecarSync._publish
    calls = []

    def fail(*args):
        calls.append(args)
        raise PermissionError("injected sidecar write failure")

    monkeypatch.setattr(SidecarSync, "_publish", staticmethod(fail))

    class Control:
        attempts = calls

        @staticmethod
        def restore():
            monkeypatch.setattr(SidecarSync, "_publish", staticmethod(publish))

    return Control


@pytest.mark.usefixtures("without_maintenance")
def test_upload_edit_candidate_and_conflict_choices_write_the_sidecar(client, files, tmp_path):
    login(client)
    uploaded = client.post(
        "/api/import", params={"filename": "作品.cbz"}, files=parts(tmp_path), headers=CSRF
    )
    assert uploaded.status_code == 200, uploaded.text
    identifier = uploaded.json()["book"]["id"]
    assert uploaded.json()["sidecar_sync"]["state"] in WRITTEN
    assert written(files, identifier)["title"] == "本地标题" and settled(files, identifier)

    base = f"/api/books/{identifier}"
    metadata = {**uploaded.json()["book"]["metadata"], "title": "Edited title"}
    saved = client.put(
        base + "/metadata", params={"expected_revision": 1}, json={"metadata": metadata}, headers=CSRF
    )
    assert saved.status_code == 200 and saved.json()["sidecar_sync"]["state"] in WRITTEN
    assert files.library.get_book(identifier).metadata.title == "Edited title"
    assert written(files, identifier)["title"] == "Edited title" and settled(files, identifier)

    preview = client.get(base + "/review").json()
    candidate = preview["candidates"]["eze:info"]
    choices = {field: "candidate" if field == "title" else "current" for field in candidate["fields"]}
    picked = client.post(
        base + "/review/candidate",
        json={"token": preview["token"], "candidate": "eze:info", "choices": choices},
        headers=CSRF,
    )
    assert picked.status_code == 200 and picked.json()["sidecar_sync"]["state"] in WRITTEN
    assert files.library.get_book(identifier).metadata.title == "Eze title"
    assert written(files, identifier)["title"] == "Eze title" and settled(files, identifier)

    sync = SidecarSync(files)
    for choice, title in (("library", "Outside 1"), ("file", "Outside 2"), ("fields", "Outside 3")):
        before = files.library.get_book(identifier).metadata.title
        external(sync, identifier, title, language="ja")
        conflict = client.post("/api/sidecar/sync", json={"book_id": identifier}, headers=CSRF).json()
        assert conflict["state"] == "conflict"
        if choice == "fields":
            listed = client.get("/api/sidecar/conflicts", params={"book_id": identifier}).json()["items"]
            body = {
                "fields": {field: "file" if field == "title" else "library" for field in listed[0]["fields"]}
            }
        else:
            body = {"choice": choice}
        resolved = client.post(
            f"/api/sidecar/conflicts/{conflict['conflict_id']}/resolve", json=body, headers=CSRF
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["sidecar_sync"]["state"] in {"synced", "unchanged"}
        book = files.library.get_book(identifier)
        assert book.metadata.title == (before if choice == "library" else title)
        assert written(files, identifier) == book.metadata.model_dump(mode="json")
        assert settled(files, identifier)


@pytest.mark.usefixtures("without_maintenance")
def test_failed_sidecar_write_reports_saved_pending_and_is_written_later(
    client, files, tmp_path, failing_writes
):
    login(client)
    upload = client.post(
        "/api/import",
        params={"filename": "a.cbz"},
        content=make_zip(tmp_path / "a.zip").read_bytes(),
        headers=CSRF,
    )
    identifier = upload.json()["book"]["id"]
    assert upload.status_code == 200 and upload.json()["sidecar_sync"]["state"] == "pending"
    metadata = {**upload.json()["book"]["metadata"], "title": "Saved anyway"}
    saved = client.put(
        f"/api/books/{identifier}/metadata",
        params={"expected_revision": 1},
        json={"metadata": metadata},
        headers=CSRF,
    )
    # The database change stands; only the file is still to be written.
    assert saved.status_code == 200
    assert saved.json()["sidecar_sync"] == {"state": "pending", "error": "storage_error"}
    assert saved.json()["metadata"]["title"] == "Saved anyway"
    assert files.library.get_book(identifier).metadata.title == "Saved anyway"
    assert not SidecarSync(files).path(files.library.get_book(identifier)).exists()

    clock = Clock()
    worker = SidecarSync(files)
    worker.retry = Retry(clock=clock)
    attempts = len(failing_writes.attempts)
    assert worker.sync_pending() == 0
    assert worker.sync_pending() == 0  # backing off: no tight retry loop
    assert len(failing_writes.attempts) == attempts + 1
    failing_writes.restore()
    assert worker.sync_pending() == 0
    clock.now += 31
    assert worker.sync_pending() == 1
    assert written(files, identifier)["title"] == "Saved anyway" and settled(files, identifier)
    assert worker.sync_pending() == 0


def test_failing_books_do_not_starve_later_native_sidecars(files, tmp_path, monkeypatch):
    sync = SidecarSync(files)
    ids = sorted(import_book(files, tmp_path, f"book{index}").id for index in range(12))
    blocked = {sync.path(files.library.get_book(book)) for book in ids[:10]}
    publish = SidecarSync._publish

    def selective(target, data):
        if target in blocked:
            raise PermissionError("read-only folder")
        publish(target, data)

    monkeypatch.setattr(SidecarSync, "_publish", staticmethod(selective))
    clock = Clock()
    sync.retry = Retry(clock=clock)
    count = sum(sync.sync_pending() for _ in range(3))
    assert count == 2 and settled(files, ids[10]) and settled(files, ids[11])
    assert all(not settled(files, book) for book in ids[:10])
    blocked.clear()
    clock.now += 31
    assert sync.sync_pending() == 10
    assert pending_sidecars(files.library) == []


def test_failing_books_do_not_starve_later_eze_sidecars(files, tmp_path, monkeypatch):
    ids = sorted(import_book(files, tmp_path, f"book{index}").id for index in range(11))
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO eze_cache(book_id,payload_json) VALUES(?,?)",
            ((book, json.dumps({"gallery_info": {"title": book}})) for book in ids),
        )
    blocked = set(ids[:10])
    sync_one = EzeCache.sync_one
    attempts = []

    def selective(self, book_id):
        attempts.append(book_id)
        if book_id in blocked:
            raise PermissionError("read-only folder")
        sync_one(self, book_id)

    monkeypatch.setattr(EzeCache, "sync_one", selective)
    clock = Clock()
    cache = EzeCache(files)
    cache.retry = Retry(clock=clock)
    assert cache.sync_pending() == 0
    assert cache.sync_pending() == 1
    path = eze_path(files.root.resolve(files.library.get_book(ids[10]).content.relative_path))
    assert json.loads(path.read_bytes())["gallery_info"]["title"] == ids[10]
    # A failed write is not a missing archive, and the failing books wait instead of spinning.
    assert {files.library.get_book(book).availability for book in ids} == {"available"}
    assert cache.sync_pending() == 0 and len(attempts) == 11
    blocked.clear()
    clock.now += 31
    assert cache.sync_pending() == 10
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM eze_cache WHERE synced_hash IS NULL").fetchone()[0] == 0


@pytest.fixture
def fast_maintenance(monkeypatch):
    monkeypatch.setattr(maintenance, "IDLE_SECONDS", 0.05)


def test_maintenance_worker_writes_pending_sidecars(fast_maintenance, client, files, tmp_path):
    sync = SidecarSync(files)
    identifier = import_book(files, tmp_path).id
    edit(sync, identifier, "Background title")
    deadline = time.monotonic() + 10
    while not settled(files, identifier) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert written(files, identifier)["title"] == "Background title"


def test_source_check_refresh_writes_the_sidecar(queue, files):
    service, remote = queue
    done = finish(service, ready(service))
    book = files.library.get_book(done["book_id"])
    book = save_metadata(
        files.library,
        book.id,
        book.metadata.model_copy(update={"title": None}),
        expected_revision=book.metadata_revision,
    )
    sync = SidecarSync(files)
    assert sync.sync_one(book.id)["state"] == "synced" and written(files, book.id)["title"] is None
    remote.title = "Fresh from the source"
    with files.library.transaction() as db:
        db.execute("UPDATE source_check SET state='queued',retry_at=0 WHERE book_id=?", (book.id,))
    assert service.checks.step()
    assert files.library.get_book(book.id).metadata.title == "Fresh from the source"
    assert written(files, book.id)["title"] == "Fresh from the source" and settled(files, book.id)


@pytest.mark.parametrize("command", ["import", "index"])
def test_cli_import_and_index_write_the_sidecar(files, tmp_path, monkeypatch, capsys, command):
    from mangacouch.__main__ import main

    source = make_zip(
        (files.root.path / "shelf.zip") if command == "index" else (tmp_path / "outside.zip"), ["cli.png"]
    )
    arguments = ["mangacouch", "--db", str(files.library.path), "--root", str(files.root.path)]
    monkeypatch.setattr(sys, "argv", [*arguments, command, str(source)])
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["sidecar_sync"]["state"] == "synced"
    book = files.library.get_book(result["book"]["id"])
    assert SidecarSync(files).path(book).is_file() and settled(files, book.id)
    if command == "index":
        assert Path(str(source) + ".mangacouch.json").is_file()


def test_moved_archive_and_restored_record_write_sidecars_again(files, tmp_path):
    sync = SidecarSync(files)
    shelf = make_zip(files.root.path / "shelf.zip", ["moved.png"])
    book = files.import_file(shelf, index_existing=True).book
    edit(sync, book.id, "Kept title")
    with files.library.transaction() as db:
        db.execute("INSERT INTO eze_cache(book_id,payload_json) VALUES(?,'{}')", (book.id,))
    assert sync.publish(book.id)["state"] == "synced"
    with files.library.connection() as db:
        assert db.execute("SELECT synced_hash FROM eze_cache").fetchone()[0] is not None
    copy = tmp_path / "copy.zip"
    copy.write_bytes(shelf.read_bytes())
    for leftover in (shelf, sync.path(book), eze_path(shelf)):
        leftover.unlink()
    moved = files.import_file(copy).book
    assert moved.id == book.id and moved.content.relative_path != book.content.relative_path
    with files.library.connection() as db:
        assert db.execute("SELECT synced_revision FROM sidecar_sync").fetchone()[0] == 0
        assert db.execute("SELECT synced_hash FROM eze_cache").fetchone()[0] is None
    assert sync.publish(moved.id)["state"] == "synced"
    assert written(files, moved.id)["title"] == "Kept title"
    assert eze_path(files.root.resolve(moved.content.relative_path)).is_file()

    removals = Removals(files)
    preview = removals.preview(moved.id)
    receipt = removals.remove(preview["id"], moved.id, preview["token"], confirmed=True)
    sync.path(moved).unlink()  # e.g. a file deletion that stopped after the sidecars
    removals.restore(receipt["id"])
    assert not settled(files, moved.id)
    assert sync.sync_pending() == 1
    assert written(files, moved.id)["title"] == "Kept title"


def test_damaged_sidecar_can_be_replaced_explicitly(files, tmp_path):
    sync = SidecarSync(files)
    book_id = import_book(files, tmp_path).id
    edit(sync, book_id, "Library")
    path = sync.path(sync.library.get_book(book_id))
    path.write_bytes(b'{"broken"')
    assert sync.sync_one(book_id) == {"state": "error", "error": "sidecar_invalid"}
    assert path.read_bytes() == b'{"broken"'
    assert sync.sync_one(book_id, replace_invalid=True)["state"] == "synced"
    assert json.loads(path.read_bytes())["metadata"]["title"] == "Library"
