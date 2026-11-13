"""Additional allowed hosts may be reached over http (LAN) or https (through a TLS proxy) (HTTP-11)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, PASSCODE

from mangacouch.auth import ROTATE_SECONDS
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app

ALIAS = "manga.tailnet.ts.net"


def alias_client(files, auth) -> TestClient:
    settings = HTTPSettings(public_origin="https://library.example", allowed_hosts=[ALIAS])
    # uvicorn runs with proxy_headers=False: behind the TLS proxy the application sees http://.
    return TestClient(create_app(files, auth=auth, settings=settings), base_url=f"http://{ALIAS}")


def test_https_alias_behind_the_same_tls_proxy_signs_in_with_a_secure_cookie(files, auth):
    with alias_client(files, auth) as client:
        response = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "alias"},
            headers={**CSRF, "Origin": f"https://{ALIAS}"},
        )
        assert response.status_code == 200, response.text
        cookie = response.headers["set-cookie"]
        assert cookie.startswith("__Host-mangacouch_443=") and "; Secure" in cookie
        token = response.cookies["__Host-mangacouch_443"]
        client.cookies.clear()
        session = {"Cookie": f"__Host-mangacouch_443={token}"}
        assert client.get("/api/auth/me", headers=session).json()["kind"] == "session"
        rotated = client.post("/api/auth/rotate", headers={**session, **CSRF, "Origin": f"https://{ALIAS}"})
        assert rotated.status_code == 200 and rotated.json()["rotated"] is False
        for origin in ("https://library.example", "https://evil.example", f"https://{ALIAS}.evil.example"):
            forged = client.post("/api/auth/logout", headers={**session, **CSRF, "Origin": origin})
            assert forged.status_code == 403
        logout = client.post("/api/auth/logout", headers={**session, **CSRF, "Origin": f"https://{ALIAS}"})
        assert logout.status_code == 204
        assert logout.headers["set-cookie"].startswith("__Host-mangacouch_443=")
        assert client.get("/api/auth/me", headers=session).status_code == 401


def test_alias_cookie_writes_still_need_an_origin_of_that_host(files, auth):
    with alias_client(files, auth) as client:
        for headers in (
            CSRF,
            {**CSRF, "Origin": "https://library.example"},
            {**CSRF, "Origin": "https://evil.example"},
            {**CSRF, "Origin": f"ftp://{ALIAS}"},
            {"Origin": f"https://{ALIAS}"},
        ):
            response = client.post(
                "/api/auth/login", json={"passcode": PASSCODE, "label": "alias"}, headers=headers
            )
            assert response.status_code == 403, headers


def test_public_host_keeps_its_configured_scheme(files, auth):
    settings = HTTPSettings(public_origin="https://library.example", allowed_hosts=[ALIAS])
    with TestClient(
        create_app(files, auth=auth, settings=settings), base_url="http://library.example"
    ) as client:
        response = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "downgraded"},
            headers={**CSRF, "Origin": "http://library.example"},
        )
        assert response.status_code == 403


@pytest.mark.parametrize("revoked", [False, True])
def test_https_alias_prefers_secure_session_when_http_cookie_remains(files, auth, revoked):
    now = [auth.clock()]
    auth.clock = lambda: now[0]
    with alias_client(files, auth) as client:
        old = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE},
            headers={**CSRF, "Origin": f"http://{ALIAS}"},
        )
        old_token = old.cookies["mangacouch_session_80"]
        if revoked:
            auth.revoke(old.json()["id"])
        new = client.post(
            "/api/auth/login",
            json={"passcode": PASSCODE},
            headers={**CSRF, "Origin": f"https://{ALIAS}"},
        )
        assert new.status_code == 200
        token = new.cookies["__Host-mangacouch_443"]
        client.cookies.clear()
        both = {"Cookie": f"mangacouch_session_80={old_token}; __Host-mangacouch_443={token}"}
        current = client.get("/api/auth/me", headers=both)
        assert current.status_code == 200, current.text
        assert current.json()["id"] == new.json()["id"]
        assert any(
            item.startswith("mangacouch_session_80=") and "Max-Age=0" in item
            for item in new.headers.get_list("set-cookie")
        )
        http = client.get("/api/auth/me", headers={**both, "Origin": f"http://{ALIAS}"})
        assert http.status_code == (401 if revoked else 200)
        if not revoked:
            assert http.json()["id"] == old.json()["id"]
        now[0] += ROTATE_SECONDS
        rotated = client.post("/api/auth/rotate", headers={**both, **CSRF, "Origin": f"https://{ALIAS}"})
        assert rotated.status_code == 200 and rotated.json()["rotated"] is True
        token = rotated.cookies["__Host-mangacouch_443"]
        client.cookies.clear()
        both = {"Cookie": f"mangacouch_session_80={old_token}; __Host-mangacouch_443={token}"}
        assert client.get("/api/auth/me", headers=both).json()["id"] == new.json()["id"]
        logout = client.post("/api/auth/logout", headers={**both, **CSRF, "Origin": f"https://{ALIAS}"})
        assert logout.status_code == 204
        assert any(
            item.startswith("mangacouch_session_80=") and "Max-Age=0" in item
            for item in logout.headers.get_list("set-cookie")
        )
        assert client.get("/api/auth/me", headers=both).status_code == 401


def test_explicit_alias_origin_and_public_host_do_not_fall_back_to_another_cookie():
    settings = HTTPSettings(public_origin="https://library.example", allowed_hosts=[ALIAS])
    assert (
        settings.session_cookie(ALIAS, "http", f"https://{ALIAS}", {"mangacouch_session_80": "old"})
        == "__Host-mangacouch_443"
    )
    assert (
        settings.session_cookie("library.example", "http", None, {"mangacouch_session_80": "old"})
        == "__Host-mangacouch_443"
    )
