from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote

import anyio
import httpx
import pypdfium2 as pdfium
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    login,
    make_zip,
    parts,
)
from starlette.requests import ClientDisconnect, Request

from mangacouch import http, uploads
from mangacouch.archives import PDFIUM_LOCK
from mangacouch.auth import (
    Auth,
)
from mangacouch.configuration import HTTPSettings
from mangacouch.editions import Editions
from mangacouch.files import STAGING
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.models import PersonalState, read_native
from mangacouch.review import Review
from mangacouch.services import Services
from mangacouch.sidecars import SidecarSync


@pytest.mark.parametrize("operation", ["candidates", "book_output"])
@pytest.mark.parametrize("failure", ["database", "filesystem"])
def test_committed_upload_survives_auxiliary_read_failure(
    api, files, tmp_path, monkeypatch, operation, failure
):
    def unavailable(*args, **kwargs):
        if failure == "filesystem":
            raise OSError(5, "Injected read failure")
        error = sqlite3.OperationalError("Injected database read failure")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    owner = Editions if operation == "candidates" else Services
    with monkeypatch.context() as patch:
        patch.setattr(owner, operation, unavailable)
        response = api.post(
            "/api/import?filename=upload.cbz", content=make_zip(tmp_path / "upload.cbz").read_bytes()
        )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["created"] is True
    warning = "previous_books_unavailable" if operation == "candidates" else "book_details_unavailable"
    assert warning in result["warnings"]
    book = files.library.get_book(result["book"]["id"])
    assert result["book"]["content"]["sha256"] == book.content.sha256
    assert (files.root.path / book.content.relative_path).is_file()
    assert api.get(f"/api/books/{book.id}").status_code == 200
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 1
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "done"


@pytest.mark.parametrize("mode", ["archive", "native", "eze", "both"])
def test_pair_import_reuses_metadata_rules_and_keeps_personal_state_private(client, files, tmp_path, mode):
    data = parts(tmp_path, mode)
    login(client)
    response = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert response.status_code == 200, response.text
    book = files.library.get_book(response.json()["book"]["id"])
    expected = "本地标题" if mode in {"native", "both"} else "Eze title" if mode == "eze" else None
    assert book.metadata.title == expected
    assert book.content.original_filename == "作品😀.cbz" and book.personal == PersonalState()
    assert book.review_state == "needs_review" and book.metadata_revision == 1
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")
    assert (files.root.path / book.content.relative_path).read_bytes() == data[0][1][1]
    preview = Review(files).preview(book.id)
    if mode == "both":
        assert "title" in preview["candidates"]["eze:info"]["fields"]
    with files.library.connection() as db:
        assert "MUST_NOT_RETAIN" not in "\n".join(db.iterdump())
    # The upload publishes the library's sidecar next to the stored archive right away (or finds
    # that background maintenance, which may run between the commit and the write, already did).
    assert response.json()["sidecar_sync"]["state"] in {"synced", "unchanged"}
    written = SidecarSync(files).path(book).read_bytes()
    assert read_native(written, book.content.sha256).metadata == book.metadata
    assert b"MUST_NOT_RETAIN" not in written and b"personal" not in written


@pytest.mark.parametrize("damage", ["json", "hash", "schema", "personal"])
def test_invalid_native_keeps_archive_readable_and_valid_eze_fallback(client, files, tmp_path, damage):
    data = parts(tmp_path)
    native = json.loads(data[1][1][1])
    if damage == "hash":
        native["content_sha256"] = "0" * 64
    elif damage == "schema":
        native["schema"] = "unsupported"
    elif damage == "personal":
        native["personal"] = {"rating": 5, "private": "MUST_NOT_RETAIN"}
    content = b'{"MUST_NOT_RETAIN"' if damage == "json" else json.dumps(native).encode()
    data[1] = ("native", (data[1][1][0], content, "application/json"))
    login(client)
    response = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert response.status_code == 200
    assert "native_unreadable_or_invalid" in response.json()["warnings"]
    book = files.library.get_book(response.json()["book"]["id"])
    assert book.metadata.title == "Eze title" and book.personal == PersonalState()
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")
    with files.library.connection() as db:
        assert "MUST_NOT_RETAIN" not in "\n".join(db.iterdump())


def test_duplicate_pair_keeps_saved_metadata_personal_and_confirmation(client, files, tmp_path):
    data = parts(tmp_path)
    login(client)
    first = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF).json()
    identifier = first["book"]["id"]
    state = PersonalState(rating=4.5, resume_page=1)
    files.library.save_personal(identifier, state, expected_revision=1)
    metadata = files.library.get_book(identifier).metadata.model_copy(update={"title": "Manual"})
    Review(files).edit(identifier, metadata, expected_revision=1)
    before = files.library.get_book(identifier)
    result = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert not result.json()["created"] and result.json()["book"]["id"] == identifier
    assert "existing_book_metadata_preserved" in result.json()["warnings"]
    assert files.library.get_book(identifier) == before


