from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, archive, content, login, make_zip, register_book, save_metadata

from mangacouch.auth import CREDENTIAL_RETENTION, Auth
from mangacouch.collections import Collections
from mangacouch.database import Library
from mangacouch.duplicates import Duplicates
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.files import RootUnavailable
from mangacouch.http import HTTPSettings, create_app
from mangacouch.maintenance import Maintenance
from mangacouch.models import Metadata, PersonalState, Tag
from mangacouch.organization import Organization
from mangacouch.page_features import PageFeatures
from mangacouch.preferences import LibraryPreferences, save_preferences
from mangacouch.searching import search_terms, tag_suggestions


def titled(library: Library, title: str, *tags: Tag):
    return register_book(library, content(title, f"{title}.zip"), Metadata(title=title, tags=list(tags)))[0]


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Re:Zero", ["re:zero"]),
        ("進撃の巨人：完結編", ["進撃の巨人:完結編"]),
        ("Title: Sub", ["title:", "sub"]),
        ('artist:"blue sky"', [("artist", "blue sky")]),
        ('series:"x y" plain', [("series", "x y"), "plain"]),
        ("artist:alice", [("artist", "alice")]),
        ('"quoted phrase"', ["quoted phrase"]),
    ],
)
def test_colon_terms_are_tags_only_for_known_namespaces_or_quoted_values(query, expected):
    assert search_terms(query) == expected


def test_colon_titles_are_found_by_text_search(library: Library):
    zero = titled(library, "Re:Zero")
    finale = titled(library, "進撃の巨人：完結編")
    sub = titled(library, "Title: Sub")
    tagged = titled(library, "Tagged", Tag(namespace="artist", value="blue sky"))
    assert library.search("Re:Zero") == [zero.id]
    assert library.search("進撃の巨人：完結編") == [finale.id]
    assert library.search("Title: Sub") == [sub.id]
    assert library.search('artist:"blue sky"') == [tagged.id]
    assert library.search("artist:blue") == []


def test_tag_suggestions_count_only_matching_tags(library: Library):
    for index in range(3):
        titled(
            library, f"b{index}", Tag(namespace="artist", value="alpha"), Tag(namespace="group", value="beta")
        )
    with library.connection() as db:
        rows = tag_suggestions(db, "alp")
        assert rows == [{"namespace": "artist", "value": "alpha", "count": 3, "translation": None}]
        assert [row["value"] for row in tag_suggestions(db, "group:")] == ["beta"]


def test_recent_order_uses_reading_activity(library: Library):
    first, second, third = (titled(library, name) for name in ("a", "b", "c"))
    library.save_personal(second.id, PersonalState(resume_page=1), expected_revision=1)
    with library.connection() as db:
        db.execute("UPDATE reading_activity SET updated_at=100 WHERE book_id=?", (second.id,))
    library.save_personal(third.id, PersonalState(resume_page=1), expected_revision=1)
    from mangacouch.searching import SearchOptions

    options = SearchOptions(order="recent")
    ordered = library.search("", options=options)
    assert ordered[:2] == [third.id, second.id] and ordered[2] == first.id
    assert library.search("", options=options, limit=1, after_id=third.id) == [second.id]


def test_collections_are_ordered_by_name_with_opaque_cursor(files):
    collections = Collections(files.library)
    for name in ("beta", "Alpha", "gamma", "alpha"):
        collections.create(name)
    first = collections.list(limit=2)
    assert [row["name"] for row in first["collections"]] in (["Alpha", "alpha"], ["alpha", "Alpha"])
    second = collections.list(limit=2, after=first["after"])
    assert [row["name"] for row in second["collections"]] == ["beta", "gamma"]
    assert second["after"] is None
    with pytest.raises(InvalidInput):
        collections.list(after="not-a-cursor")


