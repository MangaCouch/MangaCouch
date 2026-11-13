from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient
from helpers import CSRF, PASSCODE, login, make_zip

from mangacouch.auth import ROTATE_SECONDS
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app
from mangacouch.models import PersonalState


def test_lost_rotation_response_can_recover_after_backgrounding(auth):
    now = [1000000]
    auth.clock = lambda: now[0]
    _, original = auth.login(PASSCODE, "phone")
    now[0] += ROTATE_SECONDS
    rotated = auth.rotate(original)
    assert rotated is not None
    now[0] += 3600
    assert auth.authenticate(original, session=True).rotation_due
    retried = auth.rotate(original)
    assert retried is not None
    assert auth.authenticate(retried[0], session=True).kind == "session"


def test_basic_and_cookie_busy_has_same_503_mapping(client, auth, monkeypatch):
    login(client)

    def busy(*a, **k):
        error = sqlite3.OperationalError("busy")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(auth, "authenticate", busy)
    result = client.get("/api/books", headers={"Authorization": "Basic eDp5"})
    assert result.status_code == 503
    assert result.json()["error"] == "database_busy"
    assert result.headers["Retry-After"] == "2"


def test_missing_hashed_asset_is_not_immutable(client):
    response = client.get("/assets/missing-abcdef123.js")
    assert response.status_code == 404
    assert response.headers["Cache-Control"] == "no-store"


def test_login_count_window_recovers_after_clock_moves_backwards(auth):
    now = [1000000]
    auth.clock = lambda: now[0]
    for _ in range(10):
        auth._admit("127.0.0.1")
    now[0] -= 7200
    auth._admit("127.0.0.1")
    with auth.library.connection() as db:
        row = db.execute("SELECT window_start,attempts FROM login_attempt").fetchone()
    assert tuple(row) == (now[0], 1)


def test_port_specific_session_cookie_names():
    first = HTTPSettings(public_origin="http://localhost:8780")
    second = HTTPSettings(public_origin="http://localhost:8781")
    assert first.cookie_name != second.cookie_name


def test_https_public_origin_allows_explicit_http_lan_alias(files, auth):
    settings = HTTPSettings(public_origin="https://library.example", allowed_hosts=["192.168.1.20"])
    with TestClient(
        create_app(files, auth=auth, settings=settings), base_url="http://192.168.1.20:8780"
    ) as client:
        response = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "LAN"},
            headers={**CSRF, "Origin": "http://192.168.1.20:8780"},
        )
        assert response.status_code == 200, response.text
        assert "; Secure" not in response.headers["set-cookie"]
        assert client.get("/api/auth/me").status_code == 200
        assert (
            client.post("/api/auth/logout", headers={**CSRF, "Origin": "https://other.example"}).status_code
            == 403
        )


def test_komga_page_only_update_cannot_revert_completed(client, files, auth, tmp_path):
    from test_client_protocols import basic

    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    files.library.save_personal(book.id, PersonalState(completed=True), expected_revision=1)
    headers = basic(auth)
    path = f"/komga/api/v1/books/{book.id}"
    assert client.get(path, headers=headers).status_code == 200
    assert client.patch(path + "/read-progress", headers=headers, json={"page": 1}).status_code == 204
    assert files.library.get_book(book.id).personal.completed


def test_bulk_rating_keeps_komga_observation_current(client, files, auth, tmp_path):
    import uuid

    from test_client_protocols import basic

    from mangacouch.organization import BulkAction, BulkSelectionRequest, Organization

    book = files.import_file(make_zip(tmp_path / "rating.zip")).book
    headers = basic(auth)
    path = f"/komga/api/v1/books/{book.id}"
    assert client.get(path, headers=headers).status_code == 200
    service = Organization(files.library)
    job = service.create_bulk(
        BulkSelectionRequest(
            id=str(uuid.uuid4()), action=BulkAction(kind="rating", rating=4), book_ids=[book.id]
        )
    )
    service.advance(budget=10)
    assert service.bulk(job["id"])["counts"]["done"] == 1
    assert client.patch(path + "/read-progress", headers=headers, json={"page": 2}).status_code == 204
    saved = files.library.get_book(book.id)
    assert saved.personal.rating == 4 and saved.personal.resume_page == 1


