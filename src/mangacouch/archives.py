from __future__ import annotations

import lzma
import math
import os
import re
import stat
import struct
import sys
import threading
import zipfile
import zlib
from collections import OrderedDict
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pyvips

from .errors import InvalidInput
from .models import Model, normalized

pyvips.cache_set_max(0)

PDFIUM_LOCK = threading.RLock()
THUMBNAIL_WIDTH = 480
THUMBNAIL_HEIGHT = 720
IMAGE_VERSION = 1
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
type ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
type ThumbnailFormat = Literal["webp", "jpeg"]


class InvalidArchive(InvalidInput):
    """A damaged or unsupported archive (API code `invalid_archive`)."""

    code = "invalid_archive"


class ImportCancelled(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int | None = None
    max_page_bytes: int = 128 * 1024**2
    max_unpacked_bytes: int = 32 * 1024**3
    max_pixels: int = 100_000_000
    max_dimension: int = 32_768
    max_render_pixels: int = 16_000_000
    max_pages: int = 20_000
    max_entries: int = 50_000
    max_directory_bytes: int = 32 * 1024**2

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(
                    "Resource limits must be positive integers; None means no archive byte limit"
                )


class Page(Model):
    name: str
    width: int
    height: int
    media_type: ImageMediaType
    byte_size: int = 0


def check_cancel(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ImportCancelled("Import cancelled before publication")


def _image(data: bytes, limits: Limits) -> Any:
    image_media_type(data)
    image: Any = pyvips.Image.new_from_buffer(data, "", access="sequential", fail_on="error")
    _check_dimensions(image.width, image.height, image.bands, limits)
    return image


def _check_dimensions(width: int, height: int, bands: int, limits: Limits) -> None:
    if (
        not 0 < width * height <= limits.max_pixels
        or not 1 <= bands <= 4
        or max(width, height) > limits.max_dimension
    ):
        raise InvalidArchive("Page exceeds the supported image dimensions")


def _zip_directory_limit(path: Path, limits: Limits) -> None:
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise InvalidArchive("ZIP directory is missing")
        _, disk, start_disk, on_disk, entries, directory_size, _, comment_length = struct.unpack(
            "<4s4H2LH", tail[offset : offset + 22]
        )
        if offset + 22 + comment_length != len(tail) or disk or start_disk:
            raise InvalidArchive("Unsupported ZIP directory or multipart archive")
        if entries == 65535 or directory_size == 0xFFFFFFFF:
            locator_offset = max(0, size - 65557) + offset - 20
            if locator_offset < 0:
                raise InvalidArchive("ZIP64 locator is missing")
            stream.seek(locator_offset)
            signature, disk64, position, disks = struct.unpack("<4sLQL", stream.read(20))
            if signature != b"PK\x06\x07" or disk64 or disks != 1:
                raise InvalidArchive("Unsupported ZIP64 locator")
            stream.seek(position)
            record = stream.read(56)
            if len(record) != 56 or record[:4] != b"PK\x06\x06":
                raise InvalidArchive("Invalid ZIP64 directory")
            _, _, _, _, disk, start_disk, on_disk, entries, directory_size, _ = struct.unpack(
                "<4sQ2H2L4Q", record
            )
        if disk or start_disk or on_disk != entries:
            raise InvalidArchive("Multipart ZIP is unsupported")
        if entries > limits.max_entries or directory_size > limits.max_directory_bytes:
            raise InvalidArchive("ZIP directory exceeds configured limits")


def zip_image_bytes(path: Path, limits: Limits) -> int:
    _zip_directory_limit(path, limits)
    try:
        with zipfile.ZipFile(path) as archive:
            return sum(page.file_size for page in _zip_pages(archive, limits))
    except zipfile.BadZipFile:
        raise InvalidArchive("Invalid ZIP directory") from None


def _unsafe_name(entry: zipfile.ZipInfo) -> bool:
    name = entry.filename.rstrip("/")
    return (
        not name
        or PurePosixPath(name).is_absolute()
        or bool(PureWindowsPath(name).drive)
        or "\\" in name
        or ".." in PurePosixPath(name).parts
        or any(ord(c) < 32 for c in name)
        or stat.S_ISLNK(entry.external_attr >> 16)
    )


def _zip_pages(archive: zipfile.ZipFile, limits: Limits) -> list[zipfile.ZipInfo]:
    """Image members in reading order. Members are never extracted, only read by name, so odd
    names (backslashes, symlink entries, absolute paths) are skipped rather than fatal; duplicate
    names, encryption and size limits still reject the archive."""
    pages = []
    seen = set()
    total = 0
    for entry in archive.infolist():
        name = entry.filename.rstrip("/")
        if name in seen:
            raise InvalidArchive("Ambiguous duplicate ZIP member name")
        seen.add(name)
        total += entry.file_size
        if entry.file_size > limits.max_page_bytes or total > limits.max_unpacked_bytes:
            raise InvalidArchive("ZIP expansion exceeds configured limits")
        if entry.flag_bits & 1:
            raise InvalidArchive("Encrypted ZIP is unsupported")
        parts = PurePosixPath(name).parts
        if (
            not entry.is_dir()
            and not _unsafe_name(entry)
            and not any(p.startswith(".") or p == "__MACOSX" for p in parts)
            and Path(name).suffix.lower() in IMAGE_SUFFIXES
        ):
            pages.append(entry)
    if not 0 < len(pages) <= limits.max_pages:
        raise InvalidArchive("Archive has no readable pages or exceeds the page limit")

    def order(entry: zipfile.ZipInfo) -> tuple:
        parts = re.split(r"(\d+)", normalized(entry.filename))
        return tuple((1, int(p)) if p.isdecimal() else (0, p) for p in parts), entry.filename

    return sorted(pages, key=order)


# Cache only immutable directory metadata, never file handles (including on Windows).
_DIRECTORY_BUDGET = 32 * 1024**2
_DIRECTORY_CACHE: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
_DIRECTORY_LOCK = threading.Lock()


class _PageZip(zipfile.ZipFile):
    def __init__(self, path: Path, limits: Limits):
        self.limits = limits
        self.pages: dict[str, zipfile.ZipInfo] = {}
        super().__init__(path, mode="r")

    def _RealGetContents(self):
        # CPython's read-mode directory hook. The archive regression tests cover its
        # contract; all member reads, local-header checks and CRC checks stay in zipfile.
        assert self.fp is not None
        stamp = os.fstat(self.fp.fileno())
        key = (
            self.filename,
            stamp.st_dev,
            stamp.st_ino,
            stamp.st_size,
            stamp.st_mtime_ns,
            stamp.st_ctime_ns,
            self.limits,
        )
        with _DIRECTORY_LOCK:
            cached = _DIRECTORY_CACHE.get(key)
            if cached is not None:
                _DIRECTORY_CACHE.move_to_end(key)
                for name, value in cached[0].items():
                    setattr(self, name, value)
                return
        super()._RealGetContents()  # pyright: ignore[reportAttributeAccessIssue]
        self.pages = {entry.filename: entry for entry in _zip_pages(self, self.limits)}
        fields = {
            name: getattr(self, name) for name in ("filelist", "NameToInfo", "start_dir", "_comment", "pages")
        }
        size = sum(sys.getsizeof(value) for value in fields.values()) + sum(
            sys.getsizeof(entry)
            + sys.getsizeof(entry.filename)
            + sys.getsizeof(entry.extra)
            + sys.getsizeof(entry.comment)
            + 256
            for entry in self.filelist
        )
        if size <= _DIRECTORY_BUDGET:
            with _DIRECTORY_LOCK:
                _DIRECTORY_CACHE[key] = (fields, size)
                _DIRECTORY_CACHE.move_to_end(key)
                while (
                    len(_DIRECTORY_CACHE) > 64
                    or sum(item[1] for item in _DIRECTORY_CACHE.values()) > _DIRECTORY_BUDGET
                ):
                    _DIRECTORY_CACHE.popitem(last=False)


def _pdf_size(page: Any, limits: Limits) -> tuple[int, int]:
    width, height = page.get_size()
    if not all(math.isfinite(v) and v > 0 for v in (width, height)):
        raise InvalidArchive("Invalid PDF page dimensions")
    if width * height > limits.max_pixels or max(width, height) > limits.max_dimension:
        raise InvalidArchive("PDF page exceeds configured dimensions")
    for obj in page.get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=64):
        image_width, image_height = obj.get_px_size()
        if (
            image_width * image_height > limits.max_pixels
            or max(image_width, image_height) > limits.max_dimension
        ):
            raise InvalidArchive("PDF embedded image exceeds configured dimensions")
    return math.ceil(width), math.ceil(height)


def inspect_archive(
    path: Path,
    archive_format: str,
    limits: Limits,
    cancelled: Callable[[], bool] | None = None,
) -> list[Page]:
    try:
        if archive_format in {"zip", "cbz"}:
            _zip_directory_limit(path, limits)
            with zipfile.ZipFile(path) as archive:
                result = []
                for entry in _zip_pages(archive, limits):
                    check_cancel(cancelled)
                    data = archive.read(entry)
                    image = _image(data, limits)
                    image.avg()
                    result.append(
                        Page(
                            name=entry.filename,
                            width=image.width,
                            height=image.height,
                            media_type=image_media_type(data),
                            byte_size=len(data),
                        )
                    )
                images = {page.name for page in result}
                for entry in archive.infolist():
                    if not entry.is_dir() and entry.filename not in images:
                        with archive.open(entry) as stream:
                            while stream.read(1024**2):
                                check_cancel(cancelled)
                return result
        if archive_format != "pdf":
            raise InvalidArchive("Expected ZIP, CBZ or PDF")
        # pdfium is not thread-safe, but other readers only need the lock between our pages.
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(path)
        try:
            with PDFIUM_LOCK:
                count = len(document)
            if not 0 < count <= limits.max_pages:
                raise InvalidArchive("PDF has no pages or exceeds the page limit")
            result = []
            for index in range(count):
                check_cancel(cancelled)
                with PDFIUM_LOCK, closing(document[index]) as page:
                    width, height = _pdf_size(page, limits)
                    with closing(page.render(scale=min(1, 128 / max(width, height)))):  # pyright: ignore[reportArgumentType]
                        pass
                result.append(Page(name=str(index), width=width, height=height, media_type="image/webp"))
            return result
        finally:
            with PDFIUM_LOCK:
                document.close()
    except (
        zipfile.BadZipFile,
        struct.error,
        pyvips.Error,
        pdfium.PdfiumError,
        RuntimeError,
        zlib.error,
        lzma.LZMAError,
        EOFError,
    ) as error:
        raise InvalidArchive("Archive could not be decoded") from error


def image_media_type(data: bytes) -> ImageMediaType:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise InvalidArchive("Expected GIF, JPEG, WebP or PNG")


def _thumbnail(data: bytes, limits: Limits) -> Any:
    """A page scaled into the thumbnail box; JPEG/WebP shrink while decoding (shrink-on-load)."""
    image_media_type(data)
    header: Any = pyvips.Image.new_from_buffer(data, "", access="sequential")
    _check_dimensions(header.width, header.height, header.bands, limits)
    return pyvips.Image.thumbnail_buffer(
        data, THUMBNAIL_WIDTH, height=THUMBNAIL_HEIGHT, size="down", no_rotate=True, fail_on="error"
    )


def _encode_page(image: Any, thumbnail: ThumbnailFormat | None) -> bytes:
    if thumbnail is None:
        return image.webpsave_buffer(lossless=True, effort=3, strip=True)
    scale = min(1, THUMBNAIL_WIDTH / image.width, THUMBNAIL_HEIGHT / image.height)
    if scale < 1:
        image = image.resize(scale)
    image = image.colourspace("srgb")
    if thumbnail == "jpeg":
        if image.hasalpha():
            image = image.flatten(background=[255, 255, 255])
        return image.jpegsave_buffer(Q=80, strip=True, optimize_coding=True)
    return image.webpsave_buffer(Q=80, effort=3, lossless=False, strip=True)


def _read_page(
    path: Path, archive_format: str, page: Page, limits: Limits, *, thumbnail: ThumbnailFormat | None = None
) -> bytes:
    if archive_format in {"zip", "cbz"}:
        _zip_directory_limit(path, limits)
        with _PageZip(path, limits) as archive:
            entry = archive.pages.get(page.name)
            if entry is None:
                raise InvalidArchive("Page is no longer present")
            data = archive.read(entry)
            if thumbnail is None:
                return data
            return _encode_page(_thumbnail(data, limits), thumbnail)
    with (
        PDFIUM_LOCK,
        closing(pdfium.PdfDocument(path)) as document,
        closing(document[int(page.name)]) as pdf_page,
    ):
        width, height = _pdf_size(pdf_page, limits)
        density = 2.0
        objects = list(pdf_page.get_objects(max_depth=1))
        if len(objects) == 1 and isinstance(objects[0], pdfium.PdfImage):
            obj = objects[0]
            matrix = obj.get_matrix()
            rendered_width, rendered_height = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
            if rendered_width >= width * 0.95 and rendered_height >= height * 0.95:
                pixels_w, pixels_h = obj.get_px_size()
                density = max(pixels_w / rendered_width, pixels_h / rendered_height)
        scale = min(
            density,
            min(limits.max_dimension, 16383) / max(width, height),
            math.sqrt(limits.max_render_pixels / (width * height)),
        )
        if thumbnail:
            scale = min(scale, 1, THUMBNAIL_WIDTH / width, THUMBNAIL_HEIGHT / height)
        while math.ceil(width * scale) * math.ceil(height * scale) > limits.max_render_pixels:
            scale *= 0.99
        with closing(pdf_page.render(scale=scale, rev_byteorder=True)) as bitmap:  # pyright: ignore[reportArgumentType]
            array = bitmap.to_numpy()
            height, width = array.shape[:2]
            bands = array.shape[2] if array.ndim == 3 else 1
            image: Any = pyvips.Image.new_from_memory(array.tobytes(), width, height, bands, "uchar")
            return _encode_page(image, thumbnail)


def read_page(
    path: Path, archive_format: str, page: Page, limits: Limits, *, thumbnail: ThumbnailFormat | None = None
) -> bytes:
    try:
        return _read_page(path, archive_format, page, limits, thumbnail=thumbnail)
    except (
        zipfile.BadZipFile,
        struct.error,
        pyvips.Error,
        pdfium.PdfiumError,
        RuntimeError,
        zlib.error,
        lzma.LZMAError,
        EOFError,
    ) as error:
        raise InvalidArchive("Archive could not be decoded") from error
