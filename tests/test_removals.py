from __future__ import annotations

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, book, login, make_zip, manager_client, pending_sidecars

from mangacouch import snapshots
from mangacouch.auth import Auth
from mangacouch.collections import Collections
from mangacouch.errors import InvalidInput, NotFound, RevisionConflict
from mangacouch.eze import eze_path
from mangacouch.files import ROOT_MARKER, RootUnavailable
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState
from mangacouch.page_features import PageFeatures
from mangacouch.removals import Removals
from mangacouch.review import Review
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync


def submit(service: Removals, preview: dict, *, delete_file: bool = False):
    return service.remove(
        preview["id"], preview["book_id"], preview["token"], confirmed=True, delete_file=delete_file
    )


def submit_selected(service: Removals, plan: dict, *, delete_file: bool = False):
    return service.remove_selected(
        plan["id"], plan["book_ids"], plan["token"], confirmed=True, delete_file=delete_file
    )


def dump(files: FileLibrary, book_id: str) -> dict[str, list[tuple]]:
    """Every row that references the book, keyed by table (book_tag is derived)."""
    tables = {}
    with files.library.connection() as db:
        names = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in names:
            columns = [
                row["from"]
                for row in db.execute(f"PRAGMA foreign_key_list({table})")
                if row["table"] == "book"
            ]
            if table == "book":
                columns = ["id"]
            if not columns:
                continue
            condition = " OR ".join(f"{column}=?" for column in columns)
            rows = db.execute(
                f"SELECT * FROM {table} WHERE {condition} ORDER BY 1,2", [book_id] * len(columns)
            ).fetchall()
            if rows:
                tables[table] = sorted(tuple(row) for row in rows)
    return tables


