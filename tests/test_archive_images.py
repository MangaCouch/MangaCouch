"""ZIP member names, damaged pages, PDF rendering outside the PDFium lock and EXIF orientation."""

from __future__ import annotations

import os
import struct
import threading
import zipfile
import zlib
from contextlib import closing
from typing import Any

import numpy as np
import pytest
import pyvips
from helpers import CSRF, jpeg, make_zip, manifest, oriented_book, png, raw_zip

from mangacouch import archives
from mangacouch.archives import InvalidArchive, Limits, Page, inspect_archive, read_page, zip_image_bytes
from mangacouch.ingest import error_reason
from mangacouch.page_features import feature, matches

LIMITS = Limits()


def pixels(data: bytes) -> np.ndarray:
    image: Any = pyvips.Image.new_from_buffer(data, "")
    return np.ndarray(
        buffer=image.write_to_memory(), dtype=np.uint8, shape=(image.height, image.width, image.bands)
    )


# Member names ------------------------------------------------------------------------------------


def test_backslash_and_cp932_names_keep_every_page_on_every_platform(tmp_path, monkeypatch):
    members = [
        ("表紙.png".encode("cp932"), png(1)),  # 0x95 0x5C: a trail byte, not a separator
        (b"01.png", png(2)),
        (b"02.png", png(3)),
        ("ソード/03.png".encode("cp932"), png(4)),
        ("能力\\04.png".encode("cp932"), png(5)),
        (b"chap1\\05.png", png(6)),
    ]
    path = raw_zip(tmp_path / "sjis.cbz", members)
    results = {}
    for separator in ("/", "\\"):
        monkeypatch.setattr(os, "sep", separator)  # what zipfile reads to normalize names
        archives._DIRECTORY_CACHE.clear()
        pages = inspect_archive(path, "cbz", LIMITS)
        results[separator] = (pages, [read_page(path, "cbz", page, LIMITS) for page in pages])
    monkeypatch.undo()
    archives._DIRECTORY_CACHE.clear()
    assert results["/"] == results["\\"]
    pages, data = results["/"]
    assert len(pages) == len(members) and sorted(data) == sorted(item[1] for item in members)
    # The lookup key is the stored name, never the normalized display name.
    assert {page.name for page in pages} == {raw.decode("cp437") for raw, _ in members}
    with zipfile.ZipFile(path) as archive:
        names = [archives.member_name(entry) for entry in archive.infolist()]
    assert names == ["表紙.png", "01.png", "02.png", "ソード/03.png", "能力/04.png", "chap1/05.png"]
    assert zip_image_bytes(path, LIMITS) == sum(len(item[1]) for item in members)


def test_unicode_path_field_names_the_member_but_the_stored_name_stays_the_key(tmp_path):
    raw = "表紙.png".encode("cp932")
    unicode_name = "表紙.png".encode()
    field = struct.pack("<BL", 1, zlib.crc32(raw)) + unicode_name
    path = raw_zip(
        tmp_path / "infozip.cbz",
        [(raw, png(1)), (b"01.png", png(2))],
        extra={0: struct.pack("<HH", 0x7075, len(field)) + field},
    )
    with zipfile.ZipFile(path) as archive:
        entry = archive.infolist()[0]
        assert archives.member_name(entry) == "表紙.png"
    pages = inspect_archive(path, "cbz", LIMITS)
    assert raw.decode("cp437") in {page.name for page in pages}
    assert {read_page(path, "cbz", page, LIMITS) for page in pages} == {png(1), png(2)}


def test_names_that_collide_after_normalization_are_ambiguous(files, tmp_path):
    path = raw_zip(tmp_path / "collide.cbz", [(b"a/b.png", png(1)), (b"a\\b.png", png(2))])
    with pytest.raises(InvalidArchive, match="Ambiguous duplicate") as error:
        files.import_file(path)
    assert error.value.member == "a/b.png"
    assert path.exists()
    # A page recorded under one of the colliding names stays readable by that stored name.
    page = Page(name="a/b.png", width=12, height=8, media_type="image/png", orientation=1)
    assert read_page(path, "cbz", page, LIMITS) == png(1)
    duplicate = raw_zip(tmp_path / "duplicate.cbz", [(b"a/b.png", png(1)), (b"a/c.png", png(2))])
    duplicate.write_bytes(duplicate.read_bytes().replace(b"a/c.png", b"a/b.png"))
    with pytest.raises(InvalidArchive, match="Ambiguous duplicate"):
        read_page(duplicate, "cbz", page, LIMITS)


