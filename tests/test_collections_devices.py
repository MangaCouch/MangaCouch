from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, PASSCODE, content, login, make_zip, register_book

from mangacouch.auth import Auth, Unauthorized
from mangacouch.collections import Collections
from mangacouch.database import Library, RevisionConflict
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState
from mangacouch.snapshots import create_snapshot, restore_snapshot


def populated(files: FileLibrary, count: int = 3):
    service = Collections(files.library)
    collection = service.create("  周末阅读  ")
    ids = [register_book(files.library, content(str(i), f"{i}.zip"))[0].id for i in range(count)]
    for identifier in ids:
        collection = service.edit_item(collection["id"], identifier, collection["revision"])
    return service, collection, ids


def test_collection_order_uses_revisions_and_duplicate_add_is_noop(files: FileLibrary):
    service, collection, ids = populated(files)
    cid, revision = collection["id"], collection["revision"]
    assert collection["name"] == "周末阅读" and collection["count"] == 3
    assert service.edit_item(cid, ids[0], revision)["revision"] == revision
    with pytest.raises(RevisionConflict):
        service.rename(cid, "stale", revision - 1)
    moved = service.edit_item(cid, ids[2], revision, action="move", before_id=ids[0])
    assert [item["book_id"] for item in moved["items"]] == [ids[2], ids[0], ids[1]]
    moved = service.edit_item(cid, ids[2], moved["revision"], action="move")
    assert [item["book_id"] for item in moved["items"]] == ids
    assert service.rename(cid, "Updated", moved["revision"])["name"] == "Updated"
    with pytest.raises(RevisionConflict):
        service.delete(cid, revision)


def test_failed_collection_reorder_rolls_back_removed_rows_and_revision(files: FileLibrary):
    service, collection, ids = populated(files)
    with files.library.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_collection BEFORE INSERT ON collection_item "
            "WHEN NEW.position=1 BEGIN SELECT RAISE(ABORT,'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        service.edit_item(collection["id"], ids[0], collection["revision"], action="move")
    assert service.get(collection["id"]) == collection


@pytest.mark.parametrize("action,before", [("add", None), ("remove", None), ("move", "missing")])
def test_invalid_collection_member_never_changes_order(files: FileLibrary, action, before):
    service, collection, ids = populated(files)
    with pytest.raises(KeyError):
        service.edit_item(
            collection["id"],
            ids[0] if action == "move" else "missing",
            collection["revision"],
            action=action,
            before_id=before,
        )
    assert service.get(collection["id"]) == collection


def test_collection_context_continues_first_unfinished_without_copying_progress(files: FileLibrary):
    service, collection, ids = populated(files)
    cid = collection["id"]
    assert service.context(cid)["continue_id"] == ids[0]
    files.library.save_personal(
        ids[0], PersonalState(completed=True, resume_page=10, rating=4), expected_revision=1
    )
    context = service.context(cid, ids[1])
    assert context["continue_id"] == ids[1] and context["index"] == 1
    assert context["previous_id"] == ids[0] and context["next_id"] == ids[2]
    assert files.library.get_book(ids[1]).personal == PersonalState()
    for identifier in ids[1:]:
        files.library.save_personal(
            identifier, PersonalState(completed=True, resume_page=29), expected_revision=1
        )
    context = service.context(cid)
    assert context["all_completed"] and context["continue_id"] == ids[0]
    assert files.library.get_book(ids[0]).personal.resume_page == 10
    with pytest.raises(KeyError):
        service.context(cid, "missing")


def test_collection_removal_preserves_original_book_and_personal_state(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "original.zip")
    book = files.import_file(source).book
    original = source.read_bytes()
    book = files.library.save_personal(book.id, PersonalState(rating=4, bookmarks=[0]), expected_revision=1)
    service = Collections(files.library)
    collection = service.create("Safe removal")
    collection = service.edit_item(collection["id"], book.id, collection["revision"])
    collection = service.edit_item(collection["id"], book.id, collection["revision"], action="remove")
    assert service.context(collection["id"])["continue_id"] is None
    collection = service.edit_item(collection["id"], book.id, collection["revision"])
    service.delete(collection["id"], collection["revision"])
    assert files.library.get_book(book.id) == book
    assert (files.root.path / book.content.relative_path).read_bytes() == original


def test_large_collection_pages_and_stale_cursor(files: FileLibrary):
    service = Collections(files.library)
    collection = service.create("1005 books")
    # Bulk seed to exercise pagination without a quadratic setup loop.
    ids = [register_book(files.library, content(str(i), f"{i}.zip"))[0].id for i in range(1005)]
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO collection_item VALUES(?,?,?)",
            ((collection["id"], item, i) for i, item in enumerate(ids)),
        )
    cursor, actual = -1, []
    while cursor is not None:
        page = service.get(collection["id"], limit=200, after_position=cursor, expected_revision=1)
        assert page["count"] == 1005
        actual.extend(item["book_id"] for item in page["items"])
        cursor = page["after_position"]
    assert actual == ids
    service.edit_item(collection["id"], ids[-1], 1, action="move", before_id=ids[0])
    assert service.context(collection["id"])["continue_id"] == ids[-1]
    with pytest.raises(RevisionConflict):
        service.get(collection["id"], after_position=199, expected_revision=1)


