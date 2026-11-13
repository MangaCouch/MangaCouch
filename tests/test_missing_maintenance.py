from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, hash_outside_operation_lock, import_book, login, save_metadata

from mangacouch import removals
from mangacouch.auth import Auth
from mangacouch.collections import Collections
from mangacouch.cursors import encode_cursor
from mangacouch.database import RevisionConflict
from mangacouch.files import ROOT_MARKER, RootUnavailable
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState
from mangacouch.removals import Removals
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync


def test_removed_books_release_sidecar_backoff_and_availability_observations(files):
    from helpers import content, register_book

    books = [register_book(files.library, content(str(i), f"{i}.cbz"))[0] for i in range(30)]
    sync = SidecarSync(files)
    sync.sync_pending(limit=30)
    assert len(sync.retry.failures) == 30
    for book in books:
        files.observe_unavailable(book, "missing")
    assert len(files.availability_failures) == 30
    service = Removals(files)
    plan = service.preview_selected([book.id for book in books])
    submit(service, plan)
    assert not files.availability_failures
    sync.sync_pending(limit=30)
    assert not sync.retry.failures


def test_availability_observations_expire_after_external_delete_or_confirmation(files):
    from helpers import content, register_book

    deleted, _ = register_book(files.library, content("deleted", "deleted.cbz"))
    confirmed, _ = register_book(files.library, content("confirmed", "confirmed.cbz"))
    for book in [deleted, confirmed]:
        files.observe_unavailable(book, "missing")
    files.observe_unavailable(confirmed, "missing")
    assert confirmed.id not in files.availability_failures
    with files.library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (deleted.id,))
    files.recheck_unavailable()
    assert not files.availability_failures
    files.observe_unavailable(deleted, "missing")
    assert not files.availability_failures


def missing(files: FileLibrary, tmp_path: Path, name: str):
    item = import_book(files, tmp_path, name)
    path = files.root.resolve(item.content.relative_path)
    data = path.read_bytes()
    path.unlink()
    return item, path, data


def count_books(files: FileLibrary):
    with files.library.connection() as db:
        return db.execute("SELECT count(*) FROM book").fetchone()[0]


def submit(service: Removals, plan: dict):
    return service.remove_selected(plan["id"], plan["book_ids"], plan["token"], confirmed=True)


def test_check_pages_are_bounded_and_distinguish_missing_changed_and_unreadable(files: FileLibrary, tmp_path):
    good = import_book(files, tmp_path, "good")
    gone, _, _ = missing(files, tmp_path, "gone")
    changed = import_book(files, tmp_path, "changed")
    files.root.resolve(changed.content.relative_path).write_bytes(b"changed")
    unreadable = import_book(files, tmp_path, "unreadable")
    path = files.root.resolve(unreadable.content.relative_path)
    path.unlink()
    path.symlink_to(files.root.resolve(good.content.relative_path))
    service = Removals(files)
    results = []
    after = ""
    through = None
    count = 0
    while True:
        page = service.check_page(after=after, through_id=through, limit=2)
        count += page["checked"]
        results.extend(page["items"])
        assert page["checked"] <= 2
        after, through = page["after"], page["through_id"]
        if after is None:
            break
    assert count == 4
    assert {row["book_id"]: row["state"] for row in results} == {
        gone.id: "missing",
        changed.id: "changed",
        unreadable.id: "unreadable",
    }
    assert count_books(files) == 4 and service.list()["items"] == []
    assert files.library.get_book(gone.id).availability == "missing"
    assert files.library.get_book(good.id).availability == "available"
    assert service.check_page(after=encode_cursor(through), through_id=through)["checked"] == 0


def test_unchanged_stamp_does_not_hide_read_permission_failure(files: FileLibrary, tmp_path, monkeypatch):
    item = import_book(files, tmp_path, "test")
    archive = files.root.resolve(item.content.relative_path)
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == archive:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    assert Removals(files).check_page()["items"][0]["state"] == "unreadable"
    assert files.library.get_book(item.id).id == item.id


@pytest.mark.parametrize("failure", ["missing_root", "wrong_root", "lost_during_check"])
def test_root_failure_never_commits_mass_missing_status(files: FileLibrary, tmp_path, monkeypatch, failure):
    item, _, _ = missing(files, tmp_path, "gone")
    marker = files.root.path / ROOT_MARKER
    if failure == "missing_root":
        marker.unlink()
    elif failure == "wrong_root":
        marker.write_text(json.dumps({"schema": "mangacouch.root/1", "id": "wrong"}))
    else:
        import_book(files, tmp_path, "good")
        original = files.availability

        def lose(book):
            result = original(book)
            marker.unlink()
            return result

        monkeypatch.setattr(files, "availability", lose)
    with pytest.raises(RootUnavailable):
        Removals(files).check_page()
    assert files.library.get_book(item.id).availability == "available"
    assert files.library.get_book(item.id).personal.rating is None


