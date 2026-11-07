from __future__ import annotations

import json
import sqlite3
from unittest.mock import patch

import anyio
import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, login, parts
from starlette.requests import ClientDisconnect, Request

from mangacouch import http, uploads
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary
from mangacouch.models import PersonalState
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync


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
    assert not SidecarSync(files).path(book).exists()


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
    state = PersonalState(rating=4.5, resume_page=1, bookmarks=[0])
    files.library.save_personal(identifier, state, expected_revision=1)
    metadata = files.library.get_book(identifier).metadata.model_copy(update={"title": "Manual"})
    Review(files).edit(identifier, metadata, expected_revision=1)
    before = files.library.get_book(identifier)
    result = client.post("/api/import", params={"filename": "作品😀.cbz"}, files=data, headers=CSRF)
    assert not result.json()["created"] and result.json()["book"]["id"] == identifier
    assert "existing_book_metadata_preserved" in result.json()["warnings"]
    assert files.library.get_book(identifier) == before


@pytest.mark.parametrize("kind", ["wrong_name", "duplicate", "unknown", "field", "no_archive", "four_files"])
def test_bad_multipart_structure_is_rejected_before_import(client, files, tmp_path, kind):
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
    assert result.status_code in {400, 422}
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


@pytest.mark.parametrize("sqlite_code,status", [(sqlite3.SQLITE_IOERR, 503), (sqlite3.SQLITE_FULL, 507)])
def test_failed_commit_recovery_uses_uploaded_metadata_after_request_cleanup(
    client, files, tmp_path, monkeypatch, sqlite_code, status
):
    def fail(*_args):
        error = sqlite3.OperationalError("injected commit storage failure")
        error.sqlite_errorcode = sqlite_code
        raise error

    login(client)
    with monkeypatch.context() as patch:
        patch.setattr(files, "_commit_import", fail)
        result = client.post(
            "/api/import", params={"filename": "作品😀.cbz"}, files=parts(tmp_path), headers=CSRF
        )
    assert result.status_code == status and files.library.search("") == []
    assert result.json() == {"error": "insufficient_storage" if status == 507 else "storage_unavailable"}
    recovered = FileLibrary(Library(files.library.path), files.root.path)
    assert list(recovered.recover().values()) == ["recovered"]
    book = recovered.library.get_book(recovered.library.search("")[0])
    assert book.metadata.title == "本地标题" and book.content.original_filename == "作品😀.cbz"
    assert "eze:info" in Review(recovered).preview(book.id)["candidates"]
    assert recovered.page_bytes(book.id, 0).startswith(b"\x89PNG")


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
