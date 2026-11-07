from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from urllib.parse import unquote

import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, login, make_zip

from mangacouch.archives import PDFIUM_LOCK
from mangacouch.database import Library
from mangacouch.files import STAGING
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import restore_snapshot


@pytest.mark.parametrize(
    "name",
    [
        "a" * 300 + ".cbz",
        "漫画" * 200 + ".zip",
        "😀" * 300 + ".cbz",
        "a" * 4092 + ".cbz",
        "原件" * 200 + ".PDF",
    ],
)
def test_long_upload_name_preserves_identity_without_becoming_a_disk_path(client, files, tmp_path, name):
    source = tmp_path / "source.zip"
    if name.endswith(".PDF"):
        with PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
            document.new_page(100, 150).close()
            document.save(source)
    else:
        make_zip(source)
    original = source.read_bytes()
    login(client)
    response = client.post("/api/import", params={"filename": name}, content=original, headers=CSRF)
    assert response.status_code == 200, response.text
    book = response.json()["book"]
    assert response.json()["created"] and book["content"]["original_filename"] == name
    identifier = book["id"]
    assert files.library.search(name[:16]) == [identifier]
    assert client.get(f"/api/books/{identifier}/pages/0").content.startswith(
        b"RIFF" if name.endswith(".PDF") else b"\x89PNG"
    )
    assert files.library.get_book(identifier).personal.resume_page is None
    duplicate = client.post(
        "/api/import",
        params={"filename": "again" + source.suffix if not name.endswith(".PDF") else "again.pdf"},
        content=original,
        headers=CSRF,
    )
    assert duplicate.status_code == 200 and not duplicate.json()["created"]
    assert duplicate.json()["book"]["id"] == identifier
    assert duplicate.json()["book"]["content"]["original_filename"] == name
    download = client.get(f"/opds/books/{identifier}/archive")
    assert download.content == original
    exported_name = unquote(download.headers["content-disposition"].split("''", 1)[1])
    assert len(exported_name.encode()) <= 184
    assert exported_name.lower().endswith((".zip", ".cbz", ".pdf"))
    assert list((files.root.path / STAGING).iterdir()) == []
    assert len(files.library.search("")) == 1


def test_upload_name_limit_rejected_without_creating_a_record(client, files, tmp_path):
    login(client)
    result = client.post(
        "/api/import",
        params={"filename": "a" * 4093 + ".cbz"},
        content=make_zip(tmp_path / "sample.zip").read_bytes(),
        headers=CSRF,
    )
    assert result.status_code == 422
    assert files.library.search("") == [] and not list(files.root.path.rglob("*.cbz"))


def test_uploaded_original_name_survives_failed_commit_and_reopen(files, tmp_path, monkeypatch):
    name = "長原名😀" * 100 + ".cbz"
    source = make_zip(tmp_path / "upload.cbz")

    def stop_commit(*_args):
        raise sqlite3.OperationalError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(files, "_commit_import", stop_commit)
        with pytest.raises(sqlite3.OperationalError):
            files.import_file(source, original_filename=name)
    source.unlink()
    recovered = FileLibrary(Library(files.library.path), files.root.path)
    assert list(recovered.recover().values()) == ["recovered"]
    identifier = recovered.library.search(name[:16])[0]
    assert recovered.library.get_book(identifier).content.original_filename == name
    assert recovered.page_bytes(identifier, 0).startswith(b"\x89PNG")
    assert recovered.recover() == {} and list((files.root.path / STAGING).iterdir()) == []