def test_restored_file_is_available_on_recheck_and_present_in_preview(files: FileLibrary, tmp_path):
    item, path, data = missing(files, tmp_path, "gone")
    service = Removals(files)
    service.check_page()
    assert service.preview_selected([item.id])["items"][0]["presence"] == "missing"
    path.write_bytes(data)
    assert service.check_page()["items"] == []
    assert files.library.get_book(item.id).availability == "available"
    assert service.preview_selected([item.id])["items"][0]["presence"] == "present"


def test_batch_receipt_restores_all_personal_and_unsynced_state_and_collection_order(
    files: FileLibrary, tmp_path
):
    first, keep, last = [import_book(files, tmp_path, name) for name in ["first", "keep", "last"]]
    sync = SidecarSync(files)
    for item in [first, last]:
        sync.sync_one(item.id)
    sidecars = {sync.path(item): sync.path(item).read_bytes() for item in [first, last]}
    state = PersonalState(rating=4.5, completed=True, resume_page=0)
    files.library.save_personal(first.id, state, expected_revision=1)
    save_metadata(files.library, last.id, Metadata(title="pending"), expected_revision=1)
    collections = Collections(files.library)
    collection = collections.create("order")
    for item in [first, keep, last]:
        collection = collections.edit_item(collection["id"], item.id, collection["revision"])
    archives = {item.id: files.root.resolve(item.content.relative_path) for item in [first, last]}
    saved = {path: path.read_bytes() for path in archives.values()}
    for path in archives.values():
        path.unlink()
    service = Removals(files)
    plan = service.preview_selected([last.id, first.id])
    assert len(plan["items"]) == 2 and service.list()["items"] == []
    receipt = submit(service, plan)
    assert receipt["kind"] == "selected" and receipt["book_ids"] == sorted([first.id, last.id])
    assert collections.get(collection["id"])["revision"] == collection["revision"] + 1
    assert collections.get(collection["id"])["items"] == [{"book_id": keep.id, "position": 0}]
    assert all(path.read_bytes() == data for path, data in sidecars.items())
    assert files.root.resolve(keep.content.relative_path).is_file()
    with files.library.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    for path, data in saved.items():
        path.write_bytes(data)
    service.restore(plan["id"])
    assert files.library.get_book(first.id).personal == state
    assert files.library.get_book(last.id).metadata.title == "pending"
    assert [item["book_id"] for item in collections.get(collection["id"])["items"]] == [
        first.id,
        keep.id,
        last.id,
    ]


@pytest.mark.parametrize("when", ["before", "during_capture"])
def test_one_returned_file_rejects_the_entire_batch(files: FileLibrary, tmp_path, monkeypatch, when):
    one, path, data = missing(files, tmp_path, "one")
    two, _, _ = missing(files, tmp_path, "two")
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    if when == "before":
        path.write_bytes(data)
    else:
        original = service._batch_preview

        def returned(*args, **kwargs):
            path.write_bytes(data)
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "_batch_preview", returned)
    with pytest.raises(RevisionConflict):
        submit(service, plan)
    assert count_books(files) == 2 and service.list()["items"] == []


def test_batch_state_change_and_selection_tampering_are_rejected(files: FileLibrary, tmp_path):
    one, _, _ = missing(files, tmp_path, "one")
    two, _, _ = missing(files, tmp_path, "two")
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    with pytest.raises(RevisionConflict):
        service.remove_selected(plan["id"], [one.id], plan["token"], confirmed=True)
    files.library.save_personal(two.id, PersonalState(rating=5), expected_revision=1)
    with pytest.raises(RevisionConflict):
        submit(service, plan)
    assert service.list()["items"] == []


def test_root_loss_during_removal_preserves_entire_batch(files: FileLibrary, tmp_path, monkeypatch):
    ids = [missing(files, tmp_path, name)[0].id for name in ["one", "two"]]
    service = Removals(files)
    plan = service.preview_selected(ids)
    original = removals._capture

    def lose_root(*args, **kwargs):
        result = original(*args, **kwargs)
        (files.root.path / ROOT_MARKER).unlink(missing_ok=True)
        return result

    monkeypatch.setattr(removals, "_capture", lose_root)
    with pytest.raises(RootUnavailable):
        submit(service, plan)
    assert count_books(files) == 2 and service.list()["items"] == []


@pytest.mark.parametrize("failure", ["capture", "delete"])
def test_batch_faults_leave_all_records_and_no_success_receipt(
    files: FileLibrary, tmp_path, monkeypatch, failure
):
    one, _, _ = missing(files, tmp_path, "one")
    two, _, _ = missing(files, tmp_path, "two")
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    if failure == "capture":

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(removals, "_capture", fail)
        error = sqlite3.OperationalError
    else:
        with files.library.transaction() as db:
            db.execute(
                "CREATE TRIGGER reject_second BEFORE DELETE ON book WHEN old.id='"
                + plan["book_ids"][1]
                + "' BEGIN SELECT RAISE(ABORT,'injected'); END"
            )
        error = sqlite3.IntegrityError
    with pytest.raises(error):
        submit(service, plan)
    assert count_books(files) == 2 and service.list()["items"] == []


