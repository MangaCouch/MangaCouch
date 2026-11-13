from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, content, import_book, login, register_book, register_books, save_metadata, titled

from mangacouch.activity import Activity
from mangacouch.collections import Collections
from mangacouch.cursors import encode_cursor
from mangacouch.database import Library, RevisionConflict
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.models import Metadata, PersonalState, Source, Tag
from mangacouch.organization import (
    BulkAction,
    BulkSearchRequest,
    BulkSelectionRequest,
    Organization,
)
from mangacouch.snapshots import create_snapshot, restore_snapshot


def request_for(books, action):
    return BulkSelectionRequest(
        id=str(uuid.uuid4()),
        action=action,
        book_ids=[book.id for book in books],
    )


def test_tag_alias_search_is_unicode_normalized_and_revision_checked(files):
    org = Organization(files.library)
    books = [
        register_book(
            files.library,
            content(str(i), f"{i}.cbz"),
            Metadata(title=f"测试{i}", tags=[Tag(namespace="artist", value="ＡＢＣ")]),
        )[0]
        for i in range(3)
    ]
    org.alias(Tag(namespace="artist", value="ABC"), "测试译名", None)
    assert set(files.library.search("译名")) == {book.id for book in books}
    assert org.aliases() == {
        "items": [{"namespace": "artist", "value": "abc", "translation": "测试译名"}],
        "after": None,
    }
    with pytest.raises(RevisionConflict):
        org.alias(Tag(namespace="artist", value="abc"), "new", None)
    org.alias(Tag(namespace="artist", value="abc"), "", "测试译名")
    assert not files.library.search("译名") and org.aliases()["items"] == []


