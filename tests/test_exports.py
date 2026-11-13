from __future__ import annotations

import hashlib
import os
import zipfile
from contextlib import closing
from urllib.parse import unquote

import anyio
import pypdfium2 as pdfium
import pytest
from fastapi.testclient import TestClient
from helpers import login, make_zip
from starlette.requests import ClientDisconnect

from mangacouch import exports
from mangacouch.archives import PDFIUM_LOCK
from mangacouch.auth import Auth
from mangacouch.files import CHUNK_SIZE, ContentChanged
from mangacouch.models import Content


def imported(files, tmp_path, extension="zip"):
    source = tmp_path / f"原名 😀.{extension}"
    if extension == "pdf":
        with PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
            document.new_page(80, 120).close()
            document.save(source)
    else:
        make_zip(source)
    return files.import_file(source).book, source.read_bytes()


def disposition_name(response):
    header = response.headers["content-disposition"]
    assert header.startswith("attachment; filename*=UTF-8''")
    return unquote(header.split("''", 1)[1])


@pytest.mark.parametrize("extension", ["zip", "cbz", "pdf"])
def test_original_bytes_unicode_headers_and_head(client: TestClient, files, tmp_path, extension):
    book, original = imported(files, tmp_path, extension)
    login(client)
    url = f"/opds/books/{book.id}/archive"
    result = client.get(url)
    assert result.status_code == 200 and result.content == original
    assert hashlib.sha256(result.content).hexdigest() == book.content.sha256
    assert disposition_name(result) == f"原名 😀.{extension}"
    assert result.headers["cache-control"] == "no-store"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["accept-ranges"] == "bytes"
    head = client.head(url, headers={"Range": "bytes=1-2"})
    assert head.status_code == 200 and head.content == b""
    for header in ["content-length", "content-type", "content-disposition", "etag"]:
        assert head.headers[header] == result.headers[header]


@pytest.mark.parametrize(
    "header,part",
    [
        ("bytes=0-0", slice(0, 1)),
        ("bytes=2-7", slice(2, 8)),
        ("bytes=9-", slice(9, None)),
        ("bytes=-12", slice(-12, None)),
        ("bytes=3-999999", slice(3, None)),
        ("bytes=-999999", slice(None)),
    ],
)
def test_single_ranges_return_exact_original_bytes(client, files, tmp_path, header, part):
    book, original = imported(files, tmp_path)
    login(client)
    result = client.get(f"/opds/books/{book.id}/archive", headers={"Range": header})
    assert result.status_code == 206 and result.content == original[part]
    start, stop, _ = part.indices(len(original))
    assert result.headers["content-range"] == f"bytes {start}-{stop - 1}/{len(original)}"
    assert int(result.headers["content-length"]) == len(result.content)


@pytest.mark.parametrize(
    "header", ["bytes=0-1,3-4", "items=0-1", "bytes=-", "bytes=5-2", "bytes=" + "9" * 200 + "-"]
)
def test_unsupported_or_invalid_range_is_ignored(client, files, tmp_path, header):
    book, original = imported(files, tmp_path)
    login(client)
    result = client.get(f"/opds/books/{book.id}/archive", headers={"Range": header})
    assert result.status_code == 200 and result.content == original
    assert "content-range" not in result.headers


@pytest.mark.parametrize("header", ["bytes=999999-", "bytes=-0"])
def test_unsatisfiable_range_has_no_archive_body(client, files, tmp_path, header):
    book, original = imported(files, tmp_path)
    login(client)
    result = client.get(f"/opds/books/{book.id}/archive", headers={"Range": header})
    assert result.status_code == 416 and not result.content
    assert result.headers["content-range"] == f"bytes */{len(original)}"


def test_resume_requires_matching_if_range(client, files, tmp_path):
    book, original = imported(files, tmp_path)
    login(client)
    url = f"/opds/books/{book.id}/archive"
    etag = client.head(url).headers["etag"]
    for value, status, expected in [
        (etag, 206, original[2:]),
        ('"stale"', 200, original),
        ("W/" + etag, 200, original),
    ]:
        result = client.get(url, headers={"Range": "bytes=2-", "If-Range": value})
        assert result.status_code == status and result.content == expected


