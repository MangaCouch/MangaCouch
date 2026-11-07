from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, PASSCODE, login, make_zip

from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings, load_http_settings, save_http_settings
from mangacouch.diagnostics import library_status
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.scanning import Scanner
from mangacouch.snapshots import InvalidSnapshot, create_snapshot, restore_snapshot


def test_saved_settings_take_effect_only_on_restart(client: TestClient, files: FileLibrary, auth: Auth):
    login(client)
    before = client.get("/api/settings").json()["max_upload_bytes"]
    saved = HTTPSettings(allowed_hosts=[], public_origin="https://books.example", max_upload_bytes=12345)
    save_http_settings(files.library, saved)
    assert load_http_settings(files.library) == saved
    assert client.get("/api/settings").json()["max_upload_bytes"] == before
    assert client.get("/api/auth/me").status_code == 200
    with (
        patch("mangacouch.downloads.Downloads.start"),
        TestClient(create_app(files), base_url="https://books.example") as restarted,
    ):
        _, key = auth.create_key("management", "restart test")
        headers = {"Authorization": f"Bearer {key}"}
        assert restarted.get("/api/settings", headers=headers).json()["max_upload_bytes"] == 12345
        assert restarted.get("/", headers={"Host": "testserver"}).status_code == 400
        response = restarted.post(
            "/api/auth/login",
            json={"passcode": PASSCODE},
            headers={"Origin": "https://books.example", "X-MangaCouch-Request": "1"},
        )
        assert response.status_code == 200 and "Secure" in response.headers["set-cookie"]
        assert "__Host-mangacouch=" in response.headers["set-cookie"]


@pytest.mark.parametrize(
    "origin",
    [
        " https://example.test",
        "https://exa\nmple.test",
        "https://example.test/",
        "https://user@example.test",
        "https://example.test?",
        "https://example.test#",
        "http://example.test:",
        "http://example.test:0",
        "http://example.test:80",
        "https://example.test:443",
        "http://example.test:70000",
        "https://example.test\\@evil.test",
        "file:///tmp/books",
        "https://UPPER.test",
    ],
)
def test_unusable_origin_is_rejected(origin):
    with pytest.raises(ValueError):
        HTTPSettings(public_origin=origin, max_upload_bytes=1000)


@pytest.mark.parametrize("amount", [0, -1, True, "100", 1.5, 2**53])
def test_invalid_upload_limit_is_rejected(amount):
    with pytest.raises(ValueError):
        HTTPSettings.model_validate({"public_origin": ORIGIN, "max_upload_bytes": amount})


def test_diagnostics_and_backup_authorization_and_csrf(client: TestClient, auth: Auth):
    assert client.get("/api/snapshots").status_code == 401
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    for path in ["/api/diagnostics", "/api/snapshots"]:
        assert client.get(path, headers=headers).status_code == 403
    login(client)
    assert client.get("/api/snapshots", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post("/api/snapshots", headers=CSRF).status_code == 405


def test_offline_diagnostics_and_settings_do_not_resume_or_prune(files: FileLibrary, auth: Auth, tmp_path):
    # Test the read endpoints without a maintenance pass consuming the prepared scan.
    with (
        patch("mangacouch.maintenance.Maintenance.start"),
        TestClient(
            create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth), base_url=ORIGIN
        ) as client,
    ):
        item = files.import_file(make_zip(tmp_path / "one.zip")).book
        run = Scanner(files).start()
        login(client)
        before = library_status(files)
        files.root.path.rename(tmp_path / "offline")
        value = client.get("/api/diagnostics").json()
        assert not value["ready"] and value["books"] == 1
        assert value["scan"] == {"id": run["id"], "state": "active"}
        assert value["sidecars"]["pending"] == 1
        assert value["availability_last_known"] == before["availability_last_known"] == {"available": 1}
        assert client.get("/api/settings").status_code == 200
        assert client.get("/api/preferences").status_code == 200
        assert client.get("/api/sidecar/status").status_code == 200
        assert files.library.get_book(item.id).metadata_revision == 1
        current = Scanner(files).latest()
        assert current is not None and current["state"] == "active"


def test_actual_upload_uses_active_limit_until_restart(files: FileLibrary, auth: Auth):
    files.limits = replace(files.limits, max_archive_bytes=30)
    save_http_settings(files.library, HTTPSettings(public_origin=ORIGIN, max_upload_bytes=50))
    with TestClient(create_app(files), base_url=ORIGIN) as client:
        login(client)
        assert client.get("/api/settings").json()["max_upload_bytes"] == 30
        save_http_settings(files.library, HTTPSettings(public_origin=ORIGIN, max_upload_bytes=10))
        assert (
            client.post("/api/import?filename=book.zip", content=b"x" * 20, headers=CSRF).status_code == 422
        )
    with TestClient(create_app(files), base_url=ORIGIN) as client:
        login(client)
        assert (
            client.post("/api/import?filename=book.zip", content=b"x" * 20, headers=CSRF).status_code == 413
        )


