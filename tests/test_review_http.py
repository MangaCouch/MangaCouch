from __future__ import annotations

import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    PASSCODE,
    book_id,
    login,
    make_zip,
    pending_sidecars,
)

from mangacouch import auth as auth_module
from mangacouch.auth import (
    GRACE_SECONDS,
    ROTATE_SECONDS,
    SESSION_SECONDS,
    Auth,
    RateLimited,
    Unauthorized,
)
from mangacouch.configuration import HTTPSettings, load_http_settings, save_http_settings
from mangacouch.database import Library, RevisionConflict
from mangacouch.files import ContentChanged
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import create_snapshot, restore_snapshot


def test_web_shell_is_public_but_library_data_stays_private(client: TestClient):
    shell = client.get("/")
    assert shell.status_code == 200 and '<div id="root">' in shell.text
    assert shell.headers["cache-control"] == "no-store"
    assert "script-src 'self'" in shell.headers["content-security-policy"]
    asset = re.search(r'src="(/assets/[^\"]+\.js)"', shell.text)
    assert asset is not None
    assert client.get(asset[1]).status_code == 200
    assert client.head("/").status_code == 200
    assert client.get("/api/books").status_code == 401
    assert client.get("/api/books/anything/pages/0").status_code == 401
    assert client.get("/", headers={"Host": "wrong.example"}).status_code == 400
    assert client.get("/assets/../../library.sqlite").status_code in {401, 404}
    assert client.get("/assets/%2e%2e/http.py").status_code == 404
    assert client.get("/http.py").status_code == 401


@pytest.mark.usefixtures("without_maintenance")
def test_web_shell_loads_when_library_root_is_offline(client: TestClient, files: FileLibrary):
    files.root.path.rename(files.root.path.with_name("offline"))
    assert client.get("/").status_code == 200
    login(client)
    assert client.get("/api/books").status_code == 503


def test_conflict_filter_selects_book_before_applying_limit(
    client: TestClient, files: FileLibrary, tmp_path: Path
):
    ids = [book_id(files, tmp_path)]
    ids.append(files.import_file(make_zip(tmp_path / "other.zip", ["different.png"])).book.id)
    sync = SidecarSync(files)
    for identifier in ids:
        sync.sync_one(identifier)
        path = sync.path(files.library.get_book(identifier))
        document = json.loads(path.read_text())
        document["metadata"]["title"] = "external change"
        path.write_text(json.dumps(document))
        assert sync.sync_one(identifier)["state"] == "conflict"
    login(client)
    conflicts = client.get(f"/api/sidecar/conflicts?book_id={ids[1]}&limit=1").json()["conflicts"]
    assert len(conflicts) == 1 and conflicts[0]["book_id"] == ids[1]
    assert client.get("/api/sidecar/conflicts?book_id=missing").json()["conflicts"] == []


def test_manual_edit_protects_fields_and_rejects_forged_provenance(files: FileLibrary, tmp_path: Path):
    identifier = book_id(files, tmp_path)
    review = Review(files)
    changed = review.edit(
        identifier, Metadata(title="人工标题", field_sources={"title": "native"}), expected_revision=1
    )
    assert changed.metadata.field_sources == {"title": "manual"}
    assert changed.metadata.protected_fields == ["title"]
    assert files.library.search("人工") == [identifier]
    assert pending_sidecars(files.library) == [(identifier, 2)]
    with pytest.raises(RevisionConflict):
        review.edit(identifier, Metadata(title="stale"), expected_revision=1)
    assert files.library.get_book(identifier).metadata.title == "人工标题"