def test_bulk_conflicts_receipts_and_snapshot(files, tmp_path):
    org = Organization(files.library)
    books = [register_book(files.library, content(str(i), f"{i}.cbz"))[0] for i in range(4)]
    request = request_for(books, BulkAction(kind="rating", rating=4.5))
    task = org.create_bulk(request)
    assert task["state"] == "running" and task["counts"]["pending"] == 4
    files.library.save_personal(books[0].id, PersonalState(rating=2), expected_revision=1)
    assert org.advance(limit=2) is True
    assert org.bulk(task["id"])["counts"] == {
        "done": 1,
        "conflict": 1,
        "cancelled": 0,
        "missing": 0,
        "pending": 2,
        "error": 0,
    }
    assert org.create_bulk(request)["state"] == "running"
    # A generous budget: a slow runner must not leave the last batch for another pass.
    assert org.advance(budget=60) is False
    finished = org.bulk(task["id"])["counts"]
    assert finished == {"done": 3, "conflict": 1, "missing": 0, "pending": 0, "error": 0, "cancelled": 0}
    assert org.create_bulk(request)["state"] == "done"
    assert files.library.get_book(books[0].id).personal.rating == 2
    assert files.library.get_book(books[1].id).personal.rating == 4.5
    with pytest.raises(RevisionConflict):
        org.create_bulk(request.model_copy(update={"action": BulkAction(kind="completed")}))
    snapshot = create_snapshot(files, tmp_path / "bulk.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=files.root.path)
    assert Organization(restored.library).bulk(task["id"]) == org.bulk(task["id"])


def test_bulk_add_collection_appends_once_and_skips_dynamic_targets(files):
    org = Organization(files.library)
    collections = Collections(files.library)
    books = [register_book(files.library, content(str(i), f"{i}.cbz"))[0] for i in range(2)]
    static = collections.create("周末")
    static = collections.edit_item(static["id"], books[1].id, static["revision"])
    action = BulkAction(kind="add_collection", target_id=static["id"], target_revision=static["revision"])
    job = org.create_bulk(request_for(books, action))
    org.advance(budget=60)
    assert org.bulk(job["id"])["counts"]["done"] == 2
    current = collections.get(static["id"])
    assert [item["book_id"] for item in current["items"]] == [books[1].id, books[0].id]
    assert current["revision"] == static["revision"] + 1
    dynamic = collections.create("中文", {"query": "", "options": {"language": "Chinese"}})
    action = BulkAction(kind="add_collection", target_id=dynamic["id"], target_revision=dynamic["revision"])
    with pytest.raises(InvalidInput) as rejected:
        org.create_bulk(request_for(books, action))
    assert rejected.value.code == "dynamic_collection"
    missing = BulkAction(kind="add_collection", target_id="missing", target_revision=1)
    with pytest.raises(NotFound):
        org.create_bulk(request_for(books, missing))
    stale = BulkAction(kind="add_collection", target_id=static["id"], target_revision=1)
    with pytest.raises(RevisionConflict):
        org.create_bulk(request_for(books, stale))
    with pytest.raises(ValueError):
        BulkAction(kind="add_collection", target_id=static["id"])


@pytest.mark.parametrize("size", [500, 501, 1001])
def test_complete_selection_survives_restart_and_adds_in_server_batches(library, size):
    books = register_books(library, (content(str(i)) for i in range(size)))
    collections = Collections(library)
    target = collections.create("Many")
    request = BulkSelectionRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="add_collection", target_id=target["id"], target_revision=target["revision"]),
        book_ids=[book.id for book in books],
    )
    org = Organization(library)
    assert org.create_bulk(request)["counts"]["pending"] == size
    org.advance(limit=500)
    assert org.bulk(request.id)["counts"]["done"] == 500
    # Reopen the database as a restarted worker, with no further submission from the client.
    org = Organization(Library(library.path))
    while org.advance():
        pass
    job = org.bulk(request.id)
    assert job["state"] == "done" and job["counts"]["done"] == size
    assert org.create_bulk(request) == job
    current = collections.get(target["id"], limit=200)
    ids = [item["book_id"] for item in current["items"]]
    while current["after"] is not None:
        current = collections.get(target["id"], limit=200, after=current["after"])
        ids += [item["book_id"] for item in current["items"]]
    assert ids == request.book_ids
    assert current["revision"] == target["revision"] + -(-size // 500)


def test_collection_edit_between_server_batches_stops_remaining_items(library):
    books = register_books(library, (content(str(i)) for i in range(1501)))
    collections = Collections(library)
    target = collections.create("Shared")
    org = Organization(library)
    request = BulkSelectionRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="add_collection", target_id=target["id"], target_revision=target["revision"]),
        book_ids=[book.id for book in books],
    )
    org.create_bulk(request)
    org.advance(limit=500)
    collections.rename(target["id"], "Renamed", collections.get(target["id"])["revision"])
    org = Organization(Library(library.path))
    while org.advance():
        pass
    assert org.bulk(request.id)["counts"] == {
        "done": 500,
        "conflict": 1001,
        "pending": 0,
        "cancelled": 0,
        "missing": 0,
        "error": 0,
    }
    with library.connection() as db:
        assert db.execute("SELECT count(*) FROM collection_item").fetchone()[0] == 500
    request = request.model_copy(
        update={
            "id": str(uuid.uuid4()),
            "action": request.action.model_copy(
                update={"target_revision": collections.get(target["id"])["revision"]}
            ),
        }
    )
    org.create_bulk(request)
    while org.advance():
        pass
    with library.connection() as db:
        assert tuple(
            db.execute("SELECT count(*),count(DISTINCT book_id) FROM collection_item").fetchone()
        ) == (1501, 1501)


