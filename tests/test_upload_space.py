from __future__ import annotations

import errno
import shutil
from pathlib import Path

import anyio
import httpx
import pytest
from helpers import CSRF, login
from starlette.requests import Request

from mangacouch import uploads


@pytest.mark.parametrize("paired", [False, True])
def test_low_temporary_space_rejects_before_reading_request(tmp_path, monkeypatch, paired):
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.total, 0))
    headers = [(b"content-length", b"100")]
    if paired:
        headers.append((b"content-type", b"multipart/form-data; boundary=test"))
    consumed = []

    async def receive():
        consumed.append(True)
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    async def run():
        with pytest.raises(OSError) as error:
            await uploads.receive_upload(
                Request({"type": "http", "headers": headers}, receive), tmp_path, "sample.cbz", 10000
            )
        assert error.value.errno == errno.ENOSPC

    anyio.run(run)
    assert consumed == [] and not list(tmp_path.iterdir())


@pytest.mark.parametrize("paired", [False, True])
def test_declared_upload_space_includes_all_temporary_copies(tmp_path, monkeypatch, paired):
    payload = b"archive payload"
    request = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", payload)})
    body = request.read() if paired else payload
    headers = [(b"content-length", str(len(body)).encode())]
    if paired:
        headers.append((b"content-type", request.headers["content-type"].encode()))
    usage = shutil.disk_usage(tmp_path)
    needed = len(body)
    from mangacouch.files import SPACE_MARGIN

    free = needed + SPACE_MARGIN - 1
    monkeypatch.setattr(shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.total - free, free))
    consumed = []

    async def receive():
        consumed.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        nonlocal free
        with pytest.raises(OSError) as error:
            await uploads.receive_upload(
                Request({"type": "http", "headers": headers}, receive), tmp_path, "sample.cbz", 10000
            )
        assert error.value.errno == errno.ENOSPC and not consumed
        free += 1
        result = await uploads.receive_upload(
            Request({"type": "http", "headers": headers}, receive), tmp_path, "sample.cbz", 10000
        )
        assert result.read_bytes() == payload

    anyio.run(run)