def test_archive_downloads_require_auth_and_allow_readonly_keys(client, files, auth: Auth, tmp_path):
    book, _ = imported(files, tmp_path)
    key_id, token = auth.create_key("readonly", "export fixture")
    url = f"/opds/books/{book.id}/archive"
    assert client.get(url).status_code == client.head(url).status_code == 401
    assert client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.head(url, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    auth.revoke(key_id)
    assert client.get(url, headers={"Authorization": f"Bearer {token}"}).status_code == 401
    login(client)
    assert client.get(url, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get(url, headers={"Host": "wrong.example"}).status_code == 400
    assert client.get("/opds/books/absent/archive").status_code == 404
    assert client.get(f"/api/exports/{book.id}/archive").status_code == 404


@pytest.mark.parametrize("failure", ["missing", "changed", "symlink", "offline"])
@pytest.mark.usefixtures("without_maintenance")
def test_unavailable_archive_is_rejected_before_download(client, files, tmp_path, failure):
    book, _ = imported(files, tmp_path)
    login(client)
    path = files.root.resolve(book.content.relative_path)
    if failure == "offline":
        files.root.path.rename(files.root.path.with_name("offline"))
    elif failure == "changed":
        path.write_bytes(b"wrong content")
    else:
        path.unlink()
        if failure == "symlink":
            path.symlink_to(make_zip(tmp_path / "other.zip"))
    result = client.get(f"/opds/books/{book.id}/archive")
    assert result.status_code == (503 if failure == "offline" else 409)
    assert "content-disposition" not in result.headers
    assert files.library.get_book(book.id) == book


@pytest.mark.parametrize(
    "name",
    [
        "CON.zip",
        "LPT¹.cbz",
        'a/b\\c\r\n"?.zip',
        "...",
        "😀" * 200 + ".cbz",
        "日本語 résumé.zip",
        "NUL .zip",
        "bad.html",
    ],
)
def test_download_names_are_portable_unicode_and_pair_with_native(name):
    content = Content(
        sha256="a" * 64,
        format="cbz",
        relative_path="a.zip",
        original_filename=name,
        byte_size=1,
        page_count=1,
    )
    filename = exports.archive_filename(content)
    assert len((filename + ".mangacouch.json").encode()) <= 255
    assert not any(c in filename for c in '<>:"/\\|?*\r\n')
    assert filename.split(".")[0].rstrip().upper() not in {"CON", "NUL", "LPT¹"}
    assert filename.endswith((".zip", ".cbz"))
    assert exports.attachment(filename).isascii()
    if name.startswith("日本語"):
        assert filename == name


def test_replaced_path_keeps_open_descriptor_on_registered_bytes(files, tmp_path):
    book, original = imported(files, tmp_path)
    stream, stamp = exports.open_archive(files, book)
    try:
        path = files.root.resolve(book.content.relative_path)
        replacement = make_zip(tmp_path / "replacement.zip", ["different.png"])
        if os.name == "nt":
            with pytest.raises(PermissionError):
                os.replace(replacement, path)
        else:
            os.replace(replacement, path)
        assert exports.read_chunk(stream, stamp, len(original)) == original
    finally:
        stream.close()
    if os.name == "nt":
        os.replace(replacement, path)
    with pytest.raises(ContentChanged):
        exports.open_archive(files, book)


def test_replacement_between_validation_and_open_is_rejected(files, tmp_path, monkeypatch):
    book, _ = imported(files, tmp_path)
    original = files.availability

    def replace(book):
        result = original(book)
        files.root.resolve(book.content.relative_path).write_bytes(b"replaced")
        return result

    monkeypatch.setattr(files, "availability", replace)
    with pytest.raises(ContentChanged):
        exports.open_archive(files, book)


def test_cancellation_while_opening_closes_handle(files, tmp_path, monkeypatch):
    book, _ = imported(files, tmp_path)
    opened = []
    original_open = exports.open_archive

    async def run():
        async with anyio.create_task_group() as group:

            def cancel_after_open(*args):
                stream, stamp = original_open(*args)
                opened.append(stream)
                anyio.from_thread.run_sync(group.cancel_scope.cancel)
                return stream, stamp

            monkeypatch.setattr(exports, "open_archive", cancel_after_open)

            async def receive():
                return {"type": "http.disconnect"}

            async def send(_message):
                await anyio.lowlevel.checkpoint()

            response = exports.ArchiveResponse(files, book, anyio.CapacityLimiter(2))
            group.start_soon(
                response,
                {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}},
                receive,
                send,
            )

    anyio.run(run)
    assert len(opened) == 1 and opened[0].closed


def test_head_and_range_error_close_handle_and_do_not_require_root_writes(
    client, files, tmp_path, monkeypatch
):
    book, _ = imported(files, tmp_path)
    opened = []
    original_open, original_check = exports.open_archive, files.root.check

    def check(*, writable=False):
        assert not writable
        return original_check()

    def track(*args):
        stream, stamp = original_open(*args)
        opened.append(stream)
        return stream, stamp

    monkeypatch.setattr(files.root, "check", check)
    monkeypatch.setattr(exports, "open_archive", track)
    login(client)
    assert client.head(f"/opds/books/{book.id}/archive").status_code == 200
    assert client.get(f"/opds/books/{book.id}/archive", headers={"Range": "bytes=999999-"}).status_code == 416
    assert len(opened) == 2 and all(stream.closed for stream in opened)


@pytest.mark.parametrize("failure", ["none", "disconnect", "mutation"])
def test_stream_is_bounded_and_closes_on_completion_disconnect_or_mutation(
    files, tmp_path, monkeypatch, failure
):
    source = make_zip(tmp_path / "large.zip")
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("padding.bin", b"x" * (CHUNK_SIZE * 3 + 17))
    book = files.import_file(source).book
    opened = []
    original_open = exports.open_archive

    def track(*args):
        stream, stamp = original_open(*args)
        opened.append(stream)
        return stream, stamp

    monkeypatch.setattr(exports, "open_archive", track)
    chunks, starts, digest = [], [], hashlib.sha256()

    async def run():
        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.start":
                starts.append(message["status"])
            elif message.get("body"):
                data = message["body"]
                chunks.append(len(data))
                digest.update(data)
                if failure == "disconnect":
                    raise OSError("fixture disconnected")
                if failure == "mutation" and len(chunks) == 1:
                    with files.root.resolve(book.content.relative_path).open("r+b") as writer:
                        writer.truncate(10)

        response = exports.ArchiveResponse(files, book, anyio.CapacityLimiter(2))
        await response(
            {"type": "http", "method": "GET", "headers": [], "asgi": {"spec_version": "2.4"}}, receive, send
        )

    if failure == "none":
        anyio.run(run)
        assert sum(chunks) == book.content.byte_size and digest.hexdigest() == book.content.sha256
        assert len(chunks) >= 4
    else:
        with pytest.raises(ClientDisconnect):
            anyio.run(run)
        assert len(chunks) == 1
    assert starts == [200] and max(chunks) <= CHUNK_SIZE
    assert len(opened) == 1 and opened[0].closed
