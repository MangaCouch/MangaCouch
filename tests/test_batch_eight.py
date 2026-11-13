from __future__ import annotations

import base64
import sqlite3

import pytest
from helpers import CSRF, import_book, login

from mangacouch.auth import client_address
from mangacouch.configuration import HTTPSettings
from mangacouch.desktop.config import DesktopConfig
from mangacouch.files import ROOT_MARKER


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("::ffff:127.0.0.1", ["192.0.2.9:8080"], "192.0.2.9"),
        ("127.0.0.1", ["[2001:db8::4]:443"], "2001:db8::4"),
        ("192.0.2.1", ["10.0.0.9"], "192.0.2.1"),
    ],
)
def test_forwarded_addresses_normalize_before_trust(peer, forwarded, expected):
    assert client_address(peer, forwarded, ["127.0.0.1/32"]) == expected


def test_desktop_proxy_settings_are_passed_and_validated(tmp_path):
    config = DesktopConfig(state_dir=str(tmp_path), trusted_proxies=["127.0.0.1", "::1"])
    arguments = config.serve_arguments(None)
    assert arguments[-4:] == ["--trusted-proxy", "127.0.0.1", "--trusted-proxy", "::1"]
    assert config.http_settings(None).trusted_proxies == config.trusted_proxies
    with pytest.raises(ValueError):
        DesktopConfig(trusted_proxies=["not-an-address"])


def test_browser_session_ignores_cached_basic_but_keeps_csrf(client, files, tmp_path):
    book = import_book(files, tmp_path)
    login(client)
    basic = {"Authorization": "Basic " + base64.b64encode(b"name:wrong").decode()}
    assert client.get("/api/auth/me", headers=basic).status_code == 200
    route = f"/api/books/{book.id}/personal?expected_revision=1"
    assert client.put(route, headers=basic, json={"state": {}}).status_code == 403
    assert client.put(route, headers=basic | CSRF, json={"state": {}}).status_code == 200
    assert client.get("/opds", headers=basic).status_code == 401


def test_boundary_failure_keeps_json_and_security_headers(client, monkeypatch):
    def fail(*args):
        raise RuntimeError("unexpected host configuration failure")

    monkeypatch.setattr(HTTPSettings, "accepts_host", fail)
    response = client.get("/api/auth/me")
    assert response.status_code == 500 and response.json()["error"] == "internal_error"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_boundary_busy_has_retry_after(client, auth, monkeypatch):
    login(client)
    error = sqlite3.OperationalError("locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    monkeypatch.setattr(auth, "authenticate", lambda *a, **k: (_ for _ in ()).throw(error))
    response = client.get("/api/auth/me")
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_queue_status_stays_available_when_books_disk_is_missing(client, files):
    login(client)
    (files.root.path / ROOT_MARKER).unlink()
    for path in ["/api/tasks", "/api/source-checks", "/api/cache"]:
        assert client.get(path).status_code == 200, path


def test_protocol_covers_negotiate_webp_and_private_cache(client, auth, files, tmp_path):
    book = import_book(files, tmp_path)
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}", "Accept": "image/webp"}
    for path in [f"/opds/books/{book.id}/cover", f"/komga/api/v1/books/{book.id}/thumbnail"]:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/webp"
        assert response.headers["Vary"] == "Authorization, Accept"
        assert response.content[:4] == b"RIFF"