@pytest.mark.parametrize("low", ["destination", None])
def test_multipart_checks_separate_temporary_volumes(tmp_path, monkeypatch, low):
    import os

    from mangacouch.files import SPACE_MARGIN

    source = tmp_path / "spool"
    destination = tmp_path / "request"
    source.mkdir()
    destination.mkdir()
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == source:
            fields = list(value)
            fields[2] += 1
            return os.stat_result(fields)
        return value

    monkeypatch.setattr(Path, "stat", stat)
    request = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", b"archive")})
    body = request.read()
    usage = shutil.disk_usage(tmp_path)
    checked = []

    def disk_usage(path):
        checked.append(path)
        if path == source:
            free = SPACE_MARGIN + len(body) + 3 * uploads.PairedParser.spool_max_size
            free -= int(low == "spool")
        else:
            assert path == destination
            free = SPACE_MARGIN + len(body) - int(low == "destination")
        return type(usage)(usage.total, usage.total - free, free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    consumed = []

    async def receive():
        consumed.append(True)
        return {"type": "http.request", "body": body, "more_body": False}

    async def run():
        incoming = Request(
            {
                "type": "http",
                "headers": [
                    (b"content-type", request.headers["content-type"].encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            },
            receive,
        )
        if low:
            with pytest.raises(OSError) as error:
                await uploads.receive_upload(incoming, destination, "sample.cbz", 10000)
            assert error.value.errno == errno.ENOSPC and not consumed
        else:
            result = await uploads.receive_upload(incoming, destination, "sample.cbz", 10000)
            assert result.read_bytes() == b"archive"

    anyio.run(run)
    assert source not in checked
    assert destination in checked


@pytest.mark.parametrize("paired", [False, True])
def test_unknown_length_rechecks_space_and_stops_early(tmp_path, monkeypatch, paired, temporary_cleanup):
    from tempfile import TemporaryDirectory

    request = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", b"x" * 10000)})
    body = request.read() if paired else b"x" * 10000
    headers = [(b"content-type", request.headers["content-type"].encode())] if paired else []
    usage = shutil.disk_usage(tmp_path)
    checks = 0
    consumed = []

    def disk_usage(_path):
        nonlocal checks
        checks += 1
        free = usage.free if checks < 4 else 0
        return type(usage)(usage.total, usage.total - free, free)

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(uploads.PairedParser, "spool_max_size", 32)
    monkeypatch.setattr(uploads, "SPACE_CHECK_BYTES", 256)

    async def run(directory):
        offset = 0

        async def receive():
            nonlocal offset
            chunk = body[offset : offset + 256]
            offset += len(chunk)
            consumed.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": offset < len(body)}

        with pytest.raises(OSError) as error:
            await uploads.receive_upload(
                Request({"type": "http", "headers": headers}, receive), directory, "sample.cbz", 20000
            )
        assert error.value.errno == errno.ENOSPC

    with TemporaryDirectory(dir=tmp_path) as directory:
        anyio.run(run, Path(directory))
    assert not list(tmp_path.iterdir()) and 0 < sum(consumed) < len(body)
    if paired:
        assert temporary_cleanup and all(not handle._rolled for handle in temporary_cleanup)


@pytest.mark.parametrize("number, expected", [(errno.ENOSPC, 507), (errno.EDQUOT, 507), (errno.EACCES, 503)])
def test_temporary_write_errors_report_space_or_access_and_cleanup(
    client, files, tmp_path, monkeypatch, number, expected
):
    from helpers import parts

    data = parts(tmp_path)
    login(client)

    async def fail(*_args, **_kwargs):
        raise OSError(number, "injected write failure")

    monkeypatch.setattr(uploads.anyio, "open_file", fail)
    result = client.post("/api/import?filename=sample.cbz", files=data, headers=CSRF)
    assert result.status_code == expected
    assert result.json()["error"] == ("insufficient_storage" if expected == 507 else "storage_unavailable")
    assert files.library.search("") == []
    assert not list(files.root.path.rglob("*.part")) and not list(files.root.path.rglob("*.cbz"))


def test_library_volume_space_is_checked_independently_before_copy(client, files, tmp_path, monkeypatch):
    from helpers import parts

    data = parts(tmp_path)
    login(client)
    usage = shutil.disk_usage(tmp_path)
    seen = []

    def disk_usage(path):
        seen.append(path)
        return type(usage)(usage.total, usage.total, 0) if path == files.root.path else usage

    def cross_device(*_args, **_kwargs):
        raise OSError(errno.EXDEV, "cross-device fixture")

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    # Same-device uploads are linked into staging; a copy (other device) needs library space first.
    monkeypatch.setattr("mangacouch.ingest.os.link", cross_device)
    result = client.post("/api/import?filename=sample.cbz", files=data, headers=CSRF)
    assert result.status_code == 507 and files.root.path in seen
    assert files.library.search("") == []
    assert not list(files.root.path.rglob("*.part")) and not list(files.root.path.rglob("*.cbz"))


def test_space_loss_while_copying_parsed_files_closes_spools(client, files, tmp_path, monkeypatch):
    from helpers import parts

    data = parts(tmp_path)
    login(client)
    usage = shutil.disk_usage(tmp_path)
    original_open = uploads.anyio.open_file
    copying = False

    async def open_file(*args, **kwargs):
        nonlocal copying
        copying = True
        return await original_open(*args, **kwargs)

    monkeypatch.setattr(uploads.anyio, "open_file", open_file)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.total, 0) if copying else usage
    )
    result = client.post("/api/import?filename=sample.cbz", files=data, headers=CSRF)
    assert result.status_code == 507 and files.library.search("") == []


def test_invalid_declared_size_rejected_before_space_queries(tmp_path, monkeypatch):
    def disk_usage(_path):
        pytest.fail("Size validation must precede disk access")

    monkeypatch.setattr(shutil, "disk_usage", disk_usage)
    from fastapi import HTTPException

    async def run():
        with pytest.raises(HTTPException) as error:
            await uploads.receive_upload(
                Request({"type": "http", "headers": [(b"content-length", b"10001")]}),
                tmp_path,
                "sample.cbz",
                10000,
            )
        assert error.value.status_code == 413

    anyio.run(run)


@pytest.mark.parametrize("paired", [False, True])
def test_stalled_upload_times_out_and_closes_spools(tmp_path, monkeypatch, paired, temporary_cleanup):
    from tempfile import TemporaryDirectory

    from fastapi import HTTPException

    monkeypatch.setattr(uploads, "UPLOAD_IDLE_SECONDS", 0.05)
    monkeypatch.setattr(uploads.PairedParser, "spool_max_size", 32)
    packed = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", b"x" * 10000)})
    body = packed.read() if paired else b"x" * 10000
    headers = [(b"content-type", packed.headers["content-type"].encode())] if paired else []

    async def run(directory):
        started = False

        async def receive():
            nonlocal started
            if started:
                await anyio.sleep_forever()
            started = True
            return {"type": "http.request", "body": body[:1000], "more_body": True}

        with pytest.raises(HTTPException) as error:
            await uploads.receive_upload(
                Request({"type": "http", "headers": headers}, receive), directory, "sample.cbz", 20000
            )
        assert error.value.status_code == 408

    with TemporaryDirectory(dir=tmp_path) as directory:
        anyio.run(run, Path(directory))
    assert not list(tmp_path.iterdir())
    if paired:
        assert temporary_cleanup and all(handle.closed for handle in temporary_cleanup)


def test_upload_timeout_resets_for_each_received_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_IDLE_SECONDS", 0.2)

    async def run():
        remaining = 4

        async def receive():
            nonlocal remaining
            await anyio.sleep(0.08)
            remaining -= 1
            return {"type": "http.request", "body": b"chunk", "more_body": remaining > 0}

        result = await uploads.receive_upload(
            Request({"type": "http", "headers": []}, receive), tmp_path, "sample.cbz", 20000
        )
        assert result.read_bytes() == b"chunk" * 4

    anyio.run(run)


@pytest.mark.parametrize("paired", [False, True])
def test_stalled_uploads_release_both_slots_for_a_waiting_import(client, auth, tmp_path, monkeypatch, paired):
    import asyncio

    from helpers import make_zip

    monkeypatch.setattr(uploads, "UPLOAD_IDLE_SECONDS", 0.1)
    payload = make_zip(tmp_path / "upload.cbz").read_bytes()
    packed = httpx.Request("POST", "http://test", files={"archive": ("upload.cbz", payload)})
    body = packed.read() if paired else payload
    headers = {"Content-Type": packed.headers["content-type"]} if paired else {}
    _, key = auth.create_key("management", "stalled-upload-test")

    async def run():
        started = 0
        both_started = asyncio.Event()

        async def stalled():
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            yield body[:100]
            await asyncio.Event().wait()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client.app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer " + key},
        ) as connection:
            first = [
                asyncio.create_task(
                    connection.post("/api/import?filename=stalled.cbz", content=stalled(), headers=headers)
                )
                for _ in range(2)
            ]
            await asyncio.wait_for(both_started.wait(), 2)
            waiting = asyncio.create_task(connection.post("/api/import?filename=valid.cbz", content=payload))
            responses = await asyncio.wait_for(asyncio.gather(*first, waiting), 5)
            assert [response.status_code for response in responses] == [408, 408, 200]

    asyncio.run(run())


pytestmark = pytest.mark.usefixtures("temporary_cleanup")