@pytest.mark.parametrize(
    "kind,status",
    [
        ("wrong_name", 422),
        ("duplicate", 422),
        ("unknown", 422),
        ("field", 400),
        ("no_archive", 400),
        ("four_files", 400),
    ],
)
def test_bad_multipart_structure_is_rejected_before_import(client, files, tmp_path, kind, status):
    data = parts(tmp_path)
    if kind == "wrong_name":
        data[1] = ("native", ("../another.json", b"{}", "application/json"))
    elif kind == "duplicate":
        data[2] = data[1]
    elif kind == "unknown":
        data[1] = ("secret", data[1][1])
    elif kind == "field":
        data[1] = ("native", (None, b"{}", "application/json"))
    elif kind == "no_archive":
        data.pop(0)
    else:
        data.append(data[0])
    login(client)
    result = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert result.status_code == status, result.text
    assert result.json()["error"] == ("invalid_input" if status == 422 else "bad_request")
    assert files.library.search("") == [] and not list(files.root.path.rglob("*.cbz"))


@pytest.mark.parametrize("kind", ["archive", "native", "eze"])
def test_each_part_has_a_streaming_byte_limit(client, files, tmp_path, monkeypatch, kind):
    data = parts(tmp_path)
    monkeypatch.setattr(uploads, "MAX_NATIVE_BYTES", 200)
    monkeypatch.setattr(uploads, "MAX_EZE_BYTES", 200)
    index = {"archive": 0, "native": 1, "eze": 2}[kind]
    data = [data[0], data[index]] if index else [data[0]]
    name = data[-1][1][0]
    data[-1] = (kind, (name, b"x" * 201, "application/octet-stream"))
    login(client)
    if kind == "archive":
        with (
            patch("mangacouch.downloads.Downloads.start"),
            TestClient(
                http.create_app(files, http.HTTPSettings(public_origin=ORIGIN, max_upload_bytes=200)),
                base_url=ORIGIN,
            ) as limited,
        ):
            login(limited)
            result = limited.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    else:
        result = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert result.status_code == 413 and files.library.search("") == []


@pytest.mark.parametrize("failure", ["truncated", "bad_length", "large_headers", "no_boundary"])
def test_broken_transport_never_commits_archive(client, files, tmp_path, failure):
    request = httpx.Request("POST", ORIGIN, files=parts(tmp_path))
    body = request.read()
    headers = {**CSRF, "Content-Type": request.headers["content-type"]}
    if failure == "truncated":
        body = body[:-10]
    elif failure == "bad_length":
        headers["Content-Length"] = str(len(body) + 1)
    elif failure == "large_headers":
        body = body.replace(
            b"Content-Disposition", b"X-Oversized: " + b"x" * 65537 + b"\r\nContent-Disposition", 1
        )
    else:
        headers["Content-Type"] = "multipart/form-data"
    login(client)
    result = client.post("/api/import", params={"filename": "作品😀.cbz"}, content=body, headers=headers)
    assert result.status_code in ({400, 413} if failure == "large_headers" else {400})
    assert files.library.search("") == []


@pytest.mark.parametrize("failure", ["disconnect", "cancel"])
def test_stream_interruption_closes_spooled_files(tmp_path, temporary_cleanup, failure):
    outgoing = httpx.Request("POST", ORIGIN, files=parts(tmp_path))
    body = outgoing.read()

    async def run():
        count = 0

        async def receive():
            nonlocal count
            count += 1
            if count == 1:
                return {"type": "http.request", "body": body[:300], "more_body": True}
            if failure == "disconnect":
                return {"type": "http.disconnect"}
            raise anyio.get_cancelled_exc_class()()

        request = Request(
            {"type": "http", "headers": [(b"content-type", outgoing.headers["content-type"].encode())]},
            receive,
        )
        expected = ClientDisconnect if failure == "disconnect" else anyio.get_cancelled_exc_class()
        with pytest.raises(expected):
            await uploads.receive_upload(request, tmp_path, "作品😀.cbz", 10000)

    anyio.run(run)
    assert temporary_cleanup and all(file.closed for file in temporary_cleanup)


