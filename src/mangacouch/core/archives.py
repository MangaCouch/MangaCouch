"""Archive readers — zip/cbz (stdlib ``zipfile``) and pdf (``pypdfium2``) (R5).

Pages are read **from the archive on demand** (no full unpack, §5.7). A uniform ``ArchiveReader``
interface lets hashing, thumbnailing, and page serving share one code path across formats.
"""

from __future__ import annotations

import threading
import zipfile
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from .naturalsort import natural_page_sort

# PDFium is not thread-safe: concurrent open/render/close from the API's worker threads can
# corrupt memory. One process-wide lock serialises every pdfium call (rendering one PDF page is
# fast; the zip path is unaffected).
_PDFIUM_LOCK = threading.RLock()

IMAGE_EXTENSIONS = frozenset(
    {".jpg", ".jpeg", ".jpe", ".png", ".gif", ".webp", ".bmp", ".avif", ".jxl", ".tif", ".tiff"}
)
# Entries to ignore for page lists and fingerprints (container/metadata noise, §4).
_IGNORED_PREFIXES = ("__MACOSX/", ".")
_IGNORED_NAMES = frozenset({"Thumbs.db", ".DS_Store"})

ZIP_FORMATS = frozenset({"zip", "cbz"})
SUPPORTED_FORMATS = frozenset({"zip", "cbz", "pdf"})


class UnsupportedArchive(Exception):
    pass


def detect_format(path: Path) -> str:
    """Map a file extension to a format token. Raises ``UnsupportedArchive`` for the rest (R5)."""
    ext = path.suffix.lower().lstrip(".")
    if ext in ("zip",):
        return "zip"
    if ext in ("cbz",):
        return "cbz"
    if ext in ("pdf",):
        return "pdf"
    raise UnsupportedArchive(f"unsupported archive format: {path.suffix!r}")


def is_supported(path: Path) -> bool:
    try:
        detect_format(path)
    except UnsupportedArchive:
        return False
    return True


def _is_image_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    if not base or base in _IGNORED_NAMES:
        return False
    if name.startswith(_IGNORED_PREFIXES) or base.startswith("."):
        return False
    return Path(name).suffix.lower() in IMAGE_EXTENSIONS


class ArchiveReader(ABC):
    """Read pages from one archive. Use as a context manager."""

    format: str

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @abstractmethod
    def list_pages(self) -> list[str]:
        """Ordered list of page identifiers (entry names for zip; synthetic for pdf)."""

    @abstractmethod
    def page_count(self) -> int: ...

    @abstractmethod
    def read_page_bytes(self, page_id: str) -> bytes:
        """Return the raw encoded image bytes for ``page_id`` (renders for pdf)."""

    @abstractmethod
    def iter_image_blobs(self) -> Iterator[tuple[str, bytes]]:
        """Yield ``(entry_name, raw_bytes)`` for every image entry — used by the fingerprint."""

    def close(self) -> None: ...


class ZipArchiveReader(ArchiveReader):
    def __init__(self, path: Path, fmt: str = "zip") -> None:
        self.format = fmt
        self._zf = zipfile.ZipFile(path, "r")
        names = [
            info.filename
            for info in self._zf.infolist()
            if not info.is_dir() and _is_image_name(info.filename)
        ]
        self._pages = natural_page_sort(names)

    def list_pages(self) -> list[str]:
        return list(self._pages)

    def page_count(self) -> int:
        return len(self._pages)

    def read_page_bytes(self, page_id: str) -> bytes:
        data = self._zf.read(page_id)
        if not data:
            raise ValueError(f"empty/truncated entry: {page_id!r}")
        return data

    def iter_image_blobs(self) -> Iterator[tuple[str, bytes]]:
        for name in self._pages:
            yield name, self._zf.read(name)

    def close(self) -> None:
        self._zf.close()


class PdfArchiveReader(ArchiveReader):
    """PDF pages are rasterised on demand at a sensible scale; no full unpack."""

    format = "pdf"
    _RENDER_SCALE = 2.0  # ~144 DPI for the served page; thumbnails downscale further

    def __init__(self, path: Path) -> None:
        import pypdfium2 as pdfium

        self._pdfium = pdfium
        with _PDFIUM_LOCK:
            self._doc = pdfium.PdfDocument(path)
            self._n = len(self._doc)

    def list_pages(self) -> list[str]:
        # Synthetic, stable, naturally-sortable page ids.
        width = max(4, len(str(self._n)))
        return [f"{i + 1:0{width}d}.png" for i in range(self._n)]

    def page_count(self) -> int:
        return self._n

    def _render(self, index: int, scale: float, fmt: str = "png") -> bytes:
        from . import imaging  # local import keeps pyvips off the hashing-only import path

        with _PDFIUM_LOCK:
            page = self._doc[index]
            # rev_byteorder=True yields RGBA (vs pdfium's native BGRA), matching pyvips.
            bitmap = page.render(scale=scale, rev_byteorder=True)  # pyright: ignore[reportArgumentType]
            array = bitmap.to_numpy()
        buf, width, height, bands = imaging.normalise_bitmap(array)
        return imaging.encode_rgba(buf, width, height, bands, fmt=fmt)

    def read_page_bytes(self, page_id: str) -> bytes:
        index = self._page_index(page_id)
        return self._render(index, self._RENDER_SCALE)

    def render_page_index(self, index: int, scale: float, fmt: str = "png") -> bytes:
        return self._render(index, scale, fmt)

    def _page_index(self, page_id: str) -> int:
        try:
            index = int(Path(page_id).stem) - 1
        except ValueError as exc:
            raise ValueError(f"bad pdf page id: {page_id!r}") from exc
        if not 0 <= index < self._n:
            raise ValueError(f"pdf page out of range: {page_id!r}")
        return index

    def iter_image_blobs(self) -> Iterator[tuple[str, bytes]]:
        # PDFs have no stable per-entry image blobs cheap to extract; identity falls back to the
        # full-file hash and the fingerprint is left undefined for pdf (§4).
        return iter(())

    def close(self) -> None:
        with _PDFIUM_LOCK:
            self._doc.close()


def open_archive(path: Path) -> ArchiveReader:
    fmt = detect_format(path)
    if fmt in ZIP_FORMATS:
        return ZipArchiveReader(path, fmt)
    return PdfArchiveReader(path)