def test_eze_field_choices_and_skip_keep_archive_readable(files: FileLibrary, tmp_path: Path):
    identifier = book_id(files, tmp_path, eze=True)
    review = Review(files)
    preview = review.preview(identifier)
    assert "candidates_unresolved" in preview["issues"]
    original = files.library.get_book(identifier)
    name = next(name for name in preview["candidates"] if "full" in name)
    candidate = preview["candidates"][name]
    choices = {field: "candidate" if field == "title" else "current" for field in candidate["fields"]}
    selected = review.candidate(identifier, name, preview["token"], choices)
    assert selected.metadata.title == "Different source title"
    assert selected.metadata.uploaded_at == original.metadata.uploaded_at
    assert selected.metadata.source_rating == original.metadata.source_rating
    assert selected.personal == PersonalState() and selected.content == original.content
    assert (
        selected.metadata.field_sources["title"] == "manual" and "title" in selected.metadata.protected_fields
    )
    for name in review.preview(identifier)["candidates"]:
        current = review.preview(identifier)
        if current["candidates"][name]["decision"] is None:
            review.candidate(identifier, name, current["token"], None)
    current = review.preview(identifier)
    assert "candidates_unresolved" not in current["issues"]
    assert "declared_page_count_mismatch" in current["issues"]
    assert review.complete(identifier, current["token"], current["issues"]).review_state == "confirmed"
    assert files.page_bytes(identifier, 0).startswith(b"\x89PNG")


def test_review_requires_current_preview_and_explicit_unknown_acknowledgement(
    files: FileLibrary, tmp_path: Path
):
    identifier = book_id(files, tmp_path)
    review = Review(files)
    preview = review.preview(identifier)
    assert set(preview["issues"]) == {"title_missing", "language_unknown", "source_absent"}
    with pytest.raises(ValueError, match="acknowledge"):
        review.complete(identifier, preview["token"], [])
    assert review.complete(identifier, preview["token"], preview["issues"]).review_state == "confirmed"
    assert review.preview(identifier)["confirmation"]["acknowledged_issues"] == preview["issues"]
    with pytest.raises(RevisionConflict):
        review.complete(identifier, preview["token"], preview["issues"])
    edited = review.edit(identifier, Metadata(title="new"), expected_revision=1)
    assert edited.review_state == "needs_review"
    assert review.preview(identifier)["confirmation"] is None
    assert files.page_bytes(identifier, 0).startswith(b"\x89PNG")


def test_pending_candidates_and_sidecar_conflicts_cannot_be_acknowledged_away(
    files: FileLibrary, tmp_path: Path
):
    identifier = book_id(files, tmp_path, eze=True)
    review = Review(files)
    preview = review.preview(identifier)
    with pytest.raises(ValueError, match="pending candidates"):
        review.complete(identifier, preview["token"], preview["issues"])
    name = next(name for name, value in preview["candidates"].items() if value["fields"])
    with pytest.raises(ValueError, match="every differing field"):
        review.candidate(identifier, name, preview["token"], {})
    sync = SidecarSync(files)
    sync.sync_one(identifier)
    native = files.library.native_sidecar(identifier)
    native.metadata.title = "external conflict"
    sync.path(files.library.get_book(identifier)).write_bytes(native.canonical_bytes())
    sync.sync_one(identifier)
    with pytest.raises(RevisionConflict):
        review.candidate(identifier, name, preview["token"], None)
    assert "sidecar_conflicts" in review.preview(identifier)["issues"]


