from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from helpers import CSRF, PASSCODE, login, manager_client

from mangacouch.auth import Auth, Unauthorized
from mangacouch.database import RevisionConflict


def test_authenticated_manager_changes_passcode_without_old_password(client, auth):
    login(client)
    response = client.post("/api/auth/passcode", json={"replacement": "new-fixture-secret"}, headers=CSRF)
    assert response.status_code == 200
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
    for _ in range(2):
        assert client.delete(f"/api/auth/credentials/{data['id']}", headers=CSRF).status_code == 200
    with pytest.raises(Unauthorized):
        auth.authenticate(data["key"], session=False)
    assert client.get("/api/auth/me").json()["id"] == session_id
    assert next(row for row in auth.list_credentials() if row["id"] == data["id"])["revoked_at"] is not None


@pytest.mark.usefixtures("without_maintenance")
def test_passcode_unicode_length_change_revokes_sessions_but_keeps_both_key_kinds(
    client, auth, files, tmp_path
):
    login(client)
    _, other = auth.login(PASSCODE, "other device")
    keys = [auth.create_key(kind, kind)[1] for kind in ["readonly", "management"]]
    files.root.path.rename(tmp_path / "offline")
    replacement = "😀" * 256
    changed = client.post("/api/auth/passcode", json={"replacement": replacement}, headers=CSRF)
    assert changed.status_code == 200 and changed.json()["sessions_revoked"]
    assert "Max-Age=0" in changed.headers["set-cookie"]
    assert client.get("/api/auth/me").status_code == 401
    with pytest.raises(Unauthorized):
        auth.authenticate(other, session=True)
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
    assert response.status_code == 500 and response.json() == {"error": "internal_error"}
    assert any(record.exc_info for record in caplog.records)
    assert client.get("/api/auth/me").json()["id"] == identifier
    with files.library.connection() as db:
        db.execute("DROP TRIGGER fail_revoke")
    assert auth.login(PASSCODE, "after rollback")
    with pytest.raises(Unauthorized):
        auth.login("new-fixture-secret", "not committed")


def test_competing_passcode_changes_have_one_winner(files, auth, monkeypatch):
    import threading

    gate = threading.Barrier(2)
    from types import SimpleNamespace

    from mangacouch import auth as auth_module

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


def test_source_settings_without_secrets_and_explicit_reset(files):
    from mangacouch.source_config import SourceSettings, SourceVault

    vault = SourceVault(files.library)
    revision = vault.save(SourceSettings(comments=False), {}, 0)
    assert not vault.path.exists()
    assert vault.load()[0].comments is False
    vault.save(SourceSettings(comments=False), {"eh_cookie": "secret"}, revision)
    vault.path.unlink()
    with pytest.raises(ValueError):
        vault.load()
    vault.reset_credentials()
    assert vault.load()[1] == {}
    assert vault.load()[0].comments is False


def test_source_reset_refuses_live_worker(files):
    from mangacouch.source_config import SourceVault

    with manager_client(files), pytest.raises(ValueError, match="Stop serve"):
        SourceVault(files.library).reset_credentials()