def test_selection_captures_current_revisions_and_reports_missing_books(library):
    books = [register_book(library, content(str(i)))[0] for i in range(3)]
    library.save_personal(books[0].id, PersonalState(rating=1), expected_revision=1)
    request = BulkSelectionRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="rating", rating=4),
        book_ids=[book.id for book in books] + ["missing"],
    )
    org = Organization(library)
    org.create_bulk(request)
    library.save_personal(books[1].id, PersonalState(rating=5), expected_revision=1)
    assert org.create_bulk(request)["counts"]["pending"] == 4  # retry does not recapture revisions
    with library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (books[2].id,))
    while org.advance():
        pass
    assert org.bulk(request.id)["counts"] == {
        "done": 1,
        "conflict": 1,
        "cancelled": 0,
        "missing": 2,
        "pending": 0,
        "error": 0,
    }
    assert library.get_book(books[0].id).personal.rating == 4
    assert library.get_book(books[1].id).personal.rating == 5
    with pytest.raises(RevisionConflict):
        org.create_bulk(request.model_copy(update={"book_ids": [books[0].id]}))


@pytest.mark.usefixtures("without_maintenance")
def test_large_selection_http_accepts_one_complete_request(client, files):
    login(client)
    book = titled(files.library, "Selected")
    body = {
        "id": str(uuid.uuid4()),
        "action": {"kind": "completed"},
        "book_ids": [book.id] + [str(uuid.uuid4()) for _ in range(14000)],
    }
    response = client.post("/api/bulk-selection", json=body, headers=CSRF)
    assert response.status_code == 200
    assert response.json()["counts"]["pending"] == 14001
    assert not files.library.get_book(book.id).personal.completed
    assert client.post("/api/bulk-selection", json=body, headers=CSRF).json()["id"] == body["id"]
    body["book_ids"] = [book.id, book.id]
    assert client.post("/api/bulk-selection", json=body, headers=CSRF).status_code == 422


def test_organization_http_permissions_and_alias_input(client, files):
    login(client)
    body = {"tag": {"namespace": "artist", "value": "ABC"}, "translation": "译名"}
    assert client.put("/api/tag-aliases", json=body).status_code == 403
    saved = client.put("/api/tag-aliases", json=body, headers=CSRF)
    assert saved.status_code == 200 and saved.json() == {
        "namespace": "artist",
        "value": "abc",
        "translation": "译名",
    }
    assert client.get("/api/tag-aliases").json()["items"] == [
        {"namespace": "artist", "value": "abc", "translation": "译名"}
    ]
    too_long = {**body, "translation": "x" * 513}
    assert client.put("/api/tag-aliases", json=too_long, headers=CSRF).status_code == 422
    changed = {**body, "translation": "其他"}
    assert client.put("/api/tag-aliases", json=changed, headers=CSRF).status_code == 409
    removed = client.put(
        "/api/tag-aliases", json={**body, "translation": "", "expected": "译名"}, headers=CSRF
    )
    assert removed.status_code == 204 and removed.content == b""
    assert client.get("/api/tag-aliases").json() == {"items": [], "after": None}


def test_search_bulk_freezes_over_500_targets_and_revision_conflicts(library):
    ids = [
        book.id
        for book in register_books(library, (content(str(i)) for i in range(505)), Metadata(title="Selected"))
    ]
    org = Organization(library)
    request = BulkSearchRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="rating", rating=4),
        query="Selected",
        expected_count=505,
        exclude=ids[:2],
    )
    job = org.create_bulk(request)
    assert job["counts"]["pending"] == 503
    assert len(job["items"]) == 200 and job["after"] is not None
    assert len(org.bulk(request.id, after=job["after"])["items"]) == 200
    register_book(library, content("later"), Metadata(title="Selected"))
    assert org.create_bulk(request)["counts"]["pending"] == 503
    changed = library.get_book(ids[2])
    library.save_personal(changed.id, PersonalState(rating=5), expected_revision=changed.personal_revision)
    while org.advance(limit=20):
        pass
    job = org.bulk(request.id)
    assert job["state"] == "done" and job["counts"]["done"] == 502 and job["counts"]["conflict"] == 1
    assert library.get_book(ids[0]).personal.rating is None
    assert library.get_book(ids[2]).personal.rating == 5
    assert Activity(library).recent()["items"] == []
    with pytest.raises(RevisionConflict):
        org.create_bulk(request.model_copy(update={"id": str(uuid.uuid4())}))


