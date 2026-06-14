"""Sidecar round-trips (§3.3) — Eze interop + the native sidecar reconstruct the metadata."""

from __future__ import annotations

import unicodedata
from pathlib import Path

from mangacouch.core import sidecars
from mangacouch.core.sidecars import GalleryMetadata, McSidecar


def test_eze_round_trip(tmp_path: Path):
    meta = GalleryMetadata(
        title="A Title",
        title_jpn="あるタイトル",
        category="Doujinshi",
        uploader="someone",
        posted=1600000000,
        rating=4.5,
        gid=12345,
        token="0a1b2c3d4e",
        site="e-hentai",
        tags=["artist:foo", "female:lolicon", "language:japanese"],
    )
    archive = tmp_path / "g.zip"
    archive.write_bytes(b"PK\x03\x04stub")
    sidecars.write_eze(archive, meta)

    loaded = sidecars.read_eze(archive)
    assert loaded is not None
    assert loaded.title == "A Title"
    assert loaded.gid == 12345
    assert loaded.token == "0a1b2c3d4e"
    assert set(loaded.tags) == set(meta.tags)


def test_mc_round_trip(tmp_path: Path):
    sidecar = McSidecar(
        archive_id="abcd" * 8,
        fingerprint="ff" * 16,
        format="zip",
        page_count=20,
        original_filename="g.zip",
        title="T",
        rating=3.0,
        source_url="https://e-hentai.org/g/1/abc/",
        source_gid=1,
        source_token="abc",
        tags=["artist:foo"],
        progress_page=5,
    )
    archive = tmp_path / "g.zip"
    archive.write_bytes(b"stub")
    sidecars.write_mc(archive, sidecar)

    loaded = sidecars.read_mc(archive)
    assert loaded is not None
    assert loaded.archive_id == "abcd" * 8
    assert loaded.page_count == 20
    assert loaded.progress_page == 5
    assert loaded.source_gid == 1


def test_sidecar_paths():
    p = Path("/m/Some Gallery.zip")
    assert sidecars.eze_sidecar_path(p).name == "Some Gallery.json"
    assert sidecars.mc_sidecar_path(p).name == "Some Gallery.mc.json"


def test_normalise_display_is_nfc():
    decomposed = unicodedata.normalize("NFD", "がぎ")
    assert sidecars.normalise_display(decomposed) == unicodedata.normalize("NFC", "がぎ")
