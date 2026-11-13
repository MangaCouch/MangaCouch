from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    content,
    login,
    make_zip,
    register_book,
    register_books,
)

from mangacouch.auth import Auth
from mangacouch.collections import Collections
from mangacouch.cursors import encode_cursor
from mangacouch.database import RevisionConflict
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState


def populated(files: FileLibrary, count: int = 3):
    service = Collections(files.library)
    collection = service.create("  周末阅读  ")
    ids = [
        book.id for book in register_books(files.library, (content(str(i), f"{i}.zip") for i in range(count)))
    ]
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


def test_collection_continues_started_book_ahead_of_unread_one(files: FileLibrary):
    service, collection, ids = populated(files, 4)
    cid = collection["id"]
    files.library.save_personal(ids[0], PersonalState(completed=True), expected_revision=1)
    files.library.save_personal(ids[2], PersonalState(resume_page=3), expected_revision=1)
    context = service.context(cid)
    assert (context["continue_id"], context["continue_state"]) == (ids[1], "unread")
    service.edit_item(cid, ids[2], service.get(cid)["revision"], action="move", before_id=ids[1])
    context = service.context(cid)
    assert (context["continue_id"], context["continue_state"]) == (ids[2], "reading")
    assert not context["all_completed"]


def test_collection_removal_preserves_original_book_and_personal_state(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "original.zip")
    book = files.import_file(source).book
    original = source.read_bytes()
    book = files.library.save_personal(book.id, PersonalState(rating=4), expected_revision=1)
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
    ids = [
        book.id for book in register_books(files.library, (content(str(i), f"{i}.zip") for i in range(1005)))
    ]
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO collection_item VALUES(?,?,?)",
            ((collection["id"], item, i) for i, item in enumerate(ids)),
        )
    cursor, actual = "", []
    while cursor is not None:
        page = service.get(collection["id"], limit=200, after=cursor, expected_revision=1)
        assert page["count"] == 1005
        actual.extend(item["book_id"] for item in page["items"])
        cursor = page["after"]
    assert actual == ids
    service.edit_item(collection["id"], ids[-1], 1, action="move", before_id=ids[0])
    assert service.context(collection["id"])["continue_id"] == ids[-1]
    with pytest.raises(RevisionConflict):
        service.get(collection["id"], after=encode_cursor(199), expected_revision=1)


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
                base + "/items",
                params={"expected_revision": index + 1},
                json={"book_id": identifier},
                headers=CSRF,
            ).status_code
            == 200
        )
    page = client.get(base + "?limit=2").json()
    assert page["count"] == 3 and page["items"][0]["book"]["id"] == ids[0]
    assert client.get(base + f"/context?book_id={ids[1]}").json()["next_id"] == ids[2]
    assert (
        client.put(
            base + f"/items/{ids[2]}",
            params={"expected_revision": 4},
            json={"before_id": ids[0]},
            headers=CSRF,
        ).status_code
        == 200
    )
    assert client.get(base, params={"after": page["after"], "expected_revision": 4}).status_code == 409
    renamed = client.put(base, params={"expected_revision": 5}, json={"name": "Renamed"}, headers=CSRF)
    assert renamed.status_code == 200
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
    deleted = client.delete(base + "?expected_revision=7", headers=CSRF)
    assert deleted.status_code == 204 and deleted.content == b""
    assert client.get(base).status_code == 404


def test_dynamic_collection_rejects_changed_page_results(files, tmp_path):
    from mangacouch.collections import Collections
    from mangacouch.database import RevisionConflict

    files.import_file(make_zip(tmp_path / "one.cbz"))
    collections = Collections(files.library)
    collection = collections.create("all", {"query": ""})
    page = collections.get(collection["id"], limit=1)
    files.import_file(make_zip(tmp_path / "two.cbz", ["different.png"]))
    with pytest.raises(RevisionConflict, match="results changed"):
        collections.get(collection["id"], after=page["after"], expected_result=page["result_token"])


def following_pages(service: Collections, page: dict, limit: int) -> tuple[list[dict], list[dict]]:
    identifier = page["id"]
    pages, items = [page], list(page["items"])
    while page["after"] is not None:
        page = service.get(
            identifier,
            limit=limit,
            after=page["after"],
            expected_revision=page["revision"],
            expected_result=page["result_token"],
        )
        pages.append(page)
        items.extend(page["items"])
    return pages, items


def test_dynamic_collection_pages_follow_book_order_without_listing_members_again(files, monkeypatch):
    ids = sorted(
        register_book(files.library, content(f"p{i}"), Metadata(title=f"Book {i}"))[0].id for i in range(7)
    )
    register_book(files.library, content("other"), Metadata(title="Other"))
    service = Collections(files.library)
    group = service.create("Books", {"query": "book"})
    first = service.get(group["id"], limit=3)
    monkeypatch.setattr(
        Collections, "_members", staticmethod(lambda *_a, **_k: pytest.fail("members listed again"))
    )
    pages, items = following_pages(service, first, 3)
    assert [len(page["items"]) for page in pages] == [3, 3, 1]
    assert [item["book_id"] for item in items] == ids
    assert [item["position"] for item in items] == list(range(7))
    assert {page["count"] for page in pages} == {7}
    monkeypatch.undo()
    # A bare position (OPDS page links) continues at the same place.
    by_position = service.get(group["id"], limit=2, after=encode_cursor(2))
    assert [item["book_id"] for item in by_position["items"]] == ids[3:5]
    assert [item["position"] for item in by_position["items"]] == [3, 4]