def test_review_event_failure_rolls_back_metadata_and_search(files: FileLibrary, tmp_path: Path):
    identifier = book_id(files, tmp_path)
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_review BEFORE INSERT ON review_event BEGIN SELECT RAISE(ABORT,'fail'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        Review(files).edit(identifier, Metadata(title="must roll back"), expected_revision=1)
    assert files.library.get_book(identifier).metadata == Metadata()
    assert files.library.search("roll") == [] and pending_sidecars(files.library) == [(identifier, 1)]


def test_changed_archive_cannot_be_confirmed(files: FileLibrary, tmp_path: Path):
    identifier = book_id(files, tmp_path)
    review = Review(files)
    preview = review.preview(identifier)
    files.root.resolve(files.library.get_book(identifier).content.relative_path).write_bytes(b"changed")
    with pytest.raises(ContentChanged):
        review.complete(identifier, preview["token"], preview["issues"])
    assert files.library.get_book(identifier).review_state == "needs_review"


def test_review_preview_is_bound_to_one_book(files: FileLibrary, tmp_path: Path):
    first = book_id(files, tmp_path)
    second = files.import_file(make_zip(tmp_path / "second.zip", ["different.png"])).book.id
    review = Review(files)
    preview = review.preview(first)
    with pytest.raises(RevisionConflict):
        review.complete(second, preview["token"], preview["issues"])
    assert files.library.get_book(second).review_state == "needs_review"


def test_auth_is_explicit_and_credentials_are_hashed(files: FileLibrary, auth: Auth):
    with pytest.raises(ValueError, match="already"):
        auth.initialize("replacement-passcode")
    _, key = auth.create_key("management", "fixture key")
    _, session = auth.login(PASSCODE, "fixture session")
    with files.library.connection() as db:
        dump = "\n".join(db.iterdump())
        encoded = db.execute("SELECT passcode_hash FROM auth_settings").fetchone()[0]
    assert PASSCODE not in dump and key not in dump and session not in dump
    assert encoded.startswith("$argon2id$") and "m=19456,t=2,p=1" in encoded
    assert "secret_hash" not in json.dumps(auth.list_credentials())
    with pytest.raises(Unauthorized):
        auth.authenticate(session, session=False)
    with pytest.raises(Unauthorized):
        auth.authenticate(key, session=True)


def test_server_refuses_to_start_without_local_auth_initialization(files: FileLibrary):
    with pytest.raises(ValueError, match="Initialize a passcode"):
        create_app(files)


@pytest.mark.parametrize(
    "route",
    [
        "/api/books",
        "/api/diagnostics",
        "/api/settings",
        "/api/sidecar/conflicts",
        "/api/auth/credentials",
        "/docs",
    ],
)
def test_unauthenticated_reads_are_denied(client: TestClient, route: str):
    response = client.get(route)
    assert response.status_code == 401 and response.headers["cache-control"] == "no-store"


def test_readonly_key_cannot_write_or_read_credential_material(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    identifier = book_id(files, tmp_path)
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/books", headers=headers).status_code == 200
    assert client.get(f"/api/books/{identifier}/review", headers=headers).status_code == 200
    for method, route in [
        ("PUT", f"/api/books/{identifier}/metadata"),
        ("PUT", f"/api/books/{identifier}/personal"),
        ("POST", f"/api/books/{identifier}/review/candidate"),
        ("POST", f"/api/books/{identifier}/review/complete"),
        ("POST", "/api/import?filename=new.zip"),
        ("POST", "/api/sidecar/sync"),
        ("POST", "/api/sidecar/conflicts/unknown/resolve"),
        ("GET", "/api/snapshots"),
        ("POST", "/api/auth/keys"),
        ("POST", "/api/auth/passcode"),
        ("DELETE", "/api/auth/credentials/unknown"),
        ("GET", "/api/auth/credentials"),
        ("GET", "/api/diagnostics"),
    ]:
        assert client.request(method, route, headers=headers).status_code == 403, route
    assert files.library.get_book(identifier).metadata == Metadata()
    assert files.library.get_book(identifier).personal == PersonalState()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://evil.test", "X-MangaCouch-Request": "1"},
        {"Origin": "null", "X-MangaCouch-Request": "1"},
        {"Origin": ORIGIN},
        {**CSRF, "Sec-Fetch-Site": "cross-site"},
    ],
)
def test_login_rejects_cross_site_or_missing_browser_protection(client: TestClient, headers: dict):
    assert client.post("/api/auth/login", json={"passcode": PASSCODE}, headers=headers).status_code == 403


