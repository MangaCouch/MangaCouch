"""Identity & dedup hashing (§4).

- **Primary id** = full-file ``xxh3-128``, streamed in chunks. Whole-content identity — no
  head-collisions (LANraragi hashed only the first 512 KB, which false-merged distinct archives
  sharing a cover and false-split re-zips). ``xxh3`` is far faster than any disk, so hashing the
  whole file is IO-bound even on a slow USB drive.
- **Dedup fingerprint** = hash of the *sorted per-entry image digests* (ignore container,
  compression level and entry order; skip ``__MACOSX``/metadata entries). Re-zips and cbz↔zip
  conversions of the same pages therefore share a fingerprint.

Both are cached keyed by ``(rel_path, size, mtime)`` (R4): a file is only (re)hashed when new or
changed. Empty and truncated reads are rejected explicitly.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import xxhash

from .archives import ZIP_FORMATS, ArchiveReader, detect_format, open_archive

_CHUNK = 1 << 20  # 1 MiB


@dataclass(frozen=True, slots=True)
class FileStat:
    """The cache key for a hash (R4). Two files with the same triple are treated as identical."""

    rel_path: str
    size: int
    mtime: float

    @classmethod
    def of(cls, path: Path, manga_root: Path) -> FileStat:
        st = path.stat()
        rel = path.relative_to(manga_root).as_posix()
        return cls(rel_path=rel, size=st.st_size, mtime=st.st_mtime)


class TruncatedFileError(Exception):
    """Raised when a file is empty or shorter than its reported size (a partial download)."""


def content_id(path: Path) -> str:
    """Full-file xxh3-128 hex (the 32-char archive id). Rejects empty/truncated files."""
    size = path.stat().st_size
    if size == 0:
        raise TruncatedFileError(f"empty file: {path}")
    h = xxhash.xxh3_128()
    read = 0
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
            read += len(chunk)
    if read != size:
        raise TruncatedFileError(f"short read ({read} of {size} bytes): {path}")
    return h.hexdigest()


def _image_digest(blob: bytes) -> str:
    if not blob:
        raise TruncatedFileError("empty archive entry")
    return xxhash.xxh3_128(blob).hexdigest()


def fingerprint_from_reader(reader: ArchiveReader) -> str | None:
    """Content fingerprint from an open reader, or ``None`` when the format has no image entries.

    The digest order is sorted so container, compression and entry order do not affect the result.
    """
    digests = sorted(_image_digest(blob) for _name, blob in reader.iter_image_blobs())
    if not digests:
        return None
    agg = xxhash.xxh3_128()
    for d in digests:
        agg.update(d.encode("ascii"))
    return agg.hexdigest()


def fingerprint(path: Path) -> str | None:
    """Open ``path`` and compute its dedup fingerprint (``None`` for pdf — see §4)."""
    fmt = detect_format(path)
    if fmt not in ZIP_FORMATS:
        return None
    with open_archive(path) as reader:
        return fingerprint_from_reader(reader)


@dataclass(frozen=True, slots=True)
class HashResult:
    archive_id: str
    fingerprint: str | None
    stat: FileStat


def hash_archive(path: Path, manga_root: Path) -> HashResult:
    """Compute both hashes plus the cache-key stat for a single archive in one pass over the file."""
    stat = FileStat.of(path, manga_root)
    return HashResult(
        archive_id=content_id(path),
        fingerprint=fingerprint(path),
        stat=stat,
    )


def iter_chunks(path: Path, chunk: int = _CHUNK) -> Iterator[bytes]:
    with path.open("rb") as fh:
        while data := fh.read(chunk):
            yield data
