from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    PASSCODE,
    login,
)

from mangacouch import auth as auth_module
from mangacouch.auth import (
    CREDENTIAL_RETENTION,
    MAX_CREDENTIALS,
    Auth,
    Unauthorized,
)
from mangacouch.database import RevisionConflict
from mangacouch.errors import InvalidInput


def test_only_a_signed_in_browser_session_creates_api_keys(client, auth):
    _, management = auth.create_key("management", "automation")
    response = client.post(
        "/api/auth/keys",
        json={"kind": "management", "label": "minted by a key"},
        headers={"Authorization": f"Bearer {management}"},
    )
    assert response.status_code == 403
    assert [row["label"] for row in auth.list_credentials()] == ["automation"]
    login(client)
    created = client.post("/api/auth/keys", json={"kind": "readonly", "label": "reader"}, headers=CSRF)
    assert created.status_code == 200 and created.json()["kind"] == "readonly"


@pytest.mark.usefixtures("without_maintenance")
def test_passcode_change_can_also_revoke_every_api_key(client, auth):
    login(client)
    keys = [auth.create_key(kind, kind)[1] for kind in ["readonly", "management", "downloads"]]
    changed = client.post(
        "/api/auth/passcode", json={"replacement": "new-fixture-secret", "revoke_keys": True}, headers=CSRF
    )
    assert changed.status_code == 204 and changed.content == b""
    for key in keys:
        with pytest.raises(Unauthorized):
            auth.authenticate(key, session=False)
    assert all(row["revoked_at"] is not None for row in auth.list_credentials())


def test_abandoned_sessions_never_block_sign_in_but_keys_keep_the_cap(files):
    clock = [2_000_000_000.0]
    auth = Auth(files.library, clock=lambda: clock[0])
    auth.initialize(PASSCODE)
    _, key = auth.create_key("management", "automation")
    with files.library.transaction() as db:
        for index in range(MAX_CREDENTIALS - 1):
            clock[0] += 1
            auth._issue(db, "session", f"abandoned {index}")
    stalest = auth.list_credentials()[1]
    assert stalest["label"] == "abandoned 0"
    with pytest.raises(InvalidInput, match="Revoke unused credentials"):
        auth.create_key("readonly", "one too many")
    clock[0] += 1
    identifier, token = auth.login(PASSCODE, "owner")
    assert auth.authenticate(token, session=True).id == identifier
    rows = {row["id"]: row for row in auth.list_credentials()}
    assert rows[stalest["id"]]["revoked_at"] is not None
    assert sum(row["revoked_at"] is None for row in rows.values()) == MAX_CREDENTIALS
    assert auth.authenticate(key, session=False)


def test_authenticated_manager_changes_passcode_without_old_password(client, auth):
    login(client)
    response = client.post("/api/auth/passcode", json={"replacement": "new-fixture-secret"}, headers=CSRF)
    assert response.status_code == 204
    assert "new-fixture-secret" not in response.text
    assert client.get("/api/auth/me").status_code == 401
    assert auth.login("new-fixture-secret", "new passcode")
    with pytest.raises(Unauthorized):
        auth.login(PASSCODE, "old passcode")


def test_expired_session_still_gets_401_before_password_verification(client, auth):
    identifier = login(client)
    auth.revoke(identifier)
    assert (
        client.post(
            "/api/auth/passcode",
            json={"replacement": "new-fixture-secret"},
            headers=CSRF,
        ).status_code
        == 401
    )


@pytest.mark.parametrize("kind", ["readonly", "management"])
@pytest.mark.usefixtures("without_maintenance")
def test_keys_create_once_reveal_once_and_revoke_without_touching_sessions_offline(
    client, files, auth, tmp_path, kind
):
    session_id = login(client)
    files.root.path.rename(tmp_path / "offline")
    result = client.post("/api/auth/keys", json={"kind": kind, "label": "测试 😀"}, headers=CSRF)
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    data = result.json()
    assert data["kind"] == kind
    assert auth.authenticate(data["key"], session=False).id == data["id"]
    listing = client.get("/api/auth/credentials")
    assert listing.headers["cache-control"] == "no-store"
    assert data["key"] not in listing.text and "secret_hash" not in listing.text
    with files.library.connection() as db:
        raw = json.dumps([dict(row) for row in db.execute("SELECT * FROM credential")])
    assert data["key"] not in raw
    key_headers = {"Authorization": f"Bearer {data['key']}"}
    if kind == "management":
        listed = client.get("/api/auth/credentials", headers=key_headers)
        assert data["key"] not in listed.text and "secret_hash" not in listed.text
        assert any(item["id"] == data["id"] for item in listed.json()["items"])
    # A key is not a browser session, so it has nothing to rotate.
    assert client.post("/api/auth/rotate", headers=key_headers).status_code == 403
    for _ in range(2):
        assert client.delete(f"/api/auth/credentials/{data['id']}", headers=CSRF).status_code == 204
    with pytest.raises(Unauthorized):
        auth.authenticate(data["key"], session=False)
    assert client.get("/api/auth/me").json()["id"] == session_id
    assert next(row for row in auth.list_credentials() if row["id"] == data["id"])["revoked_at"] is not None