def test_collection_counts_context_and_membership_edits(files):
    library = files.library
    books = [titled(library, f"c{index}") for index in range(4)]
    collections = Collections(library)
    static = collections.create("static")
    for book in books:
        static = collections.edit_item(static["id"], book.id, static["revision"])
    library.save_personal(books[0].id, PersonalState(completed=True), expected_revision=1)
    library.save_personal(books[1].id, PersonalState(resume_page=2), expected_revision=1)
    context = collections.context(static["id"], books[2].id)
    assert context["continue_id"] == books[2].id and context["continue_state"] == "unread"
    assert (context["index"], context["previous_id"], context["next_id"]) == (2, books[1].id, books[3].id)
    with pytest.raises(NotFound):
        collections.context(static["id"], "missing")
    static = collections.edit_item(static["id"], books[1].id, static["revision"], action="remove")
    assert [item["position"] for item in static["items"]] == [0, 2, 3]
    static = collections.edit_item(
        static["id"], books[3].id, static["revision"], action="move", before_id=books[0].id
    )
    assert [item["book_id"] for item in static["items"]] == [books[3].id, books[0].id, books[2].id]
    dynamic = collections.create("dynamic", {"query": "c", "options": {}})
    listed = {row["name"]: row for row in collections.list()["collections"]}
    assert listed["static"]["count"] == 3 and listed["dynamic"]["count"] == 4
    assert listed["dynamic"]["kind"] == "dynamic" and "rule_json" not in listed["dynamic"]
    with pytest.raises(InvalidInput):
        collections.edit_item(dynamic["id"], books[0].id, dynamic["revision"])


def test_bulk_jobs_advance_in_the_background(client: TestClient, files):
    books = [titled(files.library, f"bulk{index}") for index in range(3)]
    login(client)
    request = {
        "id": str(uuid.uuid4()),
        "action": {"kind": "completed", "completed": True},
        "targets": [
            {"book_id": book.id, "metadata_revision": 1, "personal_revision": book.personal_revision}
            for book in books
        ],
    }
    created = client.post("/api/bulk", json=request, headers=CSRF)
    assert created.status_code == 200 and created.json()["state"] == "running"
    deadline = time.monotonic() + 10
    while (job := client.get(f"/api/bulk/{request['id']}").json())["state"] != "done":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert job["counts"]["done"] == 3
    assert all(files.library.get_book(book.id).personal.completed for book in books)
    assert client.post(f"/api/bulk/{request['id']}/step", headers=CSRF).status_code in {404, 405}