def test_preview_is_read_only_and_lists_impact(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    files.library.save_personal(item.id, PersonalState(rating=4.5, bookmarks=[0]), expected_revision=1)
    collections = Collections(files.library)
    collection = collections.create("Reading order")
    collections.edit_item(collection["id"], item.id, collection["revision"])
    service = Removals(files)
    preview = service.preview(item.id)
    assert preview["presence"] == "present" and preview["unsynced_metadata"]
    assert preview["personal"]["rating"] == 4.5 and len(preview["collections"]) == 1
    assert service.list()["items"] == []
    with pytest.raises(InvalidInput, match="confirmation"):
        service.remove(preview["id"], item.id, preview["token"], confirmed=False)
    assert files.library.get_book(item.id).id == item.id


def test_restore_reinserts_every_referencing_row_and_collection_position(
    files: FileLibrary, auth: Auth, tmp_path: Path
):
    first, item, last = [book(files, tmp_path, name) for name in ["first", "target", "last"]]
    sync = SidecarSync(files)
    sync.sync_one(item.id)
    archive, sidecar = files.root.resolve(item.content.relative_path), sync.path(item)
    before = archive.read_bytes(), sidecar.read_bytes()
    collections = Collections(files.library)
    collection = collections.create("Keep this order")
    for member in [first, item, last]:
        collection = collections.edit_item(collection["id"], member.id, collection["revision"])
    edited = Review(files).edit(item.id, Metadata(title="Unsynced title"), expected_revision=1)
    state = PersonalState(rating=5, completed=True, resume_page=0, bookmarks=[0])
    files.library.save_personal(item.id, state, expected_revision=1)
    credential, _ = auth.create_key("readonly", "client")
    now = int(time.time())
    with files.library.transaction() as db:
        db.execute("INSERT INTO duplicate_decision VALUES(?,?)", (first.id, item.id))
        db.execute("INSERT INTO duplicate_edge VALUES(?,?,0,0)", (item.id, last.id))
        feature = db.execute(
            "INSERT INTO page_feature(book_id,page,digest,dhash,gray,aspect) VALUES(?,0,'d','h',?,1.5)",
            (item.id, b"\x00\x01binary"),
        ).lastrowid
        db.execute("INSERT INTO page_band VALUES(1,2,?)", (feature,))
        db.execute("INSERT INTO import_receipt VALUES('import/target.cbz','{}',?)", (item.id,))
        db.execute("INSERT INTO client_observation VALUES(?,?,3)", (credential, item.id))
        db.execute("INSERT INTO source_observation VALUES(?,'https://example.test/g/1','{}')", (item.id,))
        db.execute(
            "INSERT OR REPLACE INTO source_check(book_id,state,updated_at) VALUES(?,'done',?)", (item.id, now)
        )
        db.execute("INSERT INTO eze_cache(book_id,payload_json) VALUES(?,'{}')", (item.id,))
        db.execute(
            "INSERT INTO download_job(id,input_url,source_json,state,book_id,created_at,updated_at) "
            "VALUES('job','https://example.test/g/1','{}','done',?,?,?)",
            (item.id, now, now),
        )
    saved = dump(files, item.id)
    assert {"duplicate_decision", "duplicate_edge", "page_feature", "client_observation"} <= set(saved)
    service = Removals(files)
    preview = service.preview(item.id)
    receipt = submit(service, preview)
    assert receipt["files_kept"] and service.get(preview["id"]) == receipt
    assert receipt["items"] == [
        {
            "book_id": item.id,
            "title": "Unsynced title",
            "filename": "target.zip",
            "file": "kept",
            "restored": False,
        }
    ]
    assert archive.read_bytes() == before[0] and sidecar.read_bytes() == before[1]
    with pytest.raises(NotFound):
        files.library.get_book(item.id)
    assert dump(files, item.id) == {}
    assert collections.get(collection["id"])["items"] == [
        {"book_id": first.id, "position": 0},
        {"book_id": last.id, "position": 1},
    ]
    with files.library.connection() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("SELECT book_id FROM download_job").fetchone()[0] is None
        assert not db.execute("SELECT 1 FROM page_band").fetchone()
    assert service.restore(receipt["id"])["restored"]
    assert dump(files, item.id) == saved
    recovered = files.library.get_book(item.id)
    assert recovered.metadata == edited.metadata and recovered.personal == state
    assert (item.id, edited.metadata_revision) in pending_sidecars(files.library)
    assert [entry["book_id"] for entry in collections.get(collection["id"])["items"]] == [
        first.id,
        item.id,
        last.id,
    ]
    with files.library.connection() as db:
        assert db.execute("SELECT gray FROM page_feature").fetchone()[0] == b"\x00\x01binary"
        assert db.execute("SELECT count(*) FROM page_band").fetchone()[0] == 1
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    assert files.library.search("Unsynced") == [item.id]


def test_tables_added_later_are_captured_through_foreign_keys(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    with files.library.transaction() as db:
        db.execute(
            "CREATE TABLE future_feature("
            "book_id TEXT NOT NULL REFERENCES book(id) ON DELETE CASCADE, note TEXT)"
        )
        db.execute("INSERT INTO future_feature VALUES(?,'kept')", (item.id,))
    service = Removals(files)
    receipt = submit(service, service.preview(item.id))
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM future_feature").fetchone()[0] == 0
    service.restore(receipt["id"])
    with files.library.connection() as db:
        assert [tuple(row) for row in db.execute("SELECT book_id,note FROM future_feature")] == [
            (item.id, "kept")
        ]


def test_restore_remaps_reused_event_ids_and_requeues_running_checks(files: FileLibrary, tmp_path: Path):
    other, item = book(files, tmp_path, "other"), book(files, tmp_path, "target")
    with files.library.transaction() as db:
        first = db.execute(
            "INSERT INTO review_event(book_id,metadata_revision,action,details_json) "
            "VALUES(?,1,'manual_confirm','{}')",
            (item.id,),
        ).lastrowid
        db.execute(
            "INSERT INTO review_event(book_id,metadata_revision,action,details_json) "
            "VALUES(?,1,'automatic',?)",
            (item.id, json.dumps({"evidence_event_id": first})),
        )
        db.execute(
            "INSERT OR REPLACE INTO source_check(book_id,state,generation,updated_at) "
            "VALUES(?,'running',4,0)",
            (item.id,),
        )
    service = Removals(files)
    receipt = submit(service, service.preview(item.id))
    with files.library.transaction() as db:
        reused = db.execute(
            "INSERT INTO review_event(book_id,metadata_revision,action,details_json) VALUES(?,1,'edit','{}')",
            (other.id,),
        ).lastrowid
    assert reused == first
    service.restore(receipt["id"])
    with files.library.connection() as db:
        events = db.execute(
            "SELECT id,action,details_json FROM review_event WHERE book_id=? ORDER BY id", (item.id,)
        ).fetchall()
        check = db.execute("SELECT state,generation FROM source_check WHERE book_id=?", (item.id,)).fetchone()
    confirm = next(row for row in events if row["action"] == "manual_confirm")
    automatic = next(row for row in events if row["action"] == "automatic")
    assert (
        confirm["id"] != first and json.loads(automatic["details_json"])["evidence_event_id"] == confirm["id"]
    )
    assert tuple(check) == ("queued", 5)


@pytest.mark.parametrize("selected", [False, True])
def test_background_indexing_does_not_invalidate_removal_preview(files, tmp_path, selected):
    items = [book(files, tmp_path, name) for name in ["first", "second"]]
    service = Removals(files)
    preview = (
        service.preview_selected([item.id for item in items]) if selected else service.preview(items[0].id)
    )
    assert PageFeatures(files).index(items[0].id, limit=1)["processed"] == 1
    receipt = submit_selected(service, preview) if selected else submit(service, preview)
    service.restore(receipt["id"])
    with files.library.connection() as db:
        assert db.execute("SELECT indexed_pages FROM book WHERE id=?", (items[0].id,)).fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM page_feature WHERE book_id=?", (items[0].id,)).fetchone()[0] == 1
        )


@pytest.mark.parametrize("change", ["metadata", "personal", "collection", "file", "review"])
def test_stale_preview_never_removes_current_record(files: FileLibrary, tmp_path: Path, change: str):
    item = book(files, tmp_path)
    service = Removals(files)
    preview = service.preview(item.id)
    if change == "metadata":
        Review(files).edit(item.id, Metadata(title="New"), expected_revision=1)
    elif change == "personal":
        files.library.save_personal(item.id, PersonalState(rating=4), expected_revision=1)
    elif change == "collection":
        collections = Collections(files.library)
        collection = collections.create("new collection")
        collections.edit_item(collection["id"], item.id, collection["revision"])
    elif change == "file":
        path = files.root.resolve(item.content.relative_path)
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
    else:
        review = Review(files)
        review.automatic(item.id, review.preview(item.id)["token"])
    with pytest.raises(RevisionConflict):
        submit(service, preview)
    assert files.library.get_book(item.id).id == item.id and service.list()["items"] == []


def test_missing_record_cleanup_rechecks_file_return_and_root_identity(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    path = files.root.resolve(item.content.relative_path)
    data = path.read_bytes()
    path.unlink()
    service = Removals(files)
    preview = service.preview(item.id)
    assert preview["presence"] == "missing"
    path.write_bytes(data)
    with pytest.raises(RevisionConflict):
        submit(service, preview)
    preview = service.preview(item.id)
    marker = files.root.path / ROOT_MARKER
    saved = marker.read_bytes()
    marker.unlink()
    with pytest.raises(RootUnavailable):
        submit(service, preview)
    marker.write_bytes(saved)
    path.unlink()
    receipt = submit(service, service.preview(item.id))
    assert receipt["files_kept"] and not path.exists()
    assert not service.list()["items"][0]["can_restore"]
    with pytest.raises(RevisionConflict):
        service.restore(receipt["id"])
    path.write_bytes(data)
    assert service.restore(receipt["id"])["restored"]
    assert files.library.get_book(item.id).id == item.id


def test_removal_takes_no_full_snapshot_and_creates_no_backup_files(
    files: FileLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    item = book(files, tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("removal must not snapshot the whole database")

    monkeypatch.setattr(snapshots, "create_snapshot", forbidden)
    before = set(files.library.path.parent.iterdir())
    service = Removals(files)
    assert submit(service, service.preview(item.id))["state"] == "removed"
    assert set(files.library.path.parent.iterdir()) == before


def test_deletion_transaction_failure_rolls_back_and_retry_succeeds(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    service = Removals(files)
    preview = service.preview(item.id)
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_receipt BEFORE INSERT ON removal BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        submit(service, preview)
    assert files.library.get_book(item.id).id == item.id and service.list()["items"] == []
    with files.library.transaction() as db:
        db.execute("DROP TRIGGER reject_receipt")
    assert submit(service, preview)["state"] == "removed"


def test_lost_response_retry_returns_same_receipt_even_if_root_is_offline(
    files: FileLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    item = book(files, tmp_path)
    service = Removals(files)
    preview = service.preview(item.id)
    original = service.get

    def lost(identifier):
        raise OSError("response lost")

    monkeypatch.setattr(service, "get", lost)
    with pytest.raises(OSError):
        submit(service, preview)
    monkeypatch.setattr(service, "get", original)
    (files.root.path / ROOT_MARKER).unlink()
    receipt = submit(service, preview)
    assert service.get(receipt["id"]) == receipt and len(service.list()["items"]) == 1


def test_concurrent_retries_remove_once(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    service = Removals(files)
    preview = service.preview(item.id)

    def attempt():
        try:
            return submit(service, preview)
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: attempt(), range(3)))
    assert any(results)
    receipt = submit(service, preview)
    assert all(result is None or result == receipt for result in results)
    assert len(service.list()["items"]) == 1


def test_scan_and_pending_work_block_removal(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    service = Removals(files)
    inbox = files.root.path / "import"
    inbox.mkdir()
    (inbox / "target.cbz").write_bytes(files.root.resolve(item.content.relative_path).read_bytes())
    scanner = Scanner(files)
    run = scanner.start(keep_source=True)["id"]
    preview = service.preview(item.id)
    assert "scan_active" in preview["blocked"]
    with pytest.raises(InvalidInput):
        submit(service, preview)
    scanner.pause(run)
    preview = service.preview(item.id)
    with (
        files.root.mutation(lock_name=".mangacouch-scan.lock"),
        pytest.raises(RevisionConflict, match="scan batch"),
    ):
        submit(service, preview)
    with files.library.transaction() as db:
        db.execute("UPDATE import_job SET state='ready' WHERE book_id=?", (item.id,))
    assert "import_recovery_pending" in service.preview(item.id)["blocked"]


def test_reimported_kept_archive_has_fresh_identity_and_blocks_restore(files: FileLibrary, tmp_path: Path):
    item = book(files, tmp_path)
    SidecarSync(files).sync_one(item.id)
    files.library.save_personal(item.id, PersonalState(rating=4.5, completed=True), expected_revision=1)
    service = Removals(files)
    receipt = submit(service, service.preview(item.id))
    new = files.import_file(files.root.resolve(item.content.relative_path), index_existing=True).book
    assert new.id != item.id and new.content.sha256 == item.content.sha256
    assert new.personal == PersonalState()
    with pytest.raises(RevisionConflict, match="already"):
        service.restore(receipt["id"])


def test_receipts_are_bound_to_their_request(files: FileLibrary, tmp_path: Path):
    first, second = book(files, tmp_path), book(files, tmp_path, "second")
    service = Removals(files)
    preview = service.preview(first.id)
    with pytest.raises(RevisionConflict):
        service.remove(preview["id"], second.id, preview["token"], confirmed=True)
    submit(service, preview)
    with pytest.raises(RevisionConflict):
        service.remove(preview["id"], second.id, preview["token"], confirmed=True)
    with pytest.raises(RevisionConflict):
        submit(service, preview, delete_file=True)
    with pytest.raises(InvalidInput):
        service.get("../../outside")


def test_removal_api_management_csrf_history_and_read_only_list(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    item = book(files, tmp_path)
    _, key = auth.create_key("readonly", "test readonly")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/removals", headers=headers).status_code == 403
    login(client)
    assert client.post("/api/removals/preview", json={"book_id": item.id}).status_code == 403
    preview = client.post("/api/removals/preview", json={"book_id": item.id}, headers=CSRF).json()
    body = {key: preview[key] for key in ["id", "book_id", "token"]}
    assert client.post("/api/removals", json={**body, "confirmed": False}, headers=CSRF).status_code == 422
    response = client.post("/api/removals", json={**body, "confirmed": True}, headers=CSRF)
    assert response.status_code == 200
    receipt = response.json()
    assert receipt["kind"] == "single" and receipt["delete_requested"] is False
    assert receipt["files_kept"] and not receipt["delete_pending"] and receipt["deletion_stopped"] is None
    (files.root.path / ROOT_MARKER).unlink()
    assert client.get("/api/removals").json()["items"] == [{**receipt, "can_restore": False}]
    assert "snapshot_url" not in receipt
    assert client.post("/api/removals", json={**body, "confirmed": True}, headers=CSRF).json() == receipt
    with files.library.transaction() as db:
        db.execute("UPDATE removal SET created_at=?", (int(time.time()) - 8 * 86400,))
    assert len(client.get("/api/removals").json()["items"]) == 1
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM removal").fetchone()[0] == 1


def test_selected_removal_restores_collections_over_http(files, tmp_path):
    books = [files.import_file(make_zip(tmp_path / f"{n}.cbz", [f"{n}.png"])).book for n in range(4)]
    collections = Collections(files.library)
    audit = collections.create("Audit")
    for item in books:
        audit = collections.edit_item(audit["id"], item.id, audit["revision"])
    removed = [books[1], books[3]]
    with manager_client(files) as client:
        data = client.post(
            "/api/removals/selected/preview", json={"book_ids": [b.id for b in removed]}
        ).json()
        response = client.post(
            "/api/removals/selected",
            json={"id": data["id"], "book_ids": data["book_ids"], "token": data["token"], "confirmed": True},
        )
        assert response.status_code == 200, response.text
        assert response.json()["kind"] == "selected" and len(response.json()["items"]) == 2
        assert set(files.library.search("")) == {books[0].id, books[2].id}
        assert client.post(f"/api/removals/{data['id']}/restore").status_code == 200
        assert set(files.library.search("")) == {b.id for b in books}
        assert [item["book_id"] for item in collections.get(audit["id"])["items"]] == [b.id for b in books]
        assert Removals(files).get(data["id"])["restored"]


def test_restore_position_falls_back_to_the_end_of_a_shorter_collection(files, tmp_path):
    books = [book(files, tmp_path, name) for name in "abc"]
    collections = Collections(files.library)
    shelf = collections.create("Shelf")
    for item in books:
        shelf = collections.edit_item(shelf["id"], item.id, shelf["revision"])
    service = Removals(files)
    receipt = submit(service, service.preview(books[2].id))
    shelf = collections.get(shelf["id"])
    collections.edit_item(shelf["id"], books[0].id, shelf["revision"], action="remove")
    service.restore(receipt["id"])
    assert [item["book_id"] for item in collections.get(shelf["id"])["items"]] == [books[1].id, books[2].id]


def test_recycle_restore_preserves_personal_and_is_idempotent(files, tmp_path):
    item = files.import_file(make_zip(tmp_path / "restore.cbz")).book
    item = files.library.save_personal(item.id, PersonalState(rating=4.5, resume_page=1), expected_revision=1)
    service = Removals(files)
    preview = service.preview(item.id)
    receipt = service.remove(preview["id"], item.id, preview["token"], confirmed=True)
    assert service.list()["items"][0]["can_restore"]
    restored = service.restore(receipt["id"])
    assert restored["restored"] and restored["items"][0]["restored"]
    assert files.library.get_book(item.id).personal == item.personal
    assert service.restore(receipt["id"]) == restored
    assert not service.list()["items"][0]["can_restore"]


def test_delete_files_also_deletes_both_sidecars(files, tmp_path):
    item = files.import_file(make_zip(tmp_path / "delete.cbz")).book
    archive = files.root.resolve(item.content.relative_path)
    SidecarSync(files).sync_one(item.id)
    native, eze = SidecarSync(files).path(item), eze_path(archive)
    eze.write_text("{}")
    assert native.exists() and archive.exists()
    service = Removals(files)
    receipt = submit(service, service.preview(item.id), delete_file=True)
    assert not archive.exists() and not native.exists() and not eze.exists()
    assert receipt["delete_requested"] and not receipt["files_kept"] and not receipt["delete_pending"]
    assert receipt["items"][0]["file"] == "deleted" and not service.list()["items"][0]["can_restore"]


def test_file_delete_failure_retains_durable_receipt_and_retry_finishes(files, tmp_path, monkeypatch):
    item = files.import_file(make_zip(tmp_path / "delete.cbz")).book
    original = files.root.resolve(item.content.relative_path)
    service = Removals(files)
    preview = service.preview(item.id)
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == original:
            raise PermissionError("fixture")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError):
        submit(service, preview, delete_file=True)
    listed = service.list()["items"][0]
    assert (
        original.exists() and listed["delete_pending"] and listed["files_kept"] and not listed["can_restore"]
    )
    monkeypatch.setattr(Path, "unlink", unlink)
    result = submit(service, preview, delete_file=True)
    assert not original.exists() and not result["files_kept"] and not result["delete_pending"]


def test_batch_deletion_checks_every_file_before_unlinking_any(files, tmp_path):
    one, two = book(files, tmp_path, "one"), book(files, tmp_path, "two")
    first, second = (files.root.resolve(item.content.relative_path) for item in (one, two))
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    service._remove(
        plan["id"], "selected", plan["book_ids"], plan["token"], confirmed=True, delete_file=False
    )
    # Simulate a removal that asked for deletion but whose second file changed before the unlink.
    with files.library.transaction() as db:
        receipt = json.loads(db.execute("SELECT receipt_json FROM removal").fetchone()[0])
        receipt.update(delete_requested=True, delete_pending=True)
        db.execute("UPDATE removal SET receipt_json=?", (json.dumps(receipt),))
    paths = {one.id: first, two.id: second}
    paths[receipt["items"][1]["book_id"]].write_bytes(b"edited elsewhere")
    service.finish_deletion(plan["id"])
    result = service.get(plan["id"])
    assert first.exists() and second.exists()
    assert (
        result["files_kept"] and result["deletion_stopped"] == "file_changed" and not result["delete_pending"]
    )
    assert [item["file"] for item in result["items"]] == ["kept", "kept"]


def test_partial_deletion_receipt_is_truthful_and_remaining_book_restores(files, tmp_path, monkeypatch):
    one, two = book(files, tmp_path, "one"), book(files, tmp_path, "two")
    paths = {item.id: files.root.resolve(item.content.relative_path) for item in (one, two)}
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    blocked = paths[plan["book_ids"][1]]
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("fixture")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    with pytest.raises(PermissionError):
        submit_selected(service, plan, delete_file=True)
    monkeypatch.setattr(Path, "unlink", unlink)
    partial = service.get(plan["id"])
    assert not partial["files_kept"] and partial["delete_pending"]
    assert [item["file"] for item in partial["items"]] == ["deleted", "kept"]
    cancelled = service.cancel_deletion(plan["id"])
    assert cancelled["deletion_stopped"] == "cancelled" and not cancelled["delete_pending"]
    assert not cancelled["files_kept"] and service.list()["items"][0]["can_restore"]
    restored = service.restore(plan["id"])
    assert [item["restored"] for item in restored["items"]] == [False, True] and restored["restored"]
    assert files.library.search("") == [plan["book_ids"][1]] and blocked.exists()


def test_restore_can_select_individual_books(files, tmp_path):
    one, two = book(files, tmp_path, "one"), book(files, tmp_path, "two")
    service = Removals(files)
    plan = service.preview_selected([one.id, two.id])
    submit_selected(service, plan)
    partial = service.restore(plan["id"], [two.id])
    assert not partial["restored"] and files.library.search("") == [two.id]
    assert service.restore(plan["id"])["restored"]
    assert set(files.library.search("")) == {one.id, two.id}
    with pytest.raises(InvalidInput):
        service.restore(plan["id"], ["unknown"])


def test_purge_after_seven_days_keeps_archive_and_pending_deletions(files, tmp_path):
    item = files.import_file(make_zip(tmp_path / "purge.cbz")).book
    service = Removals(files)
    preview = service.preview(item.id)
    service.remove(preview["id"], item.id, preview["token"], confirmed=True)
    with files.library.transaction() as db:
        db.execute("UPDATE removal SET created_at=?", (int(time.time()) - 8 * 86400,))
    assert not service.list()["items"][0]["can_restore"]
    with pytest.raises(NotFound):
        service.restore(preview["id"])
    assert service.purge() == 1 and service.list()["items"] == []
    assert files.root.resolve(item.content.relative_path).exists()
