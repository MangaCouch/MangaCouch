from __future__ import annotations

import base64
import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    PASSCODE,
    import_book,
    login,
)

from mangacouch import auth as auth_module
from mangacouch.auth import (
    ROTATE_SECONDS,
    SESSION_SECONDS,
    Auth,
    RateLimited,
    Unauthorized,
    client_address,
    login_bucket,
)
from mangacouch.configuration import HTTPSettings
from mangacouch.database import Library
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState


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
    identifier = import_book(files, tmp_path).id
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
        ("POST", "/api/snapshots"),
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
            "__Host-mangacouch_443=" in cookie
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
    clock[0] = 2_000_000_000 + SESSION_SECONDS
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
        old = client.cookies["mangacouch_session_80"]
        clock[0] += ROTATE_SECONDS
        first = client.post("/api/auth/rotate", headers=CSRF)
        assert first.json()["rotated"] is True and "set-cookie" in first.headers
        second = client.post("/api/auth/rotate", headers={**CSRF, "Cookie": "mangacouch_session_80=" + old})
        assert second.json()["rotated"] is False and "set-cookie" not in second.headers
        assert "set-cookie" not in client.get("/api/books").headers


def test_revocation_applies_to_conditional_media_requests(
    client: TestClient, auth: Auth, files: FileLibrary, tmp_path: Path
):
    identifier = import_book(files, tmp_path).id
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


def test_login_has_no_shared_ceiling_and_keeps_the_concurrent_hash_budget(files: FileLibrary, monkeypatch):
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
    # Many clients failing cannot lock out another one: there is no limit they share.
    assert results.count("denied") == 36
    assert hasher.calls == 36 and hasher.peak <= 2
    identifier, token = auth.login(PASSCODE, "owner", client="198.51.100.200")
    assert auth.authenticate(token, session=True).id == identifier


def test_rate_limit_is_reported_over_http(client: TestClient):
    for _ in range(10):
        response = client.post("/api/auth/login", json={"passcode": "wrong passcode"}, headers=CSRF)
        assert response.status_code == 401
    for passcode in ("wrong passcode", PASSCODE):
        limited = client.post("/api/auth/login", json={"passcode": passcode}, headers=CSRF)
        assert limited.status_code == 429 and limited.json()["error"] == "login_rate_limited"
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


def test_logout_revokes_only_the_current_session(client: TestClient, auth: Auth):
    session_id = login(client)
    token = client.cookies["mangacouch_session_80"]
    other_id, other = auth.login(PASSCODE, "other device")
    response = client.post("/api/auth/logout", headers=CSRF)
    assert response.status_code == 204 and "Max-Age=0" in response.headers["set-cookie"]
    with pytest.raises(Unauthorized):
        auth.authenticate(token, session=True)
    assert auth.authenticate(other, session=True).id == other_id
    rows = {row["id"]: row for row in auth.list_credentials()}
    assert rows[session_id]["revoked_at"] is not None and rows[other_id]["revoked_at"] is None


def encoded(credentials: bytes) -> str:
    return "Basic " + base64.b64encode(credentials).decode()


@pytest.mark.parametrize(
    "method,path,authorization",
    [
        ("GET", "/komga/api/v1/series", "Basic not-base64!"),
        ("GET", "/komga/api/v1/series", encoded(b"no separator")),
        ("GET", "/komga/api/v1/series", encoded(b"reader:")),
        ("GET", "/komga/api/v1/series", encoded(b"reader:\xff\xfe")),
        ("GET", "/komga/api/v1/series", "Token abc"),
        ("GET", "/api/books", encoded(b"reader:KEY")),
        ("POST", "/opds/books", encoded(b"reader:KEY")),
    ],
)
def test_malformed_or_misplaced_basic_credentials_are_refused(client, auth, method, path, authorization):
    _, key = auth.create_key("management", "reader app")
    response = client.request(method, path, headers={"Authorization": authorization.replace("KEY", key)})
    assert response.status_code == 401 and response.json()["error"] == "invalid_credentials"
    # Reader apps on /opds and /komga are asked for Basic credentials; the web API is not.
    assert ("www-authenticate" in response.headers) == path.startswith(("/opds", "/komga/"))


def test_a_valid_basic_key_and_a_repeated_authorization_header(client, auth):
    _, key = auth.create_key("readonly", "reader app")
    valid = encoded(f"reader:{key}".encode())
    assert client.get("/komga/api/v1/series", headers={"Authorization": valid}).status_code == 200
    repeated = client.get(
        "/komga/api/v1/series", headers=[("Authorization", valid), ("Authorization", valid)]
    )
    assert repeated.status_code == 401 and repeated.headers["www-authenticate"].startswith("Basic ")