def test_downloads_key_is_limited_to_download_routes(client: TestClient, auth: Auth, files):
    _, key = auth.create_key("downloads", "extension")
    assert key.startswith("mcd_")
    headers = {"Authorization": f"Bearer {key}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["kind"] == "downloads"
    for method, path in [("GET", "/api/books"), ("GET", "/api/auth/credentials"), ("POST", "/api/auth/keys")]:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 403 and response.json()["error"] == "key_scope"
    assert client.get("/api/downloads/unknown", headers=headers).status_code == 404
    assert client.post("/api/downloads", headers=headers, json={}).status_code == 422
    login(client)
    created = client.post("/api/auth/keys", json={"kind": "downloads", "label": "ext"}, headers=CSRF)
    assert created.status_code == 200 and created.json()["kind"] == "downloads"


def test_credentials_older_than_retention_are_hidden_and_purged(files):
    clock = [2_000_000_000.0]
    auth = Auth(files.library, clock=lambda: clock[0])
    auth.initialize("fixture-only-passcode")
    old, _ = auth.create_key("readonly", "old")
    recent, _ = auth.create_key("readonly", "recent")
    auth.revoke(old)
    clock[0] += CREDENTIAL_RETENTION - 10
    auth.revoke(recent)
    clock[0] += 20
    assert [item["id"] for item in auth.list_credentials()] == [recent]
    assert auth.purge() == 1
    with files.library.connection() as db:
        assert {row[0] for row in db.execute("SELECT id FROM credential")} == {recent}


def test_error_bodies_carry_codes_messages_and_unexpected_errors_are_logged_500(
    api, files, caplog, monkeypatch
):
    missing = api.get("/api/books/missing")
    assert missing.status_code == 404 and missing.json() == {
        "error": "book_not_found",
        "message": "Book not found",
    }
    invalid = api.get("/api/reading-history", params={"before": "x"})
    assert invalid.status_code == 422 and invalid.json()["message"] == "Invalid history cursor"
    validation = api.put("/api/books/x/personal", json={"expected_revision": 0, "state": {}})
    assert validation.status_code == 422 and validation.json()["error"] == "invalid_input"
    # Models validated inside a route from user input are still input errors, not 500s.
    options = api.get("/api/books", params={"order": "random"})
    assert options.status_code == 422 and options.json()["error"] == "invalid_input"
    assert "seed" in options.json()["message"]

    lenient = TestClient(
        api.app, base_url=str(api.base_url), headers=dict(api.headers), raise_server_exceptions=False
    )
    for error in (ValueError("internal detail"), KeyError("internal detail")):

        def broken(*_args, error=error, **_kwargs):
            raise error

        monkeypatch.setattr(files.library, "get_book", broken)
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            legacy = lenient.get("/api/books/anything")
        assert legacy.status_code == 500 and legacy.json() == {"error": "internal_error"}
        assert any(record.exc_info for record in caplog.records)
    lenient.close()
    monkeypatch.undo()
    assert api.get("/opds/books/x/pages/0").json()["error"] == "not_found"


def test_history_and_duplicates_skip_books_that_no_longer_exist(api, files):
    kept = titled(files.library, "kept")
    gone = titled(files.library, "gone")
    for book in (kept, gone):
        files.library.save_personal(book.id, PersonalState(resume_page=1), expected_revision=1)
    with closing(sqlite3.connect(files.library.path)) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("DELETE FROM personal WHERE book_id=?", (gone.id,))
        db.execute("DELETE FROM book WHERE id=?", (gone.id,))
        db.commit()
    history = api.get("/api/reading-history")
    assert history.status_code == 200
    assert [event["book"]["id"] for event in history.json()["events"]] == [kept.id]


def test_duplicate_groups_page_past_low_coverage_pairs(files, tmp_path, monkeypatch):
    left = archive(files, tmp_path, "a.cbz", [1, 2, 3, 4])
    right = archive(files, tmp_path, "b.cbz", [1, 2, 3, 4, 5])
    duplicates = Duplicates(PageFeatures(files))
    for _ in range(100):
        duplicates.poke()
        if not duplicates.run(budget=1.0):
            break
    # Weak pairs (3 shared pages of 30) fail the coverage filter and must not hide the real pair.
    weak = sorted(titled(files.library, f"weak{index}").id for index in range(3))
    with files.library.transaction() as db:
        for low, high in [(weak[0], weak[1]), (weak[0], weak[2]), (weak[1], weak[2])]:
            db.executemany(
                "INSERT INTO duplicate_edge VALUES(?,?,?,?)", ((low, high, page, page) for page in range(3))
            )
    monkeypatch.setattr("mangacouch.duplicates.PAIR_BATCH", 1)
    real = tuple(sorted((left.id, right.id)))
    first = duplicates.groups(limit=1)
    assert [(group["left_id"], group["right_id"]) for group in first["groups"]] == [real]
    after, seen = first["after"], 1
    while after:
        page = duplicates.groups(limit=1, after=after)
        seen += len(page["groups"])
        after = page["after"]
    assert seen == 1
    with pytest.raises(InvalidInput):
        duplicates.groups(after="no-tab")


def test_book_list_total_detail_page_sizes_and_versioned_covers(api, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "sized.zip")).book
    other = files.import_file(make_zip(tmp_path / "other.zip", ["x.png"])).book
    first = api.get("/api/books", params={"limit": 1}).json()
    assert first["total"] == 2
    rest = api.get("/api/books", params={"limit": 1, "after_id": first["after_id"]}).json()
    assert rest["total"] is None and len(rest["books"]) == 1
    assert "page_sizes" not in first["books"][0] and "comments" not in first["books"][0]["metadata"]
    detail = api.get(f"/api/books/{book.id}").json()
    assert detail["page_sizes"] == [[12, 8]] * book.content.page_count
    version = book.content.sha256[:16]
    immutable = api.get(f"/api/books/{book.id}/cover", params={"v": version})
    assert immutable.headers["cache-control"] == "private, max-age=31536000, immutable"
    stale = api.get(f"/api/books/{book.id}/cover", params={"v": other.content.sha256[:16]})
    assert stale.headers["cache-control"] == "private, no-cache" and stale.headers["etag"]
    thumb = api.get(f"/api/books/{book.id}/thumbnails/1", params={"v": version})
    assert thumb.headers["cache-control"] == "private, max-age=31536000, immutable"


