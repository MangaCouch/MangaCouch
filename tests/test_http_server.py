from __future__ import annotations

import base64
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath, PureWindowsPath
from xml.etree import ElementTree as ET

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    PASSCODE,
    SUBPROCESS_ENV,
    import_book,
    login,
)

from mangacouch.auth import (
    Auth,
)
from mangacouch.configuration import HTTPSettings
from mangacouch.database import database_uri
from mangacouch.http import create_app
from mangacouch.image_cache import ImageCache
from mangacouch.ingest import FileLibrary


def test_error_bodies_carry_codes_messages_and_unexpected_errors_are_logged_500(
    api, files, caplog, monkeypatch
):
    missing = api.get("/api/books/missing")
    assert missing.status_code == 404 and missing.json() == {
        "error": "book_not_found",
        "message": "Book not found",
    }
    invalid = api.get("/api/reading-history", params={"after": "x"})
    assert invalid.status_code == 422 and invalid.json()["message"] == "Invalid page cursor"
    validation = api.put("/api/books/x/personal", params={"expected_revision": 0}, json={"state": {}})
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
            response = lenient.get("/api/books/anything")
        assert response.status_code == 500 and response.json() == {
            "error": "internal_error",
            "message": "Internal server error",
        }
        assert any(record.exc_info for record in caplog.records)
    lenient.close()
    monkeypatch.undo()
    assert api.get("/opds/books/x/pages/0").json()["error"] == "not_found"


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
    assert client.get("/assets/../../library.sqlite").status_code == 401
    assert client.get("/assets/%2e%2e/http.py").status_code == 404
    assert client.get("/http.py").status_code == 401


@pytest.mark.usefixtures("without_maintenance")
def test_web_shell_loads_when_library_root_is_offline(client: TestClient, files: FileLibrary):
    files.root.path.rename(files.root.path.with_name("offline"))
    assert client.get("/").status_code == 200
    login(client)
    assert client.get("/api/books").status_code == 503


def test_oversized_json_is_rejected_without_exposing_body(client: TestClient):
    login(client)
    response = client.post(
        "/api/auth/keys", content=json.dumps({"label": "DO_NOT_ECHO" * 60000}), headers=CSRF
    )
    assert response.status_code == 413 and "DO_NOT_ECHO" not in response.text


def test_slow_page_work_does_not_block_auth_requests(
    client: TestClient, files: FileLibrary, tmp_path: Path, monkeypatch
):
    identifier = import_book(files, tmp_path).id
    login(client)
    entered, release = threading.Event(), threading.Event()
    original = files.page_bytes

    def slow_page(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

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
    identifier = import_book(files, tmp_path).id
    from helpers import start_test_server

    _, key = auth.create_key("readonly", "live smoke")
    process, port, response = start_test_server(
        lambda port: [
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
            f"http://127.0.0.1:{port}",
        ],
        env=SUBPROCESS_ENV,
        stdout=subprocess.PIPE,
        path="/api/books",
    )
    origin = f"http://127.0.0.1:{port}"
    try:
        assert response.status_code == 401
        with httpx.Client(base_url=origin, timeout=2, trust_env=False) as connection:
            headers = {"Authorization": f"Bearer {key}"}
            assert connection.get("/api/books", headers=headers).json()["items"][0]["id"] == identifier
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


@pytest.mark.parametrize("sql,arguments", [("SELECT 1 FROM nonexistent", ()), ("SELECT ?", ([],))])
def test_database_programming_errors_are_logged_as_internal_errors(
    api, files, monkeypatch, caplog, sql, arguments
):
    def broken_projection(identifiers, db):
        db.execute(sql, arguments)

    monkeypatch.setattr(files.library, "get_books", broken_projection)
    result = api.get("/api/books")
    assert result.status_code == 500 and result.json()["error"] == "internal_error"
    assert "nonexistent" not in result.text and "Traceback" not in result.text
    assert any(record.exc_info for record in caplog.records)


def test_database_busy_is_reported_as_busy_not_as_a_storage_fault(api, files, monkeypatch):
    def blocked_projection(identifiers, db):
        db.execute("PRAGMA busy_timeout=0")
        db.execute("UPDATE book SET metadata_revision=metadata_revision")

    monkeypatch.setattr(files.library, "get_books", blocked_projection)
    with files.library.connection() as locked:
        locked.execute("BEGIN IMMEDIATE")
        result = api.get("/api/books")
        locked.rollback()
    assert result.status_code == 503 and result.json()["error"] == "database_busy"
    assert result.headers["retry-after"] == "2"


def test_shutdown_closes_every_service_when_one_fails(files, auth, monkeypatch):
    closed = []
    app = create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth)
    from mangacouch.downloads import Downloads

    stop, close = Downloads.close, ImageCache.close

    def stuck(self):
        stop(self)
        raise TimeoutError("worker did not stop")

    def recorded(self):
        closed.append(self.path.name)
        close(self)

    monkeypatch.setattr(Downloads, "close", stuck)
    monkeypatch.setattr(ImageCache, "close", recorded)
    with TestClient(app, base_url=ORIGIN):
        pass
    assert sorted(closed) == ["pages.sqlite", "thumbnails.sqlite"]