def test_cookie_writes_need_origin_and_bearer_never_falls_back_to_cookie(client: TestClient):
    login(client)
    assert client.post("/api/sidecar/sync", json={}).status_code == 403
    assert (
        client.post("/api/sidecar/sync", json={}, headers={**CSRF, "Origin": "http://evil.test"}).status_code
        == 403
    )
    assert client.post("/api/sidecar/sync", json={}, headers=CSRF).status_code == 200
    assert client.get("/api/books", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/api/books", headers={"Host": "evil.test"}).status_code == 400
    assert client.get("/api/books", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/books?key=invalid").status_code == 200
    client.cookies.clear()
    assert client.get("/api/books?key=invalid").status_code == 401


def test_management_key_creation_and_revocation_over_http(client: TestClient):
    login(client)
    created = client.post("/api/auth/keys", json={"kind": "management", "label": "automation"}, headers=CSRF)
    assert created.status_code == 200 and created.headers["cache-control"] == "no-store"
    identifier, key = created.json()["id"], created.json()["key"]
    headers = {"Authorization": f"Bearer {key}"}
    credentials = client.get("/api/auth/credentials", headers=headers)
    assert key not in credentials.text and "secret_hash" not in credentials.text
    assert any(item["id"] == identifier for item in credentials.json()["credentials"])
    assert client.post("/api/auth/rotate", headers=headers).status_code == 403
    assert client.delete(f"/api/auth/credentials/{identifier}", headers=CSRF).status_code == 200
    assert client.get("/api/books", headers=headers).status_code == 401
    assert client.get("/api/books").status_code == 200


def test_unauthorized_upload_is_rejected_before_reading_body(client: TestClient):
    consumed = []

    def chunks():
        consumed.append(True)
        yield b"should not be consumed"

    response = client.post("/api/import?filename=sample.zip", content=chunks(), headers=CSRF)
    assert response.status_code == 401 and consumed == []


def test_secure_cookie_and_validation_errors_do_not_expose_secrets(files: FileLibrary, auth: Auth):
    origin = "https://testserver"
    with TestClient(
        create_app(files, HTTPSettings(public_origin=origin), auth=auth), base_url=origin
    ) as client:
        headers = {"Origin": origin, "X-MangaCouch-Request": "1"}
        response = client.post("/api/auth/login", json={"passcode": PASSCODE}, headers=headers)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert (
            "__Host-mangacouch=" in cookie
            and "HttpOnly" in cookie
            and "Secure" in cookie
            and "SameSite=strict" in cookie
        )
        assert "Domain=" not in cookie and "Path=/" in cookie
        bad = client.post(
            "/api/auth/login", json={"passcode": PASSCODE, "extra_secret": "DO_NOT_ECHO"}, headers=headers
        )
        assert bad.status_code == 422 and "DO_NOT_ECHO" not in bad.text and PASSCODE not in bad.text
        assert bad.headers["cache-control"] == "no-store"


def test_session_rotation_handles_parallel_old_tokens_and_absolute_expiry(files: FileLibrary):
    clock = [2_000_000_000.0]
    auth = Auth(files.library, clock=lambda: clock[0])
    auth.initialize(PASSCODE)
    identifier, old = auth.login(PASSCODE, "parallel tabs")
    clock[0] += ROTATE_SECONDS
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.rotate(old), range(4)))
    replacements = [result for result in results if result is not None]
    assert len(replacements) == 1
    new, expires_at = replacements[0]
    assert expires_at == 2_000_000_000 + ROTATE_SECONDS + SESSION_SECONDS
    assert auth.authenticate(old, session=True).id == auth.authenticate(new, session=True).id == identifier
    assert auth.rotate(old) is None
    clock[0] += GRACE_SECONDS
    with pytest.raises(Unauthorized):
        auth.authenticate(old, session=True)
    assert auth.authenticate(new, session=True).id == identifier
    # Rotation slides the expiry: an active session outlives the original 30 days.
    clock[0] = 2_000_000_000 + SESSION_SECONDS
    assert auth.authenticate(new, session=True).expires_at == expires_at
    clock[0] = expires_at
    with pytest.raises(Unauthorized):
        auth.authenticate(new, session=True)