@pytest.mark.parametrize("failure", [sqlite3.OperationalError("disk I/O error"), OSError("unreadable")])
def test_credential_storage_failure_is_unavailable_not_unauthorized(client, auth, monkeypatch, failure):
    _, key = auth.create_key("readonly", "reader")

    def broken(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(auth, "authenticate", broken)
    response = client.get("/api/books", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 503 and response.json()["error"] == "storage_unavailable"


# Sign-in limits count per client, never globally -------------------------------------------------------


def login_attempt(client: TestClient, passcode: str, forwarded: str | None = None) -> int:
    headers = dict(CSRF)
    if forwarded is not None:
        headers["X-Forwarded-For"] = forwarded
    return client.post("/api/auth/login", json={"passcode": passcode}, headers=headers).status_code


@pytest.fixture
def peer_server(files: FileLibrary, auth: Auth, without_maintenance):
    opened = []

    def make(peer: str, trusted_proxies: list[str]) -> TestClient:
        settings = HTTPSettings(public_origin=ORIGIN, trusted_proxies=trusted_proxies)
        client = TestClient(create_app(files, settings, auth=auth), base_url=ORIGIN, client=(peer, 50000))
        opened.append(client.__enter__())
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


def test_client_address_reads_forwarding_only_from_trusted_proxies():
    proxies = ["172.17.0.0/16", "10.0.0.5"]
    assert client_address("203.0.113.9", ["198.51.100.1"], proxies) == "203.0.113.9"
    assert client_address("172.17.0.1", ["198.51.100.1"], proxies) == "198.51.100.1"
    # A client cannot hide behind a forged entry: the nearest untrusted hop wins.
    assert client_address("172.17.0.1", ["1.2.3.4, 198.51.100.1, 10.0.0.5"], proxies) == "198.51.100.1"
    assert client_address("172.17.0.1", ["1.2.3.4", "198.51.100.1"], proxies) == "198.51.100.1"
    assert client_address("172.17.0.1", [], proxies) == "172.17.0.1"
    assert client_address("172.17.0.1", ["10.0.0.5"], proxies) == "172.17.0.1"
    assert client_address("172.17.0.1", ["198.51.100.1"], []) == "172.17.0.1"


def test_ipv6_clients_share_a_limit_per_64_block():
    assert login_bucket("2001:db8:1:2::1") == login_bucket("2001:db8:1:2:ffff::9") == "2001:db8:1:2::/64"
    assert login_bucket("2001:db8:1:3::1") != login_bucket("2001:db8:1:2::1")
    assert login_bucket("::ffff:192.0.2.1") == "192.0.2.1"
    assert login_bucket("192.0.2.1") == "192.0.2.1"
    assert login_bucket("testclient") == "testclient"


def test_failing_clients_cannot_use_up_sign_ins_for_everyone(files: FileLibrary):
    auth = Auth(files.library, clock=lambda: 2_000_000_000.0)
    auth.initialize(PASSCODE)
    for number in range(4):  # 40 failures, more than the former shared ceiling of 30
        for _ in range(10):
            with pytest.raises(Unauthorized):
                auth.login("wrong passcode", "attacker", client=f"198.51.100.{number}")
        with pytest.raises(RateLimited):
            auth.login(PASSCODE, "attacker", client=f"198.51.100.{number}")
    identifier, token = auth.login(PASSCODE, "owner", client="203.0.113.1")
    assert auth.authenticate(token, session=True).id == identifier


def test_sign_ins_without_a_client_address_are_not_counted(files: FileLibrary):
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    for _ in range(12):
        with pytest.raises(Unauthorized):
            auth.login("wrong passcode", "unknown")
    auth.login(PASSCODE, "unknown")
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM login_attempt").fetchone()[0] == 0


def test_behind_a_trusted_proxy_sign_ins_count_per_client(peer_server):
    client = peer_server("172.17.0.1", ["172.17.0.0/16"])
    for _ in range(10):
        assert login_attempt(client, "wrong passcode", "203.0.113.5") == 401
    assert login_attempt(client, PASSCODE, "203.0.113.5") == 429
    # Another client behind the same proxy, and the proxy itself, are unaffected.
    assert login_attempt(client, PASSCODE, "203.0.113.6") == 200
    assert login_attempt(client, PASSCODE) == 200


def test_forwarding_headers_from_untrusted_peers_are_ignored(peer_server, caplog):
    client = peer_server("198.51.100.9", [])
    with caplog.at_level(logging.WARNING, logger="mangacouch.api_auth"):
        for number in range(10):
            assert login_attempt(client, "wrong passcode", f"203.0.113.{number}") == 401
    assert login_attempt(client, PASSCODE, "203.0.113.99") == 429
    warnings = [record for record in caplog.records if "MANGACOUCH_TRUSTED_PROXIES" in record.message]
    assert len(warnings) == 1


@pytest.mark.parametrize(
    "read,method,write",
    [
        ("/api/sidecar/conflicts", "POST", "/api/sidecar/conflicts/unknown/resolve"),
        ("/api/sidecar/status", "POST", "/api/sidecar/sync"),
        ("/api/duplicates", "POST", "/api/duplicates/ignore"),
        ("/api/books/{id}/review", "POST", "/api/books/{id}/review/complete"),
    ],
)
def test_readonly_review_and_sync_inspection(client, auth, files, tmp_path, read, method, write):
    identifier = import_book(files, tmp_path).id
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get(read.format(id=identifier), headers=headers).status_code == 200
    assert client.request(method, write.format(id=identifier), json={}, headers=headers).status_code == 403