def test_previous_session_is_revoked_by_next_successful_rotation(auth):
    import pytest

    from mangacouch.auth import Unauthorized

    now = [1000000]
    auth.clock = lambda: now[0]
    identifier, old = auth.login(PASSCODE, "phone")
    now[0] += ROTATE_SECONDS
    current = auth.rotate(old)
    assert current is not None
    now[0] += ROTATE_SECONDS
    next_token = auth.rotate(current[0])
    assert next_token is not None
    with pytest.raises(Unauthorized):
        auth.authenticate(old, session=True)
    auth.revoke(identifier)
    for token in (current[0], next_token[0]):
        with pytest.raises(Unauthorized):
            auth.authenticate(token, session=True)


def test_internal_model_validation_stays_distinct_from_invalid_requests(client):
    from mangacouch.models import Source

    @client.app.get("/api/test-invalid-model")
    def invalid():
        Source.model_validate({"site": "invalid", "gallery_id": -1})

    login(client)
    result = client.get("/api/test-invalid-model")
    assert result.status_code == 500
    assert result.json()["error"] == "internal_error"


def test_snapshot_prepare_and_discard_are_off_event_loop(api, monkeypatch):
    import asyncio
    import tempfile

    import pytest

    seen = []
    original = tempfile.TemporaryDirectory

    def record(operation):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        seen.append(operation)

    class TrackedDirectory(original):
        def __init__(self, *args, **kwargs):
            record("prepare")
            super().__init__(*args, **kwargs)

        def cleanup(self):
            record("cleanup")
            return super().cleanup()

    monkeypatch.setattr(tempfile, "TemporaryDirectory", TrackedDirectory)
    responses = [api.post("/api/snapshots") for _ in range(2)]
    assert all(response.status_code == 200 for response in responses)
    assert api.get(f"/api/snapshots/{responses[-1].json()['id']}").status_code == 200
    assert set(seen) == {"prepare", "cleanup"}


def test_invalid_cli_configuration_matches_documented_exit_one(files, auth):
    import json

    from helpers import python

    result = python(
        "-m",
        "mangacouch",
        "--db",
        files.library.path,
        "--root",
        files.root.path,
        "serve",
        "--origin",
        "invalid-origin",
        "--check",
    )
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]


def test_two_ports_keep_both_instance_sessions(files, auth, tmp_path):
    from mangacouch.auth import Auth
    from mangacouch.database import Library
    from mangacouch.files import LibraryRoot
    from mangacouch.ingest import FileLibrary

    second_root = tmp_path / "second-books"
    second_root.mkdir()
    second_db = Library.initialize(tmp_path / "second.sqlite")
    LibraryRoot.bind(second_db, second_root)
    second_files = FileLibrary(second_db, second_root)
    second_auth = Auth(second_db)
    second_auth.initialize(PASSCODE)
    origins = ["http://localhost:8780", "http://localhost:8781"]
    with (
        TestClient(
            create_app(files, auth=auth, settings=HTTPSettings(public_origin=origins[0])), base_url=origins[0]
        ) as first,
        TestClient(
            create_app(second_files, auth=second_auth, settings=HTTPSettings(public_origin=origins[1])),
            base_url=origins[1],
        ) as second,
    ):
        a = first.post("/api/auth/login", json={"passcode": PASSCODE}, headers={**CSRF, "Origin": origins[0]})
        assert a.status_code == 200
        second.cookies.update(first.cookies)
        b = second.post(
            "/api/auth/login", json={"passcode": PASSCODE}, headers={**CSRF, "Origin": origins[1]}
        )
        assert b.status_code == 200
        first.cookies.update(second.cookies)
        assert first.get("/api/auth/me").json()["id"] == a.json()["id"]
        assert second.get("/api/auth/me").json()["id"] == b.json()["id"]
        assert first.post("/api/auth/logout", headers={**CSRF, "Origin": origins[0]}).status_code == 204
        assert second.get("/api/auth/me").status_code == 200