def test_settings_summary_works_while_root_is_unavailable(api, files, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RootUnavailable("offline fixture")

    monkeypatch.setattr(files.root, "check", unavailable)
    assert api.get("/api/books").json()["error"] == "root_unavailable"
    assert api.get("/api/settings/summary").status_code == 200


def test_maintenance_hourly_failure_is_logged_once_without_traceback(caplog):
    calls = []

    def offline():
        calls.append("offline")
        raise RootUnavailable("offline")

    worker = Maintenance(steps=[], hourly=[offline])
    with caplog.at_level(logging.WARNING):
        worker.run_hourly()
    assert calls == ["offline"]
    assert [record.exc_info for record in caplog.records] == [None]


def test_maintenance_steps_rerun_while_busy_and_sleep_when_idle():
    remaining = [3]
    done = []

    def work() -> bool:
        if remaining[0]:
            remaining[0] -= 1
            return True
        done.append(True)
        return False

    worker = Maintenance(steps=[work], hourly=[])
    worker.start()
    deadline = time.monotonic() + 5
    while not done:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    worker.close()
    assert remaining == [0] and len(done) == 1


def test_library_storage_details(tmp_path, files):
    with files.library.connection() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
    with files.library.connection(durable=True) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert PersonalState(bookmarks=[3, 1, 3]).bookmarks == [1, 3]
    target = tmp_path / "broken.sqlite"

    def fail(*_args):
        raise sqlite3.OperationalError("fixture")

    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.create_function("fixture_fail", 0, fail)
        return connection

    import mangacouch.database as database_module

    schema = database_module.SCHEMA
    database_module.SCHEMA = schema + "\nSELECT fixture_fail();"
    try:
        database_module.sqlite3.connect = connect  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            Library.initialize(target)
    finally:
        database_module.sqlite3.connect = original  # type: ignore[assignment]
        database_module.SCHEMA = schema
    assert not any(tmp_path.glob("broken.sqlite*"))


def test_komga_titles_follow_the_title_preference(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "pref.zip")).book
    save_metadata(
        files.library, book.id, Metadata(title="Translated", original_title="原題"), expected_revision=1
    )
    save_preferences(
        files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
    )
    auth = Auth(files.library)
    auth.initialize("fixture-only-passcode")
    _, key = auth.create_key("readonly", "reader")
    client = TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    )
    with client:
        listing = client.get("/komga/api/v1/series").json()["content"]
        assert [item["metadata"]["title"] for item in listing] == ["原題"]


def test_organization_bulk_rejects_dynamic_targets_with_code(files):
    collections = Collections(files.library)
    dynamic = collections.create("auto", {"query": "", "options": {}})
    book = titled(files.library, "x")
    from mangacouch.organization import BulkAction, BulkRequest, BulkTarget

    request = BulkRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="add_collection", target_id=dynamic["id"], target_revision=1),
        targets=[BulkTarget(book_id=book.id, metadata_revision=1, personal_revision=1)],
    )
    with pytest.raises(InvalidInput) as error:
        Organization(files.library).create_bulk(request)
    assert error.value.code == "dynamic_collection"


def test_cli_creates_downloads_keys(files, monkeypatch, capsys):
    import json
    import sys

    from mangacouch.__main__ import main

    Auth(files.library).initialize("fixture-only-passcode")
    arguments = ["mangacouch", "--db", str(files.library.path), "--root", str(files.root.path)]
    monkeypatch.setattr(sys, "argv", [*arguments, "key-create", "--kind", "downloads", "--label", "ext"])
    assert main() == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "downloads" and created["key"].startswith("mcd_")
    assert Auth(files.library).authenticate(created["key"], session=False).kind == "downloads"


def test_server_startup_recovers_interrupted_imports_and_stale_uploads(files, auth):
    job_id = str(uuid.uuid4())
    staging = files.root.path / ".mangacouch-staging"
    staging.mkdir(exist_ok=True)
    (staging / f"{job_id}.part").write_bytes(b"partial")
    with files.library.transaction() as db:
        db.execute("INSERT INTO import_job(id,state,kind) VALUES(?,'copying','copy')", (job_id,))
    stale = files.library.path.parent / f"mangacouch-upload-{files.library.path.name}-old"
    stale.mkdir()
    (stale / "spool.cbz").write_bytes(b"x")
    app = create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth)
    with TestClient(app, base_url=ORIGIN), files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job WHERE id=?", (job_id,)).fetchone()[0] == "failed"
    assert not (staging / f"{job_id}.part").exists() and not stale.exists()


def test_second_server_on_a_library_leaves_the_running_servers_imports_alone(client, files, auth):
    database = files.library.path
    spool = database.parent / f"mangacouch-upload-{database.name}-active"
    spool.mkdir()
    with files.library.transaction() as db:
        db.execute("INSERT INTO import_job(id,state,kind) VALUES('in-flight','copying','copy')")
    second = TestClient(create_app(files, auth=auth), base_url=ORIGIN)
    with pytest.raises(ValueError, match="Another download worker"), second:
        pass
    # Recovery and spool cleanup belong to the lock holder only.
    assert spool.is_dir()
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job WHERE id='in-flight'").fetchone()[0] == "copying"