@pytest.mark.usefixtures("without_maintenance")
def test_tag_suggestion_history_and_bulk_search_routes_need_auth_and_check_counts(
    client, files, auth, tmp_path
):
    identifier = import_book(files, tmp_path).id
    library = files.library
    save_metadata(
        library, identifier, Metadata(tags=[Tag(namespace="artist", value="blue sky")]), expected_revision=1
    )
    Organization(library).alias(Tag(namespace="artist", value="blue sky"), "蓝天", None)
    for path in ["/api/tag-suggestions", "/api/reading-history", "/api/statistics"]:
        assert client.get(path).status_code == 401
    login(client)
    assert client.get("/api/tag-suggestions", params={"query": "蓝天"}).json()["items"][0]["count"] == 1
    for query, total in [('artist:"blue sky"', 1), ("absent", 0)]:
        assert client.get("/api/books", params={"query": query}).json()["total"] == total
    library.save_personal(identifier, PersonalState(resume_page=0), expected_revision=1)
    assert client.get("/api/reading-history").json()["items"][0]["book"]["id"] == identifier
    assert client.get("/api/statistics?days=7").json()["period"]["books"] == 1
    request = {
        "id": str(uuid.uuid4()),
        "action": {"kind": "rating", "rating": 4},
        "query": "蓝天",
        "expected_count": 1,
    }
    response = client.post("/api/bulk-search", json=request, headers=CSRF)
    assert response.status_code == 200 and response.json()["counts"]["done"] == 1
    assert library.get_book(identifier).personal.rating == 4
    assert client.get(f"/api/bulk/{request['id']}", params={"after": encode_cursor(0)}).json()["items"] == []
    request["id"] = str(uuid.uuid4())
    request["expected_count"] = 2
    assert client.post("/api/bulk-search", json=request, headers=CSRF).status_code == 409
    _, readonly = auth.create_key("readonly", "test readonly")
    client.cookies.clear()
    assert (
        client.post(
            "/api/bulk-search", json=request, headers={"Authorization": f"Bearer {readonly}"}
        ).status_code
        == 403
    )


def test_bulk_source_queue_checks_revision_and_reports_missing_sources(files):
    service = Organization(files.library)
    source = Source(site="ehentai", gallery_id=100, gallery_token="abcdef")
    good, _ = register_book(files.library, content("good"), Metadata(source=source))
    absent, _ = register_book(files.library, content("absent"))
    stale, _ = register_book(files.library, content("stale"), Metadata(source=source))
    request = BulkSelectionRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="source"),
        book_ids=[book.id for book in (good, absent, stale)],
    )
    service.create_bulk(request)
    save_metadata(files.library, stale.id, Metadata(title="Changed", source=source), expected_revision=1)
    service.advance(budget=60)
    assert service.bulk(request.id)["counts"] == {
        "done": 1,
        "error": 1,
        "conflict": 1,
        "cancelled": 0,
        "missing": 0,
        "pending": 0,
    }
    with files.library.connection() as db:
        assert (
            db.execute("SELECT state FROM source_check WHERE book_id=?", (good.id,)).fetchone()[0] == "queued"
        )


def test_small_bulk_jobs_finish_in_the_request(client: TestClient, files, after_maintenance):
    books = [titled(files.library, f"bulk{index}") for index in range(3)]
    login(client)
    request = {
        "id": str(uuid.uuid4()),
        "action": {"kind": "completed", "completed": True},
        "book_ids": [book.id for book in books],
    }
    created = client.post("/api/bulk-selection", json=request, headers=CSRF)
    assert created.status_code == 200 and created.json()["state"] == "done"
    after_maintenance(lambda: client.get(f"/api/bulk/{request['id']}").json()["state"] == "done")
    job = client.get(f"/api/bulk/{request['id']}").json()
    assert job["counts"]["done"] == 3
    assert all(files.library.get_book(book.id).personal.completed for book in books)
    assert client.post(f"/api/bulk/{request['id']}/step", headers=CSRF).status_code == 404