@pytest.mark.usefixtures("without_maintenance")
def test_backup_download_contains_saved_configuration_and_restores_offline(
    client: TestClient,
    files: FileLibrary,
    tmp_path: Path,
):
    files.import_file(make_zip(tmp_path / "sample.zip"))
    login(client)
    saved = HTTPSettings(allowed_hosts=[], public_origin="https://books.example", max_upload_bytes=34567)
    save_http_settings(files.library, saved)
    files.root.path.rename(tmp_path / "offline")
    response = client.get("/api/snapshots")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert "attachment" in response.headers["content-disposition"]
    archive = tmp_path / "download.zip"
    archive.write_bytes(response.content)
    restored = restore_snapshot(archive, tmp_path / "restored")
    assert load_http_settings(restored.library) == saved
    diagnosis = library_status(FileLibrary(restored.library, Path(restored.profile.library_root_hint)))
    assert diagnosis["books"] == 1 and not diagnosis["ready"] and diagnosis["sidecars"]["pending"] == 1


def test_restore_rejects_invalid_saved_configuration_before_publication(files: FileLibrary, tmp_path: Path):
    snapshot = create_snapshot(files, tmp_path / "good.zip")
    with zipfile.ZipFile(snapshot) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    database = tmp_path / "edited.sqlite"
    database.write_bytes(parts["library.sqlite"])
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO http_config VALUES(1,?)",
            (json.dumps({"public_origin": "bad", "max_upload_bytes": 10}),),
        )
        db.commit()
    parts["library.sqlite"] = database.read_bytes()
    manifest = json.loads(parts["manifest.json"])
    manifest["files"]["library.sqlite"] = {
        "size": len(parts["library.sqlite"]),
        "sha256": hashlib.sha256(parts["library.sqlite"]).hexdigest(),
    }
    parts["manifest.json"] = json.dumps(manifest).encode()
    invalid = tmp_path / "invalid.zip"
    with zipfile.ZipFile(invalid, "w") as archive:
        for name, data in parts.items():
            archive.writestr(name, data)
    with pytest.raises(InvalidSnapshot):
        restore_snapshot(invalid, tmp_path / "must-not-exist")
    assert not (tmp_path / "must-not-exist").exists()


def test_cli_restore_reports_offline_state_without_task_replay(files: FileLibrary, tmp_path: Path):
    files.import_file(make_zip(tmp_path / "sample.zip"))
    Scanner(files).start()
    archive = create_snapshot(files, tmp_path / "snapshot.zip")
    files.root.path.rename(tmp_path / "offline")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "mangacouch",
            "restore",
            str(archive),
            str(tmp_path / "restored"),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["restored"] and not value["diagnostics"]["ready"]
    assert value["diagnostics"]["books"] == 1 and value["diagnostics"]["scan"]["state"] == "active"
    assert value["http_settings"] == HTTPSettings().model_dump()


def test_public_origin_is_kept_when_additional_hosts_are_set():
    settings = HTTPSettings(public_origin="https://books.example", allowed_hosts=["other.example"])
    assert settings.accepts_host("books.example")
    assert settings.accepts_host("other.example")
    assert not settings.accepts_host("untrusted.example")


@pytest.mark.parametrize(
    ("patterns", "allowed", "blocked"),
    [
        (
            ["0.0.0.0"],
            ["127.0.0.1:8788", "192.168.3.35:8788", "[::1]:8788"],
            ["example.com", "127.0.0.1.evil"],
        ),
        (
            ["*.example.com", "localhost:8788"],
            ["books.example.com", "a.b.example.com:443", "localhost:8788"],
            ["example.com", "badexample.com", "localhost:80"],
        ),
        (
            ["*"],
            ["example.com", "127.0.0.1:8788", "[::1]"],
            ["evil/path", "evil@127.0.0.1", "evil\\host", "evil:bad"],
        ),
    ],
)
def test_host_patterns(patterns, allowed, blocked):
    settings = HTTPSettings(allowed_hosts=patterns)
    assert all(settings.accepts_host(value) for value in allowed)
    assert not any(settings.accepts_host(value) for value in blocked)


def test_each_allowed_host_uses_its_own_origin_for_cookie_writes(files, auth):
    settings = HTTPSettings(public_origin=ORIGIN, allowed_hosts=["testserver", "books.local"])
    app = create_app(files, settings, auth=auth)
    with TestClient(app, base_url="http://books.local") as browser:
        response = browser.post(
            "/api/auth/login",
            json={"passcode": PASSCODE, "label": "test"},
            headers={"Origin": "http://books.local", "X-MangaCouch-Request": "1"},
        )
        assert response.status_code == 200
        assert (
            browser.post(
                "/api/auth/logout", headers={"Origin": "http://testserver", "X-MangaCouch-Request": "1"}
            ).status_code
            == 403
        )
        assert (
            browser.post(
                "/api/auth/logout", headers={"Origin": "http://books.local", "X-MangaCouch-Request": "1"}
            ).status_code
            == 200
        )
        assert browser.get("/api/books", headers={"Host": "evil.local"}).status_code == 400
