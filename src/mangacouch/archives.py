from __future__ import annotations

import errno
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
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pyvips
from pydantic import Field

from .errors import InvalidInput
from .limits import ImportCancelled as ImportCancelled
from .limits import Limits
from .limits import check_cancel as check_cancel
from .models import Model, normalized

pyvips.cache_set_max(0)

PDFIUM_LOCK = threading.RLock()
_PDF_DOCUMENTS: OrderedDict[tuple, pdfium.PdfDocument] = OrderedDict()


def close_pdf(path: Path) -> None:
    with PDFIUM_LOCK:
        for key in list(_PDF_DOCUMENTS):
            if key[0] == str(path):
                _PDF_DOCUMENTS.pop(key).close()


def _pdf_document(path: Path) -> pdfium.PdfDocument:
    """Reuse at most two parsed PDFs; callers hold PDFIUM_LOCK for all PDFium operations."""
    value = path.stat()
    key = (str(path), value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    document = _PDF_DOCUMENTS.get(key)
    if document is not None:
        _PDF_DOCUMENTS.move_to_end(key)
        return document
    for old in list(_PDF_DOCUMENTS):
        if old[0] == str(path):
            _PDF_DOCUMENTS.pop(old).close()
    document = pdfium.PdfDocument(path)
    _PDF_DOCUMENTS[key] = document
    while len(_PDF_DOCUMENTS) > 2:
        _PDF_DOCUMENTS.popitem(last=False)[1].close()
    return document


THUMBNAIL_WIDTH = 480
THUMBNAIL_HEIGHT = 720
IMAGE_VERSION = 2
"""Version of rendered covers, thumbnails and PDF pages; 2 applies JPEG EXIF orientation."""
IMAGE_SUFFIXES = {
    ".jpg",
    ".jpeg",
    ".jfif",
    ".jpe",
    ".png",
    ".webp",
    ".gif",
    ".avif",
    ".jxl",
    ".bmp",
    ".tif",
    ".tiff",
}
type ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
type ThumbnailFormat = Literal["webp", "jpeg"]


class InvalidArchive(InvalidInput):
    """A damaged or unsupported archive (API code `invalid_archive`).

    `member` names the archive member at fault, when one is known; storage and permission
    problems stay OSError so callers can tell the two apart.
    """

    code = "invalid_archive"

    def __init__(self, message: str = "Archive could not be decoded", *, member: str | None = None):
        super().__init__(f"{message}: {member}" if member is not None else message)
        self.member = member


class Page(Model):
    name: str
    """The ZIP member name as stored (its lookup key) or the PDF page index."""
    width: int
    height: int
    """Display size, after the EXIF orientation of JPEG pages."""
    media_type: ImageMediaType
    byte_size: int = 0
    orientation: Annotated[int, Field(ge=1, le=8)]
    """EXIF orientation of a JPEG page (1-8); 1 for every other page."""


DECODE_ERRORS = (
    zipfile.BadZipFile,
    struct.error,
    pyvips.Error,
    pdfium.PdfiumError,
    RuntimeError,
    zlib.error,
    lzma.LZMAError,
    EOFError,
)


def orientation(image: Any, media_type: ImageMediaType) -> int:
    """EXIF orientation of a decoded page header; only JPEG, the format every browser rotates."""
    if media_type != "image/jpeg" or not image.get_typeof("orientation"):
        return 1
    value = image.get("orientation")
    return value if type(value) is int and 1 <= value <= 8 else 1


def display_size(width: int, height: int, orientation: int) -> tuple[int, int]:
    return (height, width) if orientation >= 5 else (width, height)


def _image(data: bytes, limits: Limits) -> Any:
    image_media_type(data)
    image: Any = pyvips.Image.new_from_buffer(data, "", access="sequential", fail_on="error")
    check_dimensions(image.width, image.height, image.bands, limits)
    return image


def check_dimensions(width: int, height: int, bands: int, limits: Limits) -> None:
    if (
        not 0 < width * height <= limits.max_pixels
        or not 1 <= bands <= 4
        or max(width, height) > limits.max_dimension
    ):
        raise InvalidArchive("Page exceeds the supported image dimensions")


_PRECHECKS: OrderedDict[tuple, None] = OrderedDict()
_PRECHECK_LOCK = threading.Lock()


def check_zip_directory(path: Path, limits: Limits) -> None:
    value = path.stat()
    key = (str(path), value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns, limits)
    with _PRECHECK_LOCK:
        if key in _PRECHECKS:
            _PRECHECKS.move_to_end(key)
            return
    _check_zip_directory(path, limits)
    with _PRECHECK_LOCK:
        _PRECHECKS[key] = None
        while len(_PRECHECKS) > 128:
            _PRECHECKS.popitem(last=False)


def _check_zip_directory(path: Path, limits: Limits) -> None:
    with path.open("rb") as stream:
        size = path.stat().st_size
        stream.seek(max(0, size - 65557))
        tail = stream.read(65557)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or len(tail) - offset < 22:
            raise InvalidArchive("ZIP directory is missing")
        _, disk, start_disk, on_disk, entries, directory_size, directory_offset, comment_length = (
            struct.unpack("<4s4H2LH", tail[offset : offset + 22])
        )
        if offset + 22 + comment_length != len(tail) or disk or start_disk:
            raise InvalidArchive("Unsupported ZIP directory or multipart archive")
        eocd = max(0, size - 65557) + offset
        if (
            entries != 65535
            and directory_size != 0xFFFFFFFF
            and directory_offset != 0xFFFFFFFF
            and eocd >= 20
        ):
            stream.seek(eocd - 20)
            if stream.read(4) == b"PK\x06\x07":
                raise InvalidArchive("Undeclared ZIP64 directory")
        if entries == 65535 or directory_size == 0xFFFFFFFF or directory_offset == 0xFFFFFFFF:
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


_BAD_UTF8 = "ZIP member name is marked UTF-8 but is not valid UTF-8"


def _check_parsed_directory(archive: zipfile.ZipFile, limits: Limits) -> None:
    assert archive.fp is not None
    stream = archive.fp
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    stream.seek(max(0, size - 65557))
    tail = stream.read(65557)
    eocd = max(0, size - 65557) + tail.rfind(b"PK\x05\x06")
    if len(archive.infolist()) > limits.max_entries or eocd - archive.start_dir > limits.max_directory_bytes:
        raise InvalidArchive("ZIP directory exceeds configured limits")


def _open_zip(path: Path, limits: Limits) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(path)
        try:
            _check_parsed_directory(archive, limits)
        except BaseException:
            archive.close()
            raise
        return archive
    except UnicodeDecodeError:
        raise InvalidArchive(_BAD_UTF8) from None


def zip_image_bytes(path: Path, limits: Limits) -> int:
    check_zip_directory(path, limits)
    try:
        with _open_zip(path, limits) as archive:
            return sum(page.file_size for page in _zip_pages(archive, limits))
    except zipfile.BadZipFile:
        raise InvalidArchive("Invalid ZIP directory") from None


def member_name(entry: zipfile.ZipInfo) -> str:
    """The name used for safety checks, ordering and messages; `orig_filename` stays the lookup key.

    Without the UTF-8 flag or a Unicode Path field zipfile decodes names as cp437, so UTF-8 and
    then cp932 are tried on the original bytes: a 0x5C trail byte (表, ソ, 能) is then part of a
    character, not a separator. Remaining backslashes separate directories on every platform, as
    zipfile already does on Windows.
    """
    name = entry.orig_filename
    if entry.filename.replace("\\", "/") != name.split("\0", 1)[0].replace("\\", "/"):
        name = entry.filename  # zipfile applied an Info-ZIP Unicode Path extra field
    elif not entry.flag_bits & 0x800:
        raw = name.encode("cp437")
        for encoding in ("utf-8", "cp932"):
            try:
                name = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                pass
    return name.replace("\\", "/").rstrip("/")


def _unsafe_name(name: str, entry: zipfile.ZipInfo) -> bool:
    return (
        not name
        or PurePosixPath(name).is_absolute()
        or bool(PureWindowsPath(name).drive)
        or ".." in PurePosixPath(name).parts
        or any(ord(c) < 32 for c in name + entry.orig_filename)
        or stat.S_ISLNK(entry.external_attr >> 16)
    )


def _zip_pages(
    archive: zipfile.ZipFile, limits: Limits, rejected: list[tuple[str, str]] | None = None
) -> list[zipfile.ZipInfo]:
    """Image members in reading order. Members are never extracted, only read through their
    ZipInfo, so names are checked and ordered by `member_name` while `orig_filename` stays the key.

    Duplicate stored names, encryption and size limits reject the archive. Names that only
    collide once normalized, and image members with unsafe names (absolute, `..`, control
    characters, symlinks, which are left out), are reported in `rejected`: an import refuses
    them, while pages recorded earlier stay readable by their stored names."""
    pages = []
    stored: set[str] = set()
    seen: set[str] = set()
    total = 0
    for entry in archive.infolist():
        name = member_name(entry)
        if entry.orig_filename.rstrip("/") in stored:
            raise InvalidArchive("Ambiguous duplicate ZIP member name", member=name)
        stored.add(entry.orig_filename.rstrip("/"))
        if name in seen and rejected is not None:
            rejected.append(("Ambiguous duplicate ZIP member name", name))
        seen.add(name)
        total += entry.file_size
        if entry.file_size > limits.max_page_bytes or total > limits.max_unpacked_bytes:
            raise InvalidArchive("ZIP expansion exceeds configured limits")
        if entry.flag_bits & 1:
            raise InvalidArchive("Encrypted ZIP is unsupported")
        if entry.is_dir() or PurePosixPath(name).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if _unsafe_name(name, entry):
            if rejected is not None:
                rejected.append(("Unsafe ZIP image member name", name))
        elif not any(p.startswith(".") or p == "__MACOSX" for p in PurePosixPath(name).parts):
            pages.append(entry)
    if not 0 < len(pages) <= limits.max_pages:
        raise InvalidArchive("Archive has no readable pages or exceeds the page limit")

    def order(entry: zipfile.ZipInfo) -> tuple:
        name = member_name(entry)
        parts = re.split(r"(\d+)", normalized(name))
        return tuple((1, int(p)) if p.isdecimal() else (0, p) for p in parts), name, entry.orig_filename

    return sorted(pages, key=order)


# Cache only immutable directory metadata, never file handles (including on Windows).
_DIRECTORY_BUDGET = 32 * 1024**2
_DIRECTORY_CACHE: OrderedDict[tuple, tuple[dict, int]] = OrderedDict()
_DIRECTORY_LOCK = threading.Lock()


class _PageZip(zipfile.ZipFile):
    def __init__(self, path: Path, limits: Limits):
        self.limits = limits
        self.pages: dict[str, zipfile.ZipInfo] = {}
        try:
            super().__init__(path, mode="r")
        except UnicodeDecodeError:
            raise InvalidArchive(_BAD_UTF8) from None

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
        _check_parsed_directory(self, self.limits)
        pages = _zip_pages(self, self.limits)
        self.pages = {entry.orig_filename: entry for entry in pages}
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


def _read_member(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bytes:
    try:
        return archive.read(entry)
    except OSError as error:
        if error.errno in {errno.ENOSPC, errno.EDQUOT, errno.EIO, errno.EROFS, errno.ENODEV}:
            raise
        raise InvalidArchive("Member could not be read", member=member_name(entry)) from error


def _inspect_member(
    archive: zipfile.ZipFile, entry: zipfile.ZipInfo, limits: Limits, cancelled: Callable[[], bool] | None
) -> Page:
    check_cancel(cancelled)
    try:
        data = _read_member(archive, entry)
        media_type = image_media_type(data)
        image = _image(data, limits)
        image.avg()
    except InvalidArchive as error:
        raise InvalidArchive(error.message, member=member_name(entry)) from error
    except DECODE_ERRORS as error:
        raise InvalidArchive("Page could not be decoded", member=member_name(entry)) from error
    rotation = orientation(image, media_type)
    width, height = display_size(image.width, image.height, rotation)
    return Page(
        name=entry.orig_filename,
        width=width,
        height=height,
        media_type=media_type,
        byte_size=len(data),
        orientation=rotation,
    )


def inspect_archive(
    path: Path,
    archive_format: str,
    limits: Limits,
    cancelled: Callable[[], bool] | None = None,
) -> list[Page]:
    """Validate every page and member. Errors naming one page or member carry it in `member`."""
    try:
        if archive_format == "cbz":
            check_zip_directory(path, limits)
            with _open_zip(path, limits) as archive:
                rejected: list[tuple[str, str]] = []
                entries = _zip_pages(archive, limits, rejected)
                if rejected:
                    # Never pass an archive off as complete while leaving image pages out.
                    message, member = rejected[0]
                    raise InvalidArchive(message, member=member)
                result = [_inspect_member(archive, entry, limits, cancelled) for entry in entries]
                pages = {id(entry) for entry in entries}
                for entry in archive.infolist():
                    if not entry.is_dir() and id(entry) not in pages:
                        try:
                            with archive.open(entry) as stream:
                                while stream.read(1024**2):
                                    check_cancel(cancelled)
                        except OSError as error:
                            if error.errno in {
                                errno.ENOSPC,
                                errno.EDQUOT,
                                errno.EIO,
                                errno.EROFS,
                                errno.ENODEV,
                            }:
                                raise
                            raise InvalidArchive(
                                "Member could not be decoded", member=member_name(entry)
                            ) from error
                        except DECODE_ERRORS as error:
                            raise InvalidArchive(
                                "Member could not be decoded", member=member_name(entry)
                            ) from error
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
                result.append(
                    Page(name=str(index), width=width, height=height, media_type="image/webp", orientation=1)
                )
            return result
        finally:
            with PDFIUM_LOCK:
                document.close()
    except DECODE_ERRORS as error:
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
    """A page scaled into the thumbnail box, upright as the reader shows it; JPEG/WebP shrink
    while decoding (shrink-on-load)."""
    media_type = image_media_type(data)
    header: Any = pyvips.Image.new_from_buffer(data, "", access="sequential")
    check_dimensions(header.width, header.height, header.bands, limits)
    return pyvips.Image.thumbnail_buffer(
        data,
        THUMBNAIL_WIDTH,
        height=THUMBNAIL_HEIGHT,
        size="down",
        no_rotate=media_type != "image/jpeg",
        fail_on="error",
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
    if archive_format == "cbz":
        check_zip_directory(path, limits)
        with _PageZip(path, limits) as archive:
            entry = archive.pages.get(page.name)
            if entry is None:
                raise InvalidArchive("Page is no longer present", member=page.name)
            data = _read_member(archive, entry)
            if thumbnail is None:
                return data
            return _encode_page(_thumbnail(data, limits), thumbnail)
    with (
        PDFIUM_LOCK,
        closing(_pdf_document(path)[int(page.name)]) as pdf_page,
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
            # An owned copy: the bitmap is freed with the lock, and encoding needs neither.
            pixels = array.tobytes()
    image: Any = pyvips.Image.new_from_memory(pixels, width, height, bands, "uchar")
    return _encode_page(image, thumbnail)


def read_page(
    path: Path, archive_format: str, page: Page, limits: Limits, *, thumbnail: ThumbnailFormat | None = None
) -> bytes:
    try:
        return _read_page(path, archive_format, page, limits, thumbnail=thumbnail)
    except DECODE_ERRORS as error:
        raise InvalidArchive("Archive could not be decoded") from error