def test_rotation_response_never_sets_old_cookie(files: FileLibrary):
    clock = [2_000_000_000.0]
    auth = Auth(files.library, clock=lambda: clock[0])
    auth.initialize(PASSCODE)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth), base_url=ORIGIN
    ) as client:
        login(client)
        old = client.cookies["mangacouch_session"]
        clock[0] += ROTATE_SECONDS
        first = client.post("/api/auth/rotate", headers=CSRF)
        assert first.json()["rotated"] is True and "set-cookie" in first.headers
        second = client.post("/api/auth/rotate", headers={**CSRF, "Cookie": "mangacouch_session=" + old})
        assert second.json()["rotated"] is False and "set-cookie" not in second.headers
        assert "set-cookie" not in client.get("/api/books").headers


def test_revocation_applies_to_conditional_media_requests(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    identifier = book_id(files, tmp_path)
    key_id, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    route = f"/api/books/{identifier}/pages/0"
    image = client.get(route, headers=headers)
    assert image.status_code == 200 and image.content.startswith(b"\x89PNG")
    assert image.headers["cache-control"] == "private, no-cache"
    conditional = {**headers, "If-None-Match": image.headers["etag"]}
    assert client.get(route, headers=conditional).status_code == 304
    auth.revoke(key_id)
    assert client.get(route, headers=conditional).status_code == 401
    assert client.get(route).status_code == 401


def test_passcode_change_revokes_sessions_but_keeps_api_keys(client: TestClient, auth: Auth):
    current_id = login(client)
    other_id, other = auth.login(PASSCODE, "other")
    _, key = auth.create_key("management", "automation")
    assert client.get("/api/auth/me").json()["id"] == current_id
    changed = client.post("/api/auth/passcode", json={"replacement": "new-fixture-passcode"}, headers=CSRF)
    assert changed.status_code == 200 and changed.json()["sessions_revoked"] is True
    assert client.get("/api/books").status_code == 401
    with pytest.raises(Unauthorized):
        auth.authenticate(other, session=True)
    assert any(item["id"] == other_id and item["revoked_at"] is not None for item in auth.list_credentials())
    assert auth.authenticate(key, session=False).kind == "management"
    with pytest.raises(Unauthorized):
        auth.login(PASSCODE, "old password")
    session_id, new = auth.login("new-fixture-passcode", "new password")
    client.cookies.set("mangacouch_session", new)
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    with pytest.raises(Unauthorized):
        auth.authenticate(new, session=True)
    assert any(item["id"] == session_id and item["revoked_at"] for item in auth.list_credentials())


class CountingHasher:
    def __init__(self, real):
        self.real, self.calls, self.active, self.peak = real, 0, 0, 0
        self.lock = threading.Lock()

    def verify(self, encoded, passcode):
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.02)
            return self.real.verify(encoded, passcode)
        finally:
            with self.lock:
                self.active -= 1


def test_login_attempt_limit_is_persistent_and_checked_before_hashing(files: FileLibrary, monkeypatch):
    clock = [2_000_000_000.0]
    auth = Auth(files.library, clock=lambda: clock[0])
    auth.initialize(PASSCODE)
    hasher = CountingHasher(auth_module.PASSWORD_HASHER)
    monkeypatch.setattr(auth_module, "PASSWORD_HASHER", hasher)
    for _ in range(10):
        with pytest.raises(Unauthorized):
            auth.login("wrong password", "attempt", client="203.0.113.5")
    reopened = Auth(Library(files.library.path), clock=lambda: clock[0])
    for passcode in ("wrong password", PASSCODE):
        with pytest.raises(RateLimited) as limited:
            reopened.login(passcode, "still limited", client="203.0.113.5")
        assert limited.value.retry_after == 60
    assert hasher.calls == 10
    # Another client keeps its own window.
    reopened.login(PASSCODE, "other device", client="203.0.113.6")
    clock[0] += 60
    identifier, token = reopened.login(PASSCODE, "new window", client="203.0.113.5")
    assert reopened.authenticate(token, session=True).id == identifier