def test_batch_receipt_is_idempotent_scoped_and_queryable_offline(files: FileLibrary, tmp_path):
    ids = [missing(files, tmp_path, name)[0].id for name in ["one", "two"]]
    service = Removals(files)
    plan = service.preview_selected(ids)

    def attempt(_):
        try:
            return submit(service, plan)
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = [value for value in pool.map(attempt, range(2)) if value is not None]
    assert receipts and all(value == receipts[0] for value in receipts)
    assert submit(service, plan) == receipts[0]
    assert len(service.list()["items"]) == 1
    (files.root.path / ROOT_MARKER).unlink()
    assert submit(service, plan) == receipts[0]
    with pytest.raises(RevisionConflict):
        service.remove(plan["id"], ids[0], plan["token"], confirmed=True)
    with pytest.raises(RevisionConflict):
        service.remove_selected(plan["id"], [ids[0]], plan["token"], confirmed=True)


def test_batch_limits_confirmation_and_pending_work_are_enforced(files: FileLibrary, tmp_path):
    item, _, _ = missing(files, tmp_path, "one")
    service = Removals(files)
    for ids in [[], [item.id, item.id], ["x"] * 101]:
        with pytest.raises(ValueError):
            service.preview_selected(ids)
    plan = service.preview_selected([item.id])
    with pytest.raises(ValueError):
        service.remove_selected(plan["id"], [item.id], plan["token"], confirmed=False)
    Scanner(files).start()
    blocked = service.preview_selected([item.id])
    assert "scan_active" in blocked["blocked"]
    with pytest.raises(ValueError):
        submit(service, blocked)
    assert service.list()["items"] == []


def test_missing_maintenance_http_permissions_and_csrf(
    client: TestClient, files: FileLibrary, auth: Auth, tmp_path
):
    item, _, _ = missing(files, tmp_path, "one")
    assert client.post("/api/removals/check", headers=CSRF, json={}).status_code == 401
    login(client)
    assert client.post("/api/removals/check", json={}).status_code == 403
    _, readonly = auth.create_key("readonly", "reader")
    assert (
        client.post(
            "/api/removals/check", headers={"Authorization": "Bearer " + readonly}, json={}
        ).status_code
        == 403
    )
    checked = client.post("/api/removals/check", headers=CSRF, json={})
    assert checked.status_code == 200 and checked.json()["items"][0]["book_id"] == item.id
    assert client.post("/api/removals/check", headers=CSRF, json={"limit": 101}).status_code == 422
    plan = client.post("/api/removals/selected/preview", headers=CSRF, json={"book_ids": [item.id]}).json()
    assert plan["items"][0]["presence"] == "missing"
    body = {k: plan[k] for k in ["id", "book_ids", "token"]} | {"confirmed": True}
    for route, payload in [
        ("/api/removals/selected/preview", {"book_ids": [item.id]}),
        ("/api/removals/selected", body),
    ]:
        assert client.post(route, json=payload).status_code == 403
        assert (
            client.post(route, headers={"Authorization": "Bearer " + readonly}, json=payload).status_code
            == 403
        )
    result = client.post("/api/removals/selected", headers=CSRF, json=body)
    assert result.status_code == 200 and result.json()["book_ids"] == [item.id]
    assert client.get("/api/removals").json()["items"][0]["kind"] == "selected"
    assert client.get(f"/api/removals/{plan['id']}").status_code == 404


def test_missing_check_probes_archives_before_the_operation_lock(files, tmp_path, monkeypatch):
    present = import_book(files, tmp_path, "present.cbz")
    gone, _, _ = missing(files, tmp_path, "gone.cbz")
    hashed = hash_outside_operation_lock(files, monkeypatch)
    page = Removals(files).check_page()
    assert hashed and [item["book_id"] for item in page["items"]] == [gone.id]
    assert files.library.get_book(present.id).availability == "available"
    assert files.library.get_book(gone.id).availability == "missing"


def test_missing_check_leaves_an_archive_that_changed_after_it_was_probed(files, tmp_path, monkeypatch):
    book = import_book(files, tmp_path, "moving.cbz")
    path = files.root.resolve(book.content.relative_path)
    service = Removals(files)
    probe = service._still

    def moved_meanwhile(*args):
        path.rename(path.with_suffix(".away"))
        return probe(*args)

    monkeypatch.setattr(service, "_still", moved_meanwhile)
    assert service.check_page()["checked"] == 1
    assert files.library.get_book(book.id).availability == "available"