@pytest.mark.usefixtures("without_maintenance")
def test_http_snapshot_offline_restore_preserves_library_and_defers_external_sidecar(client, files, tmp_path):
    login(client)

    def request(connection, method, path, **kwargs):
        response = connection.request(method, path, headers=CSRF, **kwargs)
        assert response.status_code == 200, response.text
        return response.json()

    uploaded = request(
        client,
        "POST",
        "/api/import",
        params={"filename": "原件.cbz"},
        content=make_zip(tmp_path / "sample.zip").read_bytes(),
    )["book"]
    identifier = uploaded["id"]
    base = f"/api/books/{identifier}"
    request(client, "POST", "/api/sidecar/sync", json={"book_id": identifier})
    metadata = {**uploaded["metadata"], "title": "Saved before snapshot"}
    request(client, "PUT", base + "/metadata", json={"expected_revision": 1, "metadata": metadata})
    state = {**uploaded["personal"], "rating": 4.5, "resume_page": 1, "bookmarks": [0]}
    request(client, "PUT", base + "/personal", json={"expected_revision": 1, "state": state})
    collection = request(client, "POST", "/api/collections", json={"name": "Weekend"})
    collection_path = f"/api/collections/{collection['id']}"
    request(client, "POST", collection_path + "/items", json={"book_id": identifier, "expected_revision": 1})
    expected_collection = request(client, "GET", collection_path)
    preferences = request(client, "GET", "/api/preferences")
    preferences["settings"]["device_defaults"]["direction"] = "rtl"
    expected_preferences = request(
        client,
        "PUT",
        "/api/preferences",
        json={
            "expected_revision": preferences["revision"],
            "settings": preferences["settings"],
        },
    )
    key = request(client, "POST", "/api/auth/keys", json={"kind": "readonly", "label": "Reader"})["key"]
    saved = request(client, "GET", base)
    old_cookies = dict(client.cookies)
    snapshot = client.get("/api/snapshots")
    assert snapshot.status_code == 200
    archive = tmp_path / "snapshot.zip"
    archive.write_bytes(snapshot.content)
    sync = SidecarSync(files)
    sidecar = sync.path(files.library.get_book(identifier))
    external = json.loads(sidecar.read_text())
    external["metadata"]["title"] = "Changed after snapshot"
    sidecar.write_text(json.dumps(external))
    external_bytes = sidecar.read_bytes()
    offline = files.root.path.with_name("offline")
    files.root.path.rename(offline)
    restored = restore_snapshot(archive, tmp_path / "restored")
    recovered = FileLibrary(restored.library, files.root.path, restored.profile.limits)
    with TestClient(create_app(recovered), base_url=ORIGIN) as fresh:
        fresh.cookies.update(old_cookies)
        assert fresh.get("/api/auth/me").status_code == 401
        fresh.cookies.clear()
        login(fresh)
        diagnosis = request(fresh, "GET", "/api/diagnostics")
        assert not diagnosis["ready"] and diagnosis["books"] == 1
        assert diagnosis["sidecars"]["pending"] == 1
        assert fresh.get(base).status_code == 503
        assert recovered.library.get_book(identifier).personal.model_dump() == state
        assert request(fresh, "GET", "/api/preferences") == expected_preferences
        offline.rename(files.root.path)
        assert request(fresh, "GET", "/api/diagnostics")["ready"]
        assert request(fresh, "GET", base) == saved
        assert request(fresh, "GET", collection_path) == expected_collection
        assert fresh.get(base + "/pages/1", headers={"Authorization": f"Bearer {key}"}).content.startswith(
            b"\x89PNG"
        )
        conflict = request(fresh, "POST", "/api/sidecar/sync", json={"book_id": identifier})
        assert conflict["state"] == "conflict" and sidecar.read_bytes() == external_bytes
        assert request(fresh, "GET", base)["metadata"]["title"] == "Saved before snapshot"
        request(
            fresh,
            "POST",
            f"/api/sidecar/conflicts/{conflict['conflict_id']}/resolve",
            json={"choice": "library"},
        )
        assert sidecar.read_bytes() == external_bytes
        assert request(fresh, "POST", "/api/sidecar/sync", json={"book_id": identifier})["state"] == "synced"
        assert json.loads(sidecar.read_text())["metadata"]["title"] == "Saved before snapshot"
        assert request(fresh, "GET", base)["personal"] == state