async def asgi_post(app, key: str, receive) -> list[dict]:
    """POST /api/collections straight to the ASGI app with the given body source; the messages sent back."""
    sent: list[dict] = []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/collections",
        "raw_path": b"/api/collections",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"authorization", f"Bearer {key}".encode()),
            (b"content-type", b"application/json"),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.fixture
def management_app(files: FileLibrary, auth: Auth):
    _, key = auth.create_key("management", "automation")
    return create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth), key


def test_json_body_that_stops_arriving_times_out_with_408(management_app, files, monkeypatch):
    app, key = management_app
    fail_after = anyio.fail_after
    monkeypatch.setattr(anyio, "fail_after", lambda _seconds: fail_after(0.05))
    first = True

    async def stalled():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": b'{"name":', "more_body": True}
        await anyio.sleep_forever()

    sent = anyio.run(asgi_post, app, key, stalled)
    assert sent[0]["status"] == 408
    assert json.loads(sent[1]["body"])["error"] == "body_timeout"
    with TestClient(app, base_url=ORIGIN, headers={"Authorization": f"Bearer {key}"}) as client:
        assert client.get("/api/collections").json()["items"] == []


def test_client_that_disconnects_mid_body_gets_no_response_and_changes_nothing(management_app):
    app, key = management_app
    messages = iter(
        [{"type": "http.request", "body": b'{"name":', "more_body": True}, {"type": "http.disconnect"}]
    )

    async def disconnecting():
        return next(messages)

    assert anyio.run(asgi_post, app, key, disconnecting) == []
    with TestClient(app, base_url=ORIGIN, headers={"Authorization": f"Bearer {key}"}) as client:
        assert client.get("/api/collections").json()["items"] == []


def test_unexpected_errors_are_logged_once_and_keep_the_security_headers(
    files: FileLibrary, auth: Auth, without_maintenance, monkeypatch, caplog
):
    _, key = auth.create_key("management", "checks")
    app = create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth)

    def broken(*_args, **_kwargs):
        raise KeyError("internal detail")

    monkeypatch.setattr(files.library, "get_book", broken)
    # The default client re-raises anything that reaches Starlette's outermost handler.
    with (
        TestClient(app, base_url=ORIGIN, headers={"Authorization": f"Bearer {key}"}) as client,
        caplog.at_level(logging.ERROR),
    ):
        response = client.get("/api/books/anything")
    assert response.status_code == 500 and response.json()["error"] == "internal_error"
    assert "internal detail" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert len([record for record in caplog.records if record.exc_info]) == 1


def test_opds_links_follow_the_host_the_client_used(
    files: FileLibrary, auth: Auth, without_maintenance, tmp_path
):
    book = import_book(files, tmp_path)
    _, key = auth.create_key("readonly", "reader")
    settings = HTTPSettings(public_origin="http://localhost:8780", allowed_hosts=["0.0.0.0"])
    basic = {"Authorization": "Basic " + base64.b64encode(f"reader:{key}".encode()).decode()}
    with TestClient(create_app(files, settings, auth=auth), base_url="http://192.168.1.20:8780") as client:
        for prefix in ("/opds", "/komga/opds/v1.2"):
            feed = ET.fromstring(client.get(f"{prefix}/books?limit=1", headers=basic).content)
            hrefs = [link.attrib["href"] for link in feed.iter("{http://www.w3.org/2005/Atom}link")]
            assert hrefs and all(href.startswith(prefix) for href in hrefs)
            assert f"{prefix}/books/{book.id}/archive" in hrefs
            assert client.get(f"{prefix}/books/{book.id}/archive", headers=basic).status_code == 200
            search = client.get(f"{prefix}/search.xml", headers=basic).text
            assert f'template="{prefix}/books?query={{searchTerms}}"' in search


def test_database_uri_keeps_a_windows_share_in_the_path():
    assert (
        database_uri(PureWindowsPath(r"\\nas\share\state\library.sqlite"))
        == "file:////nas/share/state/library.sqlite?mode=rw"
    )
    assert (
        database_uri(PureWindowsPath(r"C:\state\library.sqlite"), "ro")
        == "file:///C:/state/library.sqlite?mode=ro"
    )
    assert (
        database_uri(PurePosixPath("/srv/my state/library.sqlite"))
        == "file:///srv/my%20state/library.sqlite?mode=rw"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group signal contract")
def test_browser_server_cleans_its_fixture_after_group_interrupt():
    import json
    import os
    import signal

    from helpers import wait_until_serving

    process = subprocess.Popen(
        ["uv", "run", "--no-sync", "python", "tests/ui_browser_server.py", "--port", "0"],
        env={**SUBPROCESS_ENV, "UV_CACHE_DIR": "/tmp/mc-uv-cache"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        address = json.loads(process.stdout.readline())
        assert wait_until_serving(process, address["url"] + "/api/auth/me").status_code == 401
        os.killpg(process.pid, signal.SIGINT)
        stdout, stderr = process.communicate(timeout=15)
        assert process.returncode == 0, stdout + stderr
        assert "Traceback" not in stderr and not Path(address["fixture"]).exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