def test_long_original_and_exact_metadata_limits_use_short_transport_names(client, files, tmp_path):
    name = "漫😀" * 2046 + ".cbz"
    data = parts(tmp_path, name=name)
    for index, limit in [(1, uploads.MAX_NATIVE_BYTES), (2, uploads.MAX_EZE_BYTES)]:
        kind, (filename, body, mime) = data[index]
        data[index] = (kind, (filename, body + b" " * (limit - len(body)), mime))
    login(client)
    result = client.post("/api/import", params={"filename": name}, files=data, headers=CSRF)
    assert result.status_code == 200, result.text
    assert result.json()["book"]["content"]["original_filename"] == name
    assert files.library.search(name[:16]) == [result.json()["book"]["id"]]


@pytest.mark.parametrize("principal", ["anonymous", "readonly", "csrf"])
def test_pair_auth_is_checked_before_reading_any_body(client, auth, files, principal):
    headers = {**CSRF, "Content-Type": "multipart/form-data; boundary=fixture"}
    expected = 401
    if principal == "readonly":
        _, key = auth.create_key("readonly", "fixture")
        headers["Authorization"] = f"Bearer {key}"
        expected = 403
    elif principal == "csrf":
        login(client)
        headers.pop("Origin")
        expected = 403
    consumed = []

    def chunks():
        consumed.append(True)
        yield b"should never be read"

    result = client.post("/api/import?filename=sample.cbz", content=chunks(), headers=headers)
    assert result.status_code == expected and not consumed
    assert files.library.search("") == []


def test_staging_write_error_closes_parsed_spools_and_leaves_no_record(client, files, tmp_path, monkeypatch):
    async def fail(*_args, **_kwargs):
        raise OSError("injected temporary disk failure")

    data = parts(tmp_path)
    login(client)
    monkeypatch.setattr(uploads.anyio, "open_file", fail)
    result = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert result.status_code == 503 and files.library.search("") == []


def test_chunked_oversized_metadata_stops_consuming_before_archive(tmp_path, monkeypatch):
    data = parts(tmp_path)
    data[1] = ("native", (data[1][1][0], b"x" * 10000, "application/json"))
    request = httpx.Request("POST", ORIGIN, files=[data[1], data[0]])
    body = request.read()
    consumed = []
    monkeypatch.setattr(uploads, "MAX_NATIVE_BYTES", 200)

    async def run():
        offset = 0

        async def receive():
            nonlocal offset
            chunk = body[offset : offset + 128]
            offset += len(chunk)
            consumed.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}

        incoming = Request(
            {
                "type": "http",
                "headers": [
                    (b"content-type", request.headers["content-type"].encode()),
                ],
            },
            receive,
        )
        with pytest.raises(HTTPException) as error:
            await uploads.receive_upload(incoming, tmp_path, "sample.cbz", 10000)
        assert error.value.status_code == 413

    anyio.run(run)
    assert sum(consumed) < len(body) and not (tmp_path / "upload.cbz").exists()


pytestmark = pytest.mark.usefixtures("temporary_cleanup")


def test_starlette_multipart_internals_used_by_paired_parser_still_exist():
    """PairedParser overrides these MultiPartParser hooks and reads two private attributes."""
    import inspect

    from starlette.formparsers import MultiPartParser

    from mangacouch.uploads import PairedParser

    for hook in (
        "on_part_begin",
        "on_header_field",
        "on_header_value",
        "on_headers_finished",
        "on_part_data",
    ):
        assert callable(getattr(MultiPartParser, hook))
    source = inspect.getsource(MultiPartParser)
    assert "self._current_part" in source and "self._files_to_close_on_error" in source
    assert isinstance(PairedParser.spool_max_size, int)


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


def test_unauthorized_upload_is_rejected_before_reading_body(client: TestClient):
    consumed = []

    def chunks():
        consumed.append(True)
        yield b"should not be consumed"

    response = client.post("/api/import?filename=sample.zip", content=chunks(), headers=CSRF)
    assert response.status_code == 401 and consumed == []


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
        params={"expected_revision": 1},
        json={"metadata": {"title": "已修改", "language": "zh"}},
        headers=headers,
    )
    assert edit.status_code == 200 and "sidecar" not in edit.json()
    assert edit.json()["sidecar_sync"]["state"] in {"synced", "unchanged"}
    assert (
        client.put(
            f"/api/books/{identifier}/metadata",
            params={"expected_revision": 1},
            json={"metadata": {}},
            headers=headers,
        ).status_code
        == 409
    )
    assert (
        client.get("/api/books", params={"query": "修改"}, headers=headers).json()["items"][0]["id"]
        == identifier
    )
    state = client.put(
        f"/api/books/{identifier}/personal",
        params={"expected_revision": 1},
        json={"state": {"rating": 4.5, "resume_page": 1}},
        headers=headers,
    )
    assert state.status_code == 200
    assert (
        client.put(
            f"/api/books/{identifier}/personal",
            params={"expected_revision": 2},
            json={"state": {"resume_page": 99}},
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
        == "unchanged"
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
