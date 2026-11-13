from __future__ import annotations

import io
from pathlib import Path, PureWindowsPath

import anyio
from fastapi import HTTPException, Request
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser

from .eze import MAX_EZE_BYTES
from .files import CHUNK_SIZE, require_free_space
from .models import MAX_METADATA_BYTES

MAX_NATIVE_BYTES = MAX_METADATA_BYTES + 4096
MAX_FORM_OVERHEAD = 64 * 1024
UPLOAD_IDLE_SECONDS = 60
SPACE_CHECK_BYTES = 16 * CHUNK_SIZE
"""Free space is checked once per window of this many bytes, for the whole window ahead."""


def archive_name(filename: str) -> str:
    if (
        not filename
        or len(filename) > 4096
        or Path(filename).name != filename
        or PureWindowsPath(filename).drive
        or "\\" in filename
        or any(ord(c) < 32 for c in filename)
        or Path(filename).suffix.lower() not in {".zip", ".cbz", ".pdf"}
    ):
        raise HTTPException(422, "Invalid archive filename")
    return filename


class PairedParser(MultiPartParser):
    """Archive plus optional sidecars in one multipart body, with per-part limits.

    Relies on Starlette's MultiPartParser callbacks and its `_current_part` and
    `_files_to_close_on_error` attributes; pyproject pins Starlette to a tested major version and
    tests/test_upload.py checks these internals still exist.
    """

    def __init__(self, request: Request, filename: str, maximum: int, directory: Path):
        super().__init__(request.headers, request.stream(), max_files=3, max_fields=0)
        transport_name = "upload" + Path(filename).suffix.lower()
        self.directory = directory
        self.names = {
            "archive": transport_name,
            "native": transport_name + ".mangacouch.json",
            "eze": transport_name + ".eze.json",
        }
        self.limits = {"archive": maximum, "native": MAX_NATIVE_BYTES, "eze": MAX_EZE_BYTES}
        self.seen: set[str] = set()
        self.complete = False
        self.header_bytes = 0
        self.part_bytes = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self.part_bytes = 0

    def _header_size(self, size: int) -> None:
        self.header_bytes += size
        if self.header_bytes > MAX_FORM_OVERHEAD:
            raise HTTPException(413, "Multipart headers exceed limit")

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_size(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_size(end - start)
        super().on_header_value(data, start, end)

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        part = self._current_part
        name = part.field_name
        if name not in self.names or name in self.seen or part.file is None:
            raise HTTPException(422, "Expected one archive and optional native/Eze files")
        if part.file.filename != self.names[name]:
            raise HTTPException(422, "Invalid upload part filename")
        previous = part.file.file
        previous.close()
        self._files_to_close_on_error.remove(previous)  # pyright: ignore[reportArgumentType]
        target = self.directory / self.names[name]
        output = target.open("xb+") if name == "archive" else io.BytesIO()
        part.file.file = output
        self._files_to_close_on_error.append(output)  # pyright: ignore[reportArgumentType]
        self.seen.add(name)

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        self.part_bytes += end - start
        if self.part_bytes > self.limits[self._current_part.field_name]:
            raise HTTPException(413, "Uploaded file exceeds limit")
        super().on_part_data(data, start, end)

    def on_end(self) -> None:
        self.complete = True

    async def parse(self) -> FormData:
        try:
            form = await super().parse()
            if not self.complete or "archive" not in self.seen:
                raise HTTPException(400, "Incomplete multipart upload")
            return form
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


async def receive_upload(request: Request, directory: Path, filename: str, maximum: int) -> Path:
    archive_name(filename)
    paired = request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "multipart/form-data"
    total_limit = maximum + MAX_NATIVE_BYTES + MAX_EZE_BYTES + MAX_FORM_OVERHEAD if paired else maximum
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isascii() or not declared.isdecimal() or len(declared) > 20):
        raise HTTPException(400, "Invalid content length")
    if declared is not None and int(declared) > total_limit:
        raise HTTPException(413, "Upload exceeds configured limit")

    def check_space(incoming_bytes: int) -> None:
        require_free_space(directory, incoming_bytes)

    expected = int(declared) if declared is not None else 0
    await anyio.to_thread.run_sync(check_space, expected)
    received = 0

    async def stream():
        nonlocal received
        processed = checked = 0
        incoming = request.stream().__aiter__()
        while True:
            try:
                with anyio.fail_after(UPLOAD_IDLE_SECONDS):
                    chunk = await anext(incoming)
            except StopAsyncIteration:
                break
            except TimeoutError:
                raise HTTPException(408, "Upload stalled; retry the upload") from None
            received += len(chunk)
            if received > total_limit:
                raise HTTPException(413, "Upload exceeds configured limit")
            for offset in range(0, len(chunk), CHUNK_SIZE):
                block = chunk[offset : offset + CHUNK_SIZE]
                if processed + len(block) > checked:
                    ahead = (
                        SPACE_CHECK_BYTES
                        if declared is None
                        else min(SPACE_CHECK_BYTES, expected - processed)
                    )
                    ahead = max(ahead, len(block))
                    checked = processed + ahead
                    await anyio.to_thread.run_sync(check_space, ahead)
                processed += len(block)
                yield block
        if declared is not None and received != int(declared):
            raise HTTPException(400, "Upload length does not match content length")

    path = directory / ("upload" + Path(filename).suffix.lower())
    if not paired:
        async with await anyio.open_file(path, "xb") as output:
            async for chunk in stream():
                await output.write(chunk)
        return path
    parser = PairedParser(request, filename, maximum, directory)
    parser.stream = stream()
    try:
        form = await parser.parse()
    except MultiPartException as exc:
        raise HTTPException(400, "Invalid multipart upload") from exc
    try:
        targets = {
            "archive": path,
            "native": path.with_name(path.name + ".mangacouch.json"),
            "eze": path.with_name(path.name + ".eze.json"),
        }
        for name, item in form.multi_items():
            assert isinstance(item, UploadFile)
            if name == "archive":
                continue
            async with await anyio.open_file(targets[name], "xb") as output:
                written = checked = 0
                while chunk := await item.read(CHUNK_SIZE):
                    if written + len(chunk) > checked:
                        ahead = max(len(chunk), min(SPACE_CHECK_BYTES, (item.size or 0) - written))
                        checked = written + ahead
                        await anyio.to_thread.run_sync(require_free_space, directory, ahead)
                    written += len(chunk)
                    await output.write(chunk)
    finally:
        with anyio.CancelScope(shield=True):
            await form.close()
    return path