def test_snapshot_roundtrip_retains_collection_order_and_shared_progress(files: FileLibrary, tmp_path: Path):
    service, collection, ids = populated(files)
    collection = service.edit_item(collection["id"], ids[0], collection["revision"], action="move")
    files.library.save_personal(ids[1], PersonalState(completed=True), expected_revision=1)
    restored = restore_snapshot(create_snapshot(files, tmp_path / "snapshot.zip"), tmp_path / "restored")
    assert Collections(restored.library).get(collection["id"]) == collection
    assert Collections(restored.library).context(collection["id"])["continue_id"] == ids[2]


@pytest.mark.parametrize("version", [4, 5, 6, 7])
def test_previous_development_schema_is_refused_without_modification(tmp_path: Path, version: int):
    path = tmp_path / "old.sqlite"
    library = Library.initialize(path)
    with library.connection() as db:
        db.execute(f"PRAGMA user_version={version}")
    with pytest.raises(ValueError, match="supported MangaCouch database"):
        Library(path)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == version


def test_collection_http_crud_revision_errors_and_readonly_boundary(
    client: TestClient, files: FileLibrary, auth: Auth
):
    _, _, ids = populated(files)
    assert client.get("/api/collections").status_code == 401
    login(client)
    assert client.post("/api/collections", json={"name": "Series"}).status_code == 403
    response = client.post("/api/collections", json={"name": "Series"}, headers=CSRF)
    assert response.status_code == 200
    cid = response.json()["id"]
    base = f"/api/collections/{cid}"
    for index, identifier in enumerate(ids):
        assert (
            client.post(
                base + "/items", json={"book_id": identifier, "expected_revision": index + 1}, headers=CSRF
            ).status_code
            == 200
        )
    page = client.get(base + "?limit=2").json()
    assert page["count"] == 3 and page["items"][0]["book"]["id"] == ids[0]
    assert client.get(base + f"/context?book_id={ids[1]}").json()["next_id"] == ids[2]
    assert (
        client.put(
            base + f"/items/{ids[2]}", json={"expected_revision": 4, "before_id": ids[0]}, headers=CSRF
        ).status_code
        == 200
    )
    assert client.get(base + "?after_position=1&expected_revision=4").status_code == 409
    assert client.put(base, json={"expected_revision": 5, "name": "Renamed"}, headers=CSRF).status_code == 200
    assert client.delete(base + f"/items/{ids[0]}?expected_revision=6", headers=CSRF).status_code == 200
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get(base, headers=headers).status_code == 200
    for method, route in [
        ("POST", "/api/collections"),
        ("PUT", base),
        ("DELETE", base),
        ("POST", base + "/items"),
        ("PUT", base + f"/items/{ids[0]}"),
        ("DELETE", base + f"/items/{ids[0]}"),
    ]:
        assert client.request(method, route, headers=headers).status_code == 403
    assert client.delete(base + "?expected_revision=7", headers=CSRF).status_code == 200
    assert client.get(base).status_code == 404


def test_device_revocation_keeps_api_keys(client: TestClient, auth: Auth):
    current = login(client)
    other_id, other_token = auth.login(PASSCODE, "Other browser")
    key_id, key = auth.create_key("management", "Automation")
    labels = {row["id"]: row["label"] for row in auth.list_credentials()}
    assert (
        labels[current] == "fixture device"
        and labels[other_id] == "Other browser"
        and labels[key_id] == "Automation"
    )
    assert client.delete(f"/api/auth/credentials/{other_id}", headers=CSRF).status_code == 200
    with pytest.raises(Unauthorized):
        auth.authenticate(other_token, session=True)
    assert auth.authenticate(key, session=False).kind == "management"
    assert client.get("/api/auth/me").json()["id"] == current


def test_dynamic_collection_rejects_changed_page_results(files, tmp_path):
    from mangacouch.collections import Collections
    from mangacouch.database import RevisionConflict

    files.import_file(make_zip(tmp_path / "one.cbz"))
    collections = Collections(files.library)
    collection = collections.create("all", {"query": ""})
    page = collections.get(collection["id"], limit=1)
    files.import_file(make_zip(tmp_path / "two.cbz", ["different.png"]))
    with pytest.raises(RevisionConflict, match="results changed"):
        collections.get(collection["id"], after_position=0, expected_result=page["result_token"])


def test_dynamic_collection_freezes_membership_and_prefers_unread(files):
    one, _ = register_book(files.library, content("ui-one"), Metadata(language="english"))
    two, _ = register_book(files.library, content("ui-two"), Metadata(language="english"))
    service = Collections(files.library)
    group = service.create("English", {"query": "", "options": {"language": "English"}})
    assert group["kind"] == "dynamic" and group["count"] == 2
    files.library.save_personal(one.id, PersonalState(resume_page=0), expected_revision=1)
    assert service.context(group["id"])["continue_id"] == two.id
    frozen = service.freeze(group["id"], group["revision"])
    register_book(files.library, content("ui-three"), Metadata(language="english"))
    assert frozen["kind"] == "static" and service.get(group["id"])["count"] == 2
    with pytest.raises(RevisionConflict):
        service.freeze(group["id"], group["revision"])
