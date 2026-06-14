"""Identity & dedup hashing correctness (§4) — the false-merge/false-split properties (R4)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from mangacouch.core import hashing
from mangacouch.core.hashing import FileStat, TruncatedFileError

from .conftest import make_image_bytes, make_zip


def test_content_id_is_full_file_not_prefix(tmp_path: Path, sample_pages):
    """Two archives that share a large identical prefix but differ later get DIFFERENT ids.

    LANraragi hashed only the first 512 KB and false-merged such archives.
    """
    shared_prefix = make_image_bytes((10, 20, 30), size=600)  # well over any small prefix window
    a = make_zip(tmp_path / "a.zip", [shared_prefix, make_image_bytes((1, 1, 1))])
    b = make_zip(tmp_path / "b.zip", [shared_prefix, make_image_bytes((2, 2, 2))])
    assert hashing.content_id(a) != hashing.content_id(b)


def test_content_id_stable(tmp_path: Path, sample_pages):
    z = make_zip(tmp_path / "x.zip", sample_pages)
    assert hashing.content_id(z) == hashing.content_id(z)


def test_fingerprint_ignores_container_and_order(tmp_path: Path, sample_pages):
    """A re-zip with different compression and entry order shares a fingerprint (no false-split)."""
    original = make_zip(tmp_path / "orig.zip", sample_pages)

    # Same pages, reversed order, stored (no compression), different filenames.
    reordered = tmp_path / "rezip.zip"
    with zipfile.ZipFile(reordered, "w", zipfile.ZIP_STORED) as zf:
        for i, data in enumerate(reversed(sample_pages)):
            zf.writestr(f"page_{i}.png", data)

    assert hashing.fingerprint(original) == hashing.fingerprint(reordered)
    # ...but the exact-identity ids differ (different bytes on disk).
    assert hashing.content_id(original) != hashing.content_id(reordered)


def test_fingerprint_skips_macosx_and_metadata(tmp_path: Path, sample_pages):
    clean = make_zip(tmp_path / "clean.zip", sample_pages)
    noisy = tmp_path / "noisy.zip"
    with zipfile.ZipFile(noisy, "w") as zf:
        zf.writestr("__MACOSX/._001.png", b"junk")
        zf.writestr("Thumbs.db", b"junk")
        for i, data in enumerate(sample_pages):
            zf.writestr(f"{i + 1:03d}.png", data)
    assert hashing.fingerprint(clean) == hashing.fingerprint(noisy)


def test_cbz_equals_zip_fingerprint(tmp_path: Path, sample_pages):
    z = make_zip(tmp_path / "a.zip", sample_pages)
    c = make_zip(tmp_path / "a.cbz", sample_pages)
    assert hashing.fingerprint(z) == hashing.fingerprint(c)


def test_distinct_content_distinct_fingerprint(tmp_path: Path, sample_pages):
    a = make_zip(tmp_path / "a.zip", sample_pages)
    b = make_zip(tmp_path / "b.zip", [*sample_pages[:2], make_image_bytes((99, 99, 99))])
    assert hashing.fingerprint(a) != hashing.fingerprint(b)


def test_empty_file_rejected(tmp_path: Path):
    empty = tmp_path / "empty.zip"
    empty.write_bytes(b"")
    with pytest.raises(TruncatedFileError):
        hashing.content_id(empty)


def test_filestat_relative_and_cacheable(tmp_path: Path, sample_pages):
    z = make_zip(tmp_path / "sub" if False else tmp_path / "g.zip", sample_pages)
    stat = FileStat.of(z, tmp_path)
    assert stat.rel_path == "g.zip"  # relative to the manga root (R2)
    assert stat.size > 0
    # The cache key is the (rel_path, size, mtime) triple.
    assert FileStat.of(z, tmp_path) == stat
