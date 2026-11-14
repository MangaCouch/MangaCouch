from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from typing import BinaryIO
from urllib.parse import quote

import anyio
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from .database import Book
from .files import CHUNK_SIZE, ContentChanged, file_stamp, live_stamp
from .ingest import FileLibrary
from .models import Content, canonical_json


def archive_filename(content: Content) -> str:
    name = unicodedata.normalize("NFC", content.original_filename)
    name = "".join(
        "_" if character in '<>:"/\\|?*' or unicodedata.category(character) in {"Cc", "Cs"} else character
        for character in name
    ).strip(" .")
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    allowed = {"pdf"} if content.format == "pdf" else {"zip", "cbz"}
    if suffix in allowed:
        stem, extension = name.rsplit(".", 1)
    else:
        stem, extension = name, content.format
    stem = stem.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")
    stem = stem or f"book-{content.sha256[:12]}"
    if re.fullmatch(r"(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])", stem.split(".")[0].rstrip()):
        stem = "_" + stem
    return f"{stem}.{extension}"


def attachment(filename: str) -> str:
    return f"attachment; filename*=UTF-8''{quote(filename, safe='')}"


def _stamp(stream: BinaryIO) -> str:
    value = os.fstat(stream.fileno())
    if not stat.S_ISREG(value.st_mode):
        raise ContentChanged("Expected a regular archive file")
    return canonical_json((value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns))


def open_archive(files: FileLibrary, book: Book) -> tuple[BinaryIO, str]:
    files.root.check()
    state, stamp = files.availability(book)
    if state != "available" or stamp is None:
        raise ContentChanged(f"Book is {state}; download requires the registered content")
    path = files.root.resolve(book.content.relative_path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    stream = os.fdopen(descriptor, "rb")
    try:
        files.root.check()
        opened_stamp = _stamp(stream)
        if (
            canonical_json(json.loads(stamp)[:4]) != opened_stamp
            or canonical_json(live_stamp(path)) != opened_stamp
            or canonical_json(file_stamp(path)) != stamp
        ):
            raise ContentChanged("Archive changed before download")
        if os.fstat(stream.fileno()).st_size != book.content.byte_size:
            raise ContentChanged("Archive size differs from registered content")
        return stream, opened_stamp
    except BaseException:
        stream.close()
        raise


def read_chunk(stream: BinaryIO, stamp: str, count: int) -> bytes:
    if _stamp(stream) != stamp:
        raise ContentChanged("Archive changed during download")
    data = stream.read(min(CHUNK_SIZE, count))
    if _stamp(stream) != stamp or len(data) != min(CHUNK_SIZE, count):
        raise ContentChanged("Archive changed during download")
    return data


def byte_range(value: str, size: int) -> tuple[int, int] | None:
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value) if len(value) < 128 else None
    if match is None or not any(match.groups()):
        return None
    first, last = match.groups()
    if first:
        start, end = int(first), int(last) if last else size - 1
        if last and end < start:
            return None
        if start >= size:
            raise IndexError("Unsatisfiable range")
        return start, min(end, size - 1)
    count = int(last)
    if count == 0:
        raise IndexError("Unsatisfiable range")
    return max(0, size - count), size - 1


class ArchiveResponse(Response):
    def __init__(self, files: FileLibrary, book: Book, limiter: anyio.CapacityLimiter):
        super().__init__()
        self.files, self.book, self.limiter = files, book, limiter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        stream: BinaryIO | None = None
        stamp = ""
        try:
            opened, stamp = await anyio.to_thread.run_sync(
                lambda: open_archive(self.files, self.book), limiter=self.limiter
            )
            stream = opened
            size = self.book.content.byte_size
            etag = f'"{self.book.content.sha256}"'
            headers = {
                "Content-Disposition": attachment(archive_filename(self.book.content)),
                "Content-Length": str(size),
                "Accept-Ranges": "bytes",
                "ETag": etag,
                "Cache-Control": "no-store",
            }
            media_type = "application/pdf" if self.book.content.format == "pdf" else "application/zip"
            request_headers = {key.lower(): value for key, value in scope["headers"]}
            start, end, status = 0, size - 1, 200
            if scope["method"] == "GET" and request_headers.get(b"if-range", etag.encode()) == etag.encode():
                try:
                    selected = byte_range(request_headers.get(b"range", b"").decode("latin-1"), size)
                except IndexError:
                    await Response(
                        status_code=416,
                        headers={"Content-Range": f"bytes */{size}", "Cache-Control": "no-store"},
                    )(scope, receive, send)
                    return
                if selected is not None:
                    start, end = selected
                    status = 206
                    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
                    headers["Content-Length"] = str(end - start + 1)
            if scope["method"] == "HEAD":
                await Response(headers=headers, media_type=media_type)(scope, receive, send)
                return
            opened.seek(start)

            async def chunks():
                remaining = end - start + 1
                while remaining:
                    data = await anyio.to_thread.run_sync(
                        read_chunk, opened, stamp, remaining, limiter=self.limiter
                    )
                    remaining -= len(data)
                    yield data

            response = StreamingResponse(chunks(), status_code=status, headers=headers, media_type=media_type)
            await response(scope, receive, send)
        finally:
            if stream is not None:
                stream.close()