@pytest.mark.usefixtures("without_maintenance")
def test_passcode_unicode_length_change_revokes_sessions_but_keeps_both_key_kinds(
    client, auth, files, tmp_path
):
    login(client)
    other_id, other = auth.login(PASSCODE, "other device")
    keys = [auth.create_key(kind, kind)[1] for kind in ["readonly", "management"]]
    files.root.path.rename(tmp_path / "offline")
    replacement = "😀" * 256
    changed = client.post("/api/auth/passcode", json={"replacement": replacement}, headers=CSRF)
    assert changed.status_code == 204
    assert "Max-Age=0" in changed.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 401
    with pytest.raises(Unauthorized):
        auth.authenticate(other, session=True)
    assert next(row for row in auth.list_credentials() if row["id"] == other_id)["revoked_at"] is not None
    for key in keys:
        assert auth.authenticate(key, session=False)
    response = client.post("/api/auth/login", json={"passcode": replacement}, headers=CSRF)
    assert response.status_code == 200 and replacement not in response.text
    assert client.get("/api/auth/me").status_code == 200


def test_key_and_passcode_mutations_enforce_csrf_readonly_and_secret_redaction(client, auth):
    login(client)
    _, token = auth.create_key("readonly", "reader")
    cases = [
        ("/api/auth/keys", {"kind": "management", "label": "fixture"}),
        ("/api/auth/passcode", {"replacement": "new-fixture-secret"}),
    ]
    for route, data in cases:
        assert client.post(route, json=data).status_code == 403
        assert client.post(route, json=data, headers={"Authorization": f"Bearer {token}"}).status_code == 403
        assert (
            client.post(route, json=data, headers={**CSRF, "Origin": "https://wrong.example"}).status_code
            == 403
        )
    response = client.post(
        "/api/auth/passcode",
        json={"replacement": "short", "private": "NEVER_ECHO"},
        headers=CSRF,
    )
    assert response.status_code == 422 and PASSCODE not in response.text and "NEVER_ECHO" not in response.text
    assert client.get("/api/auth/me").status_code == 200


def test_unauthenticated_passcode_change_is_rejected(client):
    assert (
        client.post(
            "/api/auth/passcode", json={"replacement": "new-fixture-secret"}, headers=CSRF
        ).status_code
        == 401
    )


def test_passcode_transaction_failure_preserves_old_passcode_and_sessions(client, files, auth, caplog):
    identifier = login(client)
    with files.library.connection() as db:
        db.execute(
            "CREATE TRIGGER fail_revoke BEFORE UPDATE OF revoked_at ON credential "
            "BEGIN SELECT RAISE(ABORT,'fixture failure'); END"
        )
    response = client.post("/api/auth/passcode", json={"replacement": "new-fixture-secret"}, headers=CSRF)
    assert response.status_code == 500 and response.json()["error"] == "internal_error"
    assert any(record.exc_info for record in caplog.records)
    assert client.get("/api/auth/me").json()["id"] == identifier
    with files.library.connection() as db:
        db.execute("DROP TRIGGER fail_revoke")
    assert auth.login(PASSCODE, "after rollback")
    with pytest.raises(Unauthorized):
        auth.login("new-fixture-secret", "not committed")


def test_competing_passcode_changes_have_one_winner(files, auth, monkeypatch):

    gate = threading.Barrier(2)
    from types import SimpleNamespace

    original = auth_module.PASSWORD_HASHER.hash

    def verify(value):
        encoded = original(value)
        gate.wait(timeout=5)
        return encoded

    monkeypatch.setattr(
        auth_module,
        "PASSWORD_HASHER",
        SimpleNamespace(hash=verify, verify=auth_module.PASSWORD_HASHER.verify),
    )

    def change(value):
        try:
            auth.change_passcode(value)
            return value
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(change, ["first-fixture-passcode", "second-fixture-passcode"]))
    winners = [value for value in outcomes if value]
    assert len(winners) == 1
    fresh = Auth(files.library)
    identifier, token = fresh.login(winners[0], "winner")
    assert fresh.authenticate(token, session=True).id == identifier


def test_downloads_key_is_limited_to_download_routes(client: TestClient, auth: Auth, files):
    _, key = auth.create_key("downloads", "extension")
    assert key.startswith("mcd_")
    headers = {"Authorization": f"Bearer {key}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["kind"] == "downloads"
    for method, path in [("GET", "/api/books"), ("GET", "/api/auth/credentials"), ("POST", "/api/auth/keys")]:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code == 403 and response.json()["error"] == "key_scope"
    assert client.get("/api/downloads/unknown", headers=headers).status_code == 403
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


def test_cli_creates_downloads_keys(files, monkeypatch, capsys):
    import json

    from mangacouch.__main__ import main

    Auth(files.library).initialize("fixture-only-passcode")
    arguments = ["mangacouch", "--db", str(files.library.path), "--root", str(files.root.path)]
    monkeypatch.setattr(sys, "argv", [*arguments, "key-create", "--kind", "downloads", "--label", "ext"])
    assert main() == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "downloads" and created["key"].startswith("mcd_")
    assert Auth(files.library).authenticate(created["key"], session=False).kind == "downloads"


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
    assert client.delete(f"/api/auth/credentials/{other_id}", headers=CSRF).status_code == 204
    with pytest.raises(Unauthorized):
        auth.authenticate(other_token, session=True)
    assert auth.authenticate(key, session=False).kind == "management"
    assert client.get("/api/auth/me").json()["id"] == current