def test_login_global_ceiling_and_concurrent_hash_budget(files: FileLibrary, monkeypatch):
    auth = Auth(files.library, clock=lambda: 2_000_000_000.0)
    auth.initialize(PASSCODE)
    hasher = CountingHasher(auth_module.PASSWORD_HASHER)
    monkeypatch.setattr(auth_module, "PASSWORD_HASHER", hasher)

    def attempt(number: int) -> str:
        try:
            auth.login("wrong password", "attempt", client=f"198.51.100.{number}")
        except RateLimited:
            return "limited"
        except Unauthorized:
            return "denied"
        return "accepted"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(36)))
    assert results.count("denied") == 30 and results.count("limited") == 6
    assert hasher.calls == 30 and hasher.peak <= 2
    with pytest.raises(RateLimited):
        auth.login(PASSCODE, "correct but limited", client="198.51.100.200")


def test_rate_limit_is_reported_over_http(client: TestClient):
    for _ in range(10):
        response = client.post("/api/auth/login", json={"passcode": "wrong passcode"}, headers=CSRF)
        assert response.status_code == 401
    for passcode in ("wrong passcode", PASSCODE):
        limited = client.post("/api/auth/login", json={"passcode": passcode}, headers=CSRF)
        assert limited.status_code == 429 and limited.json()["error"] == "rate_limited"
        assert 0 < int(limited.headers["retry-after"]) <= 60


def test_passcode_change_during_login_cannot_issue_stale_session(auth: Auth, monkeypatch):
    verify = auth._verify_passcode

    def changed(value, *args):
        revision = verify(value, *args)
        with auth.library.transaction() as db:
            db.execute("UPDATE auth_settings SET revision=revision+1")
        return revision

    monkeypatch.setattr(auth, "_verify_passcode", changed)
    with pytest.raises(Unauthorized):
        auth.login(PASSCODE, "racing login")
    assert auth.list_credentials() == []