def test_dynamic_collection_pages_survive_unrelated_edits_and_refuse_changed_results(files):
    ids = sorted(
        register_book(files.library, content(f"q{i}"), Metadata(title=f"Book {i}"))[0].id for i in range(4)
    )
    other, _ = register_book(files.library, content("other"), Metadata(title="Other"))
    service = Collections(files.library)
    group = service.create("Books", {"query": "book"})
    page = service.get(group["id"], limit=2)
    # The search revision changes, but the members do not: the next page still follows.
    files.library.save_personal(other.id, PersonalState(rating=3), expected_revision=1)
    files.library.save_personal(ids[0], PersonalState(rating=4), expected_revision=1)
    following = service.get(group["id"], limit=2, after=page["after"], expected_result=page["result_token"])
    assert [item["book_id"] for item in following["items"]] == ids[2:]
    assert following["result_token"] != page["result_token"] and following["count"] == 4
    register_book(files.library, content("late"), Metadata(title="Book late"))
    with pytest.raises(RevisionConflict, match="results changed"):
        service.get(group["id"], limit=2, after=page["after"], expected_result=following["result_token"])
    with pytest.raises(RevisionConflict, match="results changed"):
        service.get(group["id"], limit=2, after=page["after"], expected_result="0" * 64)
    fresh = service.get(group["id"], limit=2)
    assert fresh["count"] == 5


def test_dynamic_collection_freezes_membership_and_continues_first_unfinished(files):
    one, _ = register_book(files.library, content("ui-one"), Metadata(language="english"))
    two, _ = register_book(files.library, content("ui-two"), Metadata(language="english"))
    service = Collections(files.library)
    group = service.create("English", {"query": "", "options": {"language": "English"}})
    assert group["kind"] == "dynamic" and group["count"] == 2
    first, second = (item["book_id"] for item in service.get(group["id"])["items"])
    files.library.save_personal(second, PersonalState(resume_page=0), expected_revision=1)
    context = service.context(group["id"])
    assert (context["continue_id"], context["continue_state"]) == (first, "unread")
    files.library.save_personal(first, PersonalState(resume_page=0), expected_revision=1)
    context = service.context(group["id"])
    assert (context["continue_id"], context["continue_state"]) == (first, "reading")
    frozen = service.freeze(group["id"], group["revision"])
    register_book(files.library, content("ui-three"), Metadata(language="english"))
    assert frozen["kind"] == "static" and service.get(group["id"])["count"] == 2
    with pytest.raises(RevisionConflict):
        service.freeze(group["id"], group["revision"])


@pytest.mark.perf
def test_dynamic_collection_counts_share_one_book_scan(files, monkeypatch):
    register_book(files.library, content("one"), Metadata(title="One"))
    register_book(files.library, content("two"), Metadata(title="Two"))
    collections = Collections(files.library)
    for name, query in (("A", ""), ("B", "One"), ("C", "absent")):
        collections.create(name, {"query": query})
    statements = []
    connect = files.library.connection

    @contextmanager
    def measured():
        with connect() as db:
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith("SELECT") else None)
            yield db

    monkeypatch.setattr(files.library, "connection", measured)
    assert [item["count"] for item in collections.list()["items"]] == [2, 1, 0]
    assert len(statements) == 2


def test_collections_are_ordered_by_name_with_opaque_cursor(files):
    collections = Collections(files.library)
    for name in ("beta", "Alpha", "gamma", "alpha"):
        collections.create(name)
    first = collections.list(limit=2)
    assert [row["name"] for row in first["items"]] in (["Alpha", "alpha"], ["alpha", "Alpha"])
    second = collections.list(limit=2, after=first["after"])
    assert [row["name"] for row in second["items"]] == ["beta", "gamma"]
    assert second["after"] is None
    with pytest.raises(InvalidInput):
        collections.list(after="not-a-cursor")


def test_removal_keeps_positions_and_listing_reports_counts_and_kinds(files):
    service, static, ids = populated(files, 4)
    static = service.edit_item(static["id"], ids[1], static["revision"], action="remove")
    assert [item["position"] for item in static["items"]] == [0, 2, 3]
    context = service.context(static["id"], ids[2])
    assert context["continue_state"] == "unread" and context["previous_id"] == ids[0]
    with pytest.raises(NotFound):
        service.context(static["id"], "missing")
    dynamic = service.create("dynamic", {"query": "", "options": {}})
    listed = {row["name"]: row for row in service.list()["items"]}
    assert listed["周末阅读"]["count"] == 3 and listed["dynamic"]["count"] == 4
    assert listed["dynamic"]["kind"] == "dynamic" and "rule_json" not in listed["dynamic"]
    with pytest.raises(InvalidInput):
        service.edit_item(dynamic["id"], ids[0], dynamic["revision"])


def test_dynamic_rule_revision_and_search_membership_beyond_first_page(client, files):
    service, collection, ids = populated(files, 30)
    login(client)
    dynamic = service.create("Unread", rule={"query": "", "options": {"reading": "unread"}})
    url = f"/api/collections/{dynamic['id']}/rule?expected_revision={dynamic['revision']}"
    changed = client.put(url, json={"rule": {"query": "", "options": {"reading": "completed"}}}, headers=CSRF)
    assert changed.status_code == 200 and changed.json()["count"] == 0
    stale = client.put(url, json={"rule": {"query": "", "options": {}}}, headers=CSRF)
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    first = client.get("/api/books", params={"limit": 24, "collection_id": collection["id"]}).json()
    second = client.get(
        "/api/books",
        params={
            "limit": 24,
            "after": first["after"],
            "collection_id": collection["id"],
            "include_total": "false",
        },
    ).json()
    found = first["items"] + second["items"]
    assert len(found) == 30 and {book["id"] for book in found} == set(ids)
    assert all(book["in_collection"] for book in found)