# Damaged archives are rejected, name the member and are not storage errors ---------


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("002.jpg", jpeg(seed=3)[: len(jpeg(seed=3)) * 6 // 10], "could not be decoded"),
        ("002.jpg", b"", "Expected GIF"),
        ("002.jpg", b"<!doctype html><title>509</title>", "Expected GIF"),
    ],
    ids=["truncated", "empty", "html"],
)
def test_damaged_pages_are_rejected_with_the_member_name(files, tmp_path, name, data, message):
    path = tmp_path / "damaged.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", jpeg(seed=1))
        archive.writestr(f"chapter/{name}", data)
        archive.writestr("003.jpg", jpeg(seed=2))
    before = path.read_bytes()
    with pytest.raises(InvalidArchive, match=message) as error:
        files.import_file(path)
    assert error.value.member == f"chapter/{name}"
    assert error_reason(error.value) == ("invalid_archive", str(error.value))
    assert f"chapter/{name}" in str(error.value)
    assert path.read_bytes() == before
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0


def test_unreadable_archive_file_is_a_storage_error_not_a_bad_archive(tmp_path):
    path = tmp_path / "missing.cbz"
    with pytest.raises(OSError) as error:
        inspect_archive(path, "cbz", LIMITS)
    assert not isinstance(error.value, InvalidArchive)
    assert error_reason(error.value)[0] == "source_missing"
    assert error_reason(PermissionError(13, "Permission denied"))[0] == "storage_error"


def test_utf8_flagged_names_with_other_bytes_are_a_bad_archive(files, tmp_path, api):
    gbk = "页面.png".encode("gbk")
    utf8 = "éé.png".encode()  # same length; zipfile sets the UTF-8 flag for it
    assert len(gbk) == len(utf8)
    path = tmp_path / "gbk.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.png", png(1))
        archive.writestr(utf8.decode(), png(2))
    path.write_bytes(path.read_bytes().replace(utf8, gbk))
    with pytest.raises(InvalidArchive, match="not valid UTF-8"):
        files.import_file(path)
    with pytest.raises(InvalidArchive, match="not valid UTF-8"):
        zip_image_bytes(path, LIMITS)
    with pytest.raises(InvalidArchive, match="not valid UTF-8"):
        read_page(
            path,
            "cbz",
            Page(name="001.png", width=12, height=8, media_type="image/png", orientation=1),
            LIMITS,
        )
    response = api.post(
        "/api/import", params={"filename": "gbk.cbz"}, content=path.read_bytes(), headers=CSRF
    )
    assert response.status_code == 422 and response.json()["error"] == "invalid_archive"


# PDF encoding runs outside the PDFium lock -------------------------------------------------


def test_pdf_pages_are_encoded_after_the_pdfium_lock_is_released(tmp_path, monkeypatch):
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    path = tmp_path / "page.pdf"
    with archives.PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
        with closing(document.new_page(100, 150)) as page:
            rect = raw.FPDFPageObj_CreateNewRect(10, 20, 40, 65)
            raw.FPDFPageObj_SetFillColor(rect, 33, 125, 210, 255)
            raw.FPDFPath_SetDrawMode(rect, raw.FPDF_FILLMODE_WINDING, False)
            raw.FPDFPage_InsertObject(page, rect)
            page.gen_content()
        document.save(path)
    pages = inspect_archive(path, "pdf", LIMITS)
    encode = archives._encode_page
    free = []

    def probe(image, thumbnail):
        def other():
            acquired = archives.PDFIUM_LOCK.acquire(timeout=2)
            if acquired:
                archives.PDFIUM_LOCK.release()
            free.append(acquired)

        thread = threading.Thread(target=other)
        thread.start()
        thread.join()
        import gc

        gc.collect()  # the pixel copy must outlive the freed PDFium bitmap
        return encode(image, thumbnail)

    monkeypatch.setattr(archives, "_encode_page", probe)
    data = read_page(path, "pdf", pages[0], LIMITS)
    thumbnail = read_page(path, "pdf", pages[0], LIMITS, thumbnail="webp")
    assert free == [True, True]
    with (
        archives.PDFIUM_LOCK,
        closing(pdfium.PdfDocument(path)) as document,
        closing(document[0]) as page,
        closing(page.render(scale=2, rev_byteorder=True)) as bitmap,
    ):
        expected = bitmap.to_numpy().tobytes()
    assert pyvips.Image.new_from_buffer(data, "").write_to_memory() == expected
    assert b"VP8L" in data and pyvips.Image.new_from_buffer(thumbnail, "").width <= archives.THUMBNAIL_WIDTH