def test_http_upload_edit_review_personal_and_sync(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    _, key = auth.create_key("management", "importer")
    headers = {"Authorization": f"Bearer {key}"}
    source = make_zip(tmp_path / "uploaded.zip")
    uploaded = client.post(
        "/api/import", params={"filename": "中文 😀.zip"}, content=source.read_bytes(), headers=headers
    )
    assert uploaded.status_code == 200, uploaded.text
    book = uploaded.json()["book"]
    identifier = book["id"]
    assert book["content"]["original_filename"] == "中文 😀.zip" and book["review_state"] == "needs_review"
    assert client.get(f"/api/books/{identifier}/pages/0", headers=headers).status_code == 200
    edit = client.put(
        f"/api/books/{identifier}/metadata",
        json={"expected_revision": 1, "metadata": {"title": "已修改", "language": "zh"}},
        headers=headers,
    )
    assert edit.status_code == 200 and edit.json()["sidecar"]["pending_revision"] == 2
    assert (
        client.put(
            f"/api/books/{identifier}/metadata",
            json={"expected_revision": 1, "metadata": {}},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.get("/api/books", params={"query": "修改"}, headers=headers).json()["books"][0]["id"]
        == identifier
    )
    state = client.put(
        f"/api/books/{identifier}/personal",
        json={"expected_revision": 1, "state": {"rating": 4.5, "resume_page": 1, "bookmarks": [0]}},
        headers=headers,
    )
    assert state.status_code == 200
    assert (
        client.put(
            f"/api/books/{identifier}/personal",
            json={"expected_revision": 2, "state": {"resume_page": 99}},
            headers=headers,
        ).status_code
        == 422
    )
    preview = client.get(f"/api/books/{identifier}/review", headers=headers).json()
    completed = client.post(
        f"/api/books/{identifier}/review/complete",
        json={"token": preview["token"], "acknowledged_issues": preview["issues"]},
        headers=headers,
    )
    assert completed.status_code == 200 and completed.json()["review_state"] == "confirmed"
    assert (
        client.post("/api/sidecar/sync", json={"book_id": identifier}, headers=headers).json()["state"]
        == "synced"
    )
    assert files.library.get_book(identifier).personal.rating == 4.5


@pytest.mark.parametrize("mode", ["declared", "chunked", "bad_length", "unsafe", "corrupt"])
def test_failed_http_upload_does_not_register_or_publish(
    files: FileLibrary, auth: Auth, tmp_path: Path, mode: str
):
    _, key = auth.create_key("management", "upload")
    data = make_zip(tmp_path / "upload.zip").read_bytes()
    settings = HTTPSettings(public_origin=ORIGIN, max_upload_bytes=100)
    headers = {"Authorization": f"Bearer {key}"}
    name, body, expected = "upload.zip", data, 413
    if mode == "bad_length":
        headers["Content-Length"] = "9"
        body, expected = b"short", 400
    elif mode == "unsafe":
        name, body, expected = "../outside.zip", b"", 422
    elif mode == "corrupt":
        body, expected = b"not an archive", 422
    with TestClient(create_app(files, settings, auth=auth), base_url=ORIGIN) as client:
        if mode == "chunked":
            response = client.post(
                "/api/import",
                params={"filename": name},
                content=iter([data[:60], data[60:120]]),
                headers=headers,
            )
        else:
            response = client.post("/api/import", params={"filename": name}, content=body, headers=headers)
    assert response.status_code == expected, response.text
    assert files.library.search("") == [] and not list(files.root.path.rglob("*.cbz"))


def test_oversized_json_is_rejected_without_exposing_body(client: TestClient):
    login(client)
    response = client.post(
        "/api/auth/keys", content=json.dumps({"label": "DO_NOT_ECHO" * 60000}), headers=CSRF
    )
    assert response.status_code == 413 and "DO_NOT_ECHO" not in response.text


@pytest.mark.usefixtures("without_maintenance")
def test_offline_root_allows_authenticated_diagnostics_and_snapshot_only(
    client: TestClient, files: FileLibrary, tmp_path: Path
):
    identifier = book_id(files, tmp_path)
    login(client)
    files.root.path.rename(tmp_path / "offline")
    assert client.get("/api/books").status_code == 503
    assert client.get(f"/api/books/{identifier}/pages/0").status_code == 503
    diagnosis = client.get("/api/diagnostics").json()
    assert not diagnosis["ready"] and diagnosis["books"] == 1
    assert diagnosis["sidecars"]["pending"] == 1
    result = client.get("/api/snapshots")
    assert result.status_code == 200 and result.content.startswith(b"PK")
    assert result.headers["cache-control"] == "no-store"
    assert files.library.get_book(identifier).personal == PersonalState()


def test_snapshot_restores_review_config_and_key_hashes_but_requires_browser_login(
    files: FileLibrary, auth: Auth, tmp_path: Path
):
    identifier = book_id(files, tmp_path)
    review = Review(files)
    preview = review.preview(identifier)
    review.complete(identifier, preview["token"], preview["issues"])
    _, key = auth.create_key("readonly", "retained reader")
    _, old_session = auth.login(PASSCODE, "old browser")
    settings = HTTPSettings(public_origin="https://library.example", max_upload_bytes=12345)
    save_http_settings(files.library, settings)
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert load_http_settings(restored.library) == settings
    assert restored.library.get_book(identifier).review_state == "confirmed"
    restored_auth = Auth(restored.library)
    assert restored_auth.authenticate(key, session=False).kind == "readonly"
    with pytest.raises(Unauthorized):
        restored_auth.authenticate(old_session, session=True)
    session_id, fresh = restored_auth.login(PASSCODE, "fresh browser")
    assert restored_auth.authenticate(fresh, session=True).id == session_id
    with restored.library.connection() as db:
        assert db.execute("SELECT action FROM review_event").fetchone()[0] == "manual_confirm"


def test_slow_page_work_does_not_block_auth_requests(
    client: TestClient, files: FileLibrary, tmp_path: Path, monkeypatch
):
    identifier = book_id(files, tmp_path)
    login(client)
    entered, release = threading.Event(), threading.Event()
    original = files.page_bytes

    def slow_page(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(files, "page_bytes", slow_page)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, f"/api/books/{identifier}/pages/0")
        try:
            assert entered.wait(3)
            assert client.get("/api/auth/me").status_code == 200
        finally:
            release.set()
        assert pending.result(timeout=5).status_code == 200


def test_real_uvicorn_process_serves_isolated_http(files: FileLibrary, auth: Auth, tmp_path: Path):
    identifier = book_id(files, tmp_path)
    with socket.socket() as reservation:
        try:
            reservation.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("Sandbox forbids binding a loopback port; real HTTP listener not verified")
        port = reservation.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    _, key = auth.create_key("readonly", "live smoke")
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    process = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "mangacouch",
            "--db",
            str(files.library.path),
            "--root",
            str(files.root.path),
            "serve",
            "--port",
            str(port),
            "--origin",
            origin,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        with httpx.Client(base_url=origin, timeout=2, trust_env=False) as connection:
            for _ in range(50):
                try:
                    response = connection.get("/api/books")
                    break
                except httpx.ConnectError:
                    if process.poll() is not None:
                        raise AssertionError("HTTP process stopped before readiness") from None
                    time.sleep(0.05)
            else:
                raise AssertionError("HTTP process did not become ready")
            assert response.status_code == 401
            headers = {"Authorization": f"Bearer {key}"}
            assert connection.get("/api/books", headers=headers).json()["books"][0]["id"] == identifier
            page = connection.get(f"/api/books/{identifier}/pages/0", headers=headers)
            assert page.status_code == 200 and page.content.startswith(b"\x89PNG")
    finally:
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        assert key not in stdout + stderr and PASSCODE not in stdout + stderr
    if os.name == "nt":
        assert process.returncode == 1
    else:
        assert process.returncode in {0, -signal.SIGTERM}
        assert "Application shutdown complete" in stderr


def test_tls_proxy_login_uses_configured_scheme(files):
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    app = create_app(files, HTTPSettings(public_origin="https://books.example"), auth=auth)
    with TestClient(app, base_url="http://books.example") as client:
        response = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "proxy"},
            headers={"Origin": "https://books.example", "X-MangaCouch-Request": "1"},
        )
        assert response.status_code == 200
        assert "Secure" in response.headers["set-cookie"]
        response = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "attacker"},
            headers={"Origin": "http://books.example", "X-MangaCouch-Request": "1"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("sql,arguments", [("SELECT 1 FROM nonexistent", ()), ("SELECT ?", ([],))])
def test_database_programming_errors_are_logged_as_internal_errors(
    api, files, monkeypatch, caplog, sql, arguments
):
    def broken_projection(identifiers, db):
        db.execute(sql, arguments)

    monkeypatch.setattr(files.library, "get_books", broken_projection)
    result = api.get("/api/books")
    assert result.status_code == 500 and result.json() == {"error": "internal_error"}
    assert "nonexistent" not in result.text and "Traceback" not in result.text
    assert any(record.exc_info for record in caplog.records)


def test_database_busy_stays_a_storage_error(api, files, monkeypatch):
    def blocked_projection(identifiers, db):
        db.execute("PRAGMA busy_timeout=0")
        db.execute("UPDATE book SET metadata_revision=metadata_revision")

    monkeypatch.setattr(files.library, "get_books", blocked_projection)
    with files.library.connection() as locked:
        locked.execute("BEGIN IMMEDIATE")
        result = api.get("/api/books")
        locked.rollback()
    assert result.status_code == 503 and result.json() == {"error": "storage_unavailable"}