# EXIF orientation --------------------------------------------------------------------------------


def test_rotated_jpeg_records_display_size_and_upright_thumbnail(files, tmp_path, api):
    book = oriented_book(files, tmp_path)
    pages = manifest(files, book.id)
    assert [(page["width"], page["height"], page["orientation"]) for page in pages] == [
        (80, 40, 6),
        (40, 80, 1),
        (12, 8, 1),
    ]
    # The reader shows raw bytes, which browsers rotate; covers, sizes and spreads follow suit.
    assert files.page_bytes(book.id, 0) == jpeg(orientation=6, seed=7)
    cover = pyvips.Image.new_from_buffer(files.cover_bytes(book.id, files.page_cache), "")
    assert (cover.width, cover.height) == (80, 40)
    detail = api.get(f"/api/books/{book.id}").json()
    assert detail["page_sizes"] == [[80, 40], [40, 80], [12, 8]]
    komga = api.get(f"/komga/api/v1/books/{book.id}/pages").json()
    assert [(page["width"], page["height"]) for page in komga] == [(80, 40), (40, 80), (12, 8)]


def test_mirrored_jpeg_keeps_its_size_but_thumbnail_is_mirrored(files, tmp_path):
    book = oriented_book(files, tmp_path, orientation=2)
    assert (manifest(files, book.id)[0]["width"], manifest(files, book.id)[0]["height"]) == (40, 80)
    thumbnail = pixels(files.cover_bytes(book.id, files.page_cache))
    plain = pixels(archives._encode_page(archives._thumbnail(jpeg(seed=7), LIMITS), "webp"))
    assert np.abs(thumbnail.astype(int) - plain[:, ::-1].astype(int)).mean() < 4
    assert np.abs(thumbnail.astype(int) - plain.astype(int)).mean() > 20


def test_fingerprint_of_exif_rotated_page_matches_the_upright_page(files):
    tagged = jpeg(orientation=6, seed=9)
    upright: Any = pyvips.Image.new_from_buffer(jpeg(seed=9), "").rot90()
    rotated = feature(tagged, files)
    plain = feature(upright.jpegsave_buffer(Q=95), files)
    assert rotated["aspect"] == pytest.approx(plain["aspect"]) == pytest.approx(2.0)
    assert matches(rotated, plain)
    assert not matches(rotated, feature(jpeg(seed=9), files))


@pytest.mark.perf
def test_zip_directory_cache_invalidates_and_never_retains_open_archives(tmp_path, monkeypatch):
    path = make_zip(tmp_path / "cache.cbz", ["1.png", "2.png"])
    limits = archives.Limits()
    pages = archives.inspect_archive(path, "cbz", limits)
    calls = []
    parse = archives.zipfile.ZipFile._RealGetContents  # pyright: ignore[reportAttributeAccessIssue]

    def counted(archive):
        calls.append(1)
        return parse(archive)

    monkeypatch.setattr(archives.zipfile.ZipFile, "_RealGetContents", counted)
    with archives.zipfile.ZipFile(path) as archive:
        expected = [archive.read(page.name) for page in pages]
    calls.clear()
    assert archives.read_page(path, "cbz", pages[0], limits) == expected[0]
    assert archives.read_page(path, "cbz", pages[1], limits) == expected[1]
    assert len(calls) == 1
    with pytest.raises(archives.InvalidArchive):
        archives.read_page(path, "cbz", pages[0], archives.Limits(max_pages=1))
    # Replacement and deletion work even after a cache hit, including on Windows.
    path.unlink()
    make_zip(path, ["replacement.png"])
    with pytest.raises(archives.InvalidArchive, match="no longer present"):
        archives.read_page(path, "cbz", pages[0], limits)
    path.unlink()


def test_default_pixel_limit_matches_pillow_decompression_bomb_threshold():
    from PIL import Image

    assert Image.MAX_IMAGE_PIXELS is not None
    assert Limits().max_pixels == 2 * Image.MAX_IMAGE_PIXELS
    # A real 8601x11961 grayscale page (102.9 MP) from an EH original archive must import.
    archives.check_dimensions(8601, 11961, 1, LIMITS)
    archives.check_dimensions(1, LIMITS.max_pixels, 1, Limits(max_dimension=LIMITS.max_pixels))
    with pytest.raises(InvalidArchive, match="supported image dimensions"):
        archives.check_dimensions(1, LIMITS.max_pixels + 1, 1, Limits(max_dimension=LIMITS.max_pixels + 1))
