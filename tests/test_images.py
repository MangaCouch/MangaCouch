from __future__ import annotations

import json
import threading
import zipfile
from contextlib import closing

import pytest
from helpers import login, make_zip

from mangacouch import archives, ingest
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.files import ContentChanged
from mangacouch.image_cache import ImageCache
from mangacouch.ingest import FileLibrary, image_bytes
from mangacouch.scanning import Scanner


def test_raw_jpeg_mime_and_conditional_page_skips_render(api, files, tmp_path, monkeypatch):
    import pyvips

    payload = pyvips.Image.black(12, 8).new_from_image([40, 60, 80]).write_to_buffer(".jpg")
    archive = tmp_path / "jpeg.cbz"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("1.jpg", payload)
    book = files.import_file(archive).book
    client = api
    url = f"/api/books/{book.id}/pages/0"
    response = client.get(url)
    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"] == "image/jpeg"
    monkeypatch.setattr(files, "page_bytes", lambda *args: pytest.fail("304 must not render"))
    assert client.get(url, headers={"If-None-Match": response.headers["etag"]}).status_code == 304


def raster(format, *, animated=False):
    import numpy as np
    import pyvips

    pixels = np.full((8, 12, 3), [240, 20, 40], dtype=np.uint8)
    image = pyvips.Image.new_from_memory(pixels.tobytes(), 12, 8, 3, "uchar")
    if animated:
        image = image.join(image.new_from_image([20, 40, 240]), "vertical")
        return image.gifsave_buffer(page_height=8, dither=0)
    return image.write_to_buffer("." + format)


def image_book(files, tmp_path, formats):
    path = tmp_path / ("-".join(formats) + ".cbz")
    payloads = [raster(format, animated=format == "gif") for format in formats]
    with zipfile.ZipFile(path, "w") as archive:
        for index, (format, payload) in enumerate(zip(formats, payloads, strict=True)):
            archive.writestr(f"{index}.{format}", payload)
    return files.import_file(path).book, payloads


@pytest.mark.parametrize("format", ["jpg", "jpeg", "png", "gif", "webp"])
def test_original_formats_roundtrip_over_web_and_komga_without_reading_copies(
    api, files, tmp_path, format, monkeypatch
):
    import json

    book, payloads = image_book(files, tmp_path, [format])
    mime = "image/" + ("jpeg" if format == "jpg" else format)
    with files.library.connection() as db:
        manifest = json.loads(
            db.execute("SELECT pages_json FROM archive_manifest WHERE book_id=?", (book.id,)).fetchone()[0]
        )
    assert manifest[0]["media_type"] == mime and manifest[0]["byte_size"] == len(payloads[0])
    monkeypatch.setattr(archives, "_image", lambda *_: pytest.fail("Original reads must not decode"))
    with api as client:
        for path in (f"/api/books/{book.id}/pages/0", f"/komga/api/v1/books/{book.id}/pages/1"):
            response = client.get(path)
            assert response.status_code == 200 and response.headers["content-type"] == mime
            assert response.content == payloads[0]
        pages = client.get(f"/komga/api/v1/books/{book.id}/pages").json()
        assert pages[0]["mediaType"] == mime and pages[0]["sizeBytes"] == len(payloads[0])
    assert not files.page_cache.path.exists()


def test_animated_gif_is_preserved_and_thumbnail_uses_first_frame(files, tmp_path):
    import pyvips

    book, payloads = image_book(files, tmp_path, ["gif"])
    assert files.page_bytes(book.id, 0) == payloads[0]
    assert pyvips.Image.new_from_buffer(payloads[0], "", n=-1).get("n-pages") == 2
    thumbnail = files.cover_bytes(book.id, ImageCache(tmp_path / "covers"))
    image = pyvips.Image.new_from_buffer(thumbnail, "")
    assert (image.width, image.height) == (12, 8)
    assert image[0].avg() > 220 and image[2].avg() < 60
    assert b"VP8 " in thumbnail and b"ANIM" not in thumbnail


@pytest.mark.parametrize("format", ["jpg", "png", "webp"])
def test_webp_thumbnails_and_on_demand_jpeg_covers_have_separate_cache_entries(
    files, tmp_path, format, monkeypatch
):
    from mangacouch import ingest
    from mangacouch.archives import image_media_type

    book, _ = image_book(files, tmp_path, [format])
    cache = ImageCache(tmp_path / "covers", batch_size=1)
    calls = []
    original = ingest.read_page

    def render(*args, **kwargs):
        calls.append(kwargs["thumbnail"])
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "read_page", render)
    webp = files.cover_bytes(book.id, cache)
    assert calls == ["webp"] and image_media_type(webp) == "image/webp"
    jpeg = files.cover_bytes(book.id, cache, format="jpeg")
    assert calls == ["webp", "jpeg"] and image_media_type(jpeg) == "image/jpeg"
    cache.close()
    reopened = ImageCache(tmp_path / "covers")
    assert files.cover_bytes(book.id, reopened) == webp
    assert files.cover_bytes(book.id, reopened, format="jpeg") == jpeg
    assert calls == ["webp", "jpeg"]


def pdf_book(files, tmp_path, width=100, height=150):

    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    path = tmp_path / "page.pdf"
    with closing(pdfium.PdfDocument.new()) as document:
        with closing(document.new_page(width, height)) as page:
            rect = raw.FPDFPageObj_CreateNewRect(10, 20, 40, 65)
            raw.FPDFPageObj_SetFillColor(rect, 33, 125, 210, 255)
            raw.FPDFPath_SetDrawMode(rect, raw.FPDF_FILLMODE_WINDING, False)
            raw.FPDFPage_InsertObject(page, rect)
            page.gen_content()
        document.save(path)
    return files.import_file(path).book, path


def test_pdf_direct_webp_is_pixel_lossless_and_disk_cached(files, tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    import pypdfium2 as pdfium
    import pyvips

    from mangacouch import ingest
    from mangacouch.ingest import FileLibrary

    book, path = pdf_book(files, tmp_path)
    original = ingest.read_page
    calls = []

    def render(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(ingest, "read_page", render)
    # Any intermediate PNG encoding would hit this boundary too.
    write = pyvips.Image.write_to_buffer

    def no_png(image, suffix, **kwargs):
        assert suffix != ".png"
        return write(image, suffix, **kwargs)

    monkeypatch.setattr(pyvips.Image, "write_to_buffer", no_png)
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: files.page_bytes(book.id, 0), range(8)))
    assert len(calls) == 1 and all(data == results[0] for data in results)
    data = results[0]
    assert b"VP8L" in data and b"VP8 " not in data
    decoded = pyvips.Image.new_from_buffer(data, "")
    with (
        closing(pdfium.PdfDocument(path)) as document,
        closing(document[0]) as page,
        closing(page.render(scale=2, rev_byteorder=True)) as bitmap,
    ):
        assert decoded.write_to_memory() == bitmap.to_numpy().tobytes()
    files.page_cache.close()
    reopened = FileLibrary(files.library, files.root.path)
    assert reopened.page_bytes(book.id, 0) == data and len(calls) == 1
    path_in_library = files.root.resolve(book.content.relative_path)
    path_in_library.write_bytes(b"changed")
    from mangacouch.files import ContentChanged

    with pytest.raises(ContentChanged):
        reopened.page_bytes(book.id, 0)


def test_pdf_render_respects_webp_dimension_limit(files, tmp_path):
    import pyvips

    book, _ = pdf_book(files, tmp_path, 20000, 40)
    image = pyvips.Image.new_from_buffer(files.page_bytes(book.id, 0), "")
    assert max(image.width, image.height) <= 16383


@pytest.mark.parametrize(
    "formats,expected",
    [
        (["jpg", "jpeg"], "image/jpeg"),
        (["png", "png"], "image/png"),
        (["gif", "gif"], "image/gif"),
        (["jpg", "png"], None),
        (["webp"], None),
        (["pdf"], None),
    ],
)
def test_catalog_and_pse_format_rules_are_independent(api, files, tmp_path, formats, expected):
    import xml.etree.ElementTree as ET

    from mangacouch.archives import image_media_type

    if formats == ["pdf"]:
        book, _ = pdf_book(files, tmp_path)
    else:
        book, _ = image_book(files, tmp_path, formats)
    with api as client:
        for prefix in ["/opds", "/komga/opds/v1.2"]:
            feed = client.get(prefix + "/books")
            assert feed.status_code == 200
            links = {
                link.get("rel"): link
                for link in ET.fromstring(feed.content).iter()
                if link.tag.endswith("}link")
            }
            assert "http://opds-spec.org/acquisition" in links
            cover = client.get(links["http://opds-spec.org/image"].attrib["href"])
            assert cover.status_code == 200 and image_media_type(cover.content) == "image/jpeg"
            stream = links.get("http://vaemendis.net/opds-pse/stream")
            allowed = expected
            assert cover.headers["cache-control"] == "private, max-age=86400"
            if allowed:
                assert stream is not None and stream.get("type") == allowed
                assert stream.get("{http://vaemendis.net/opds-pse/ns}count") == str(book.content.page_count)
                page = client.get(stream.attrib["href"].replace("{pageNumber}", "0"))
                assert page.status_code == 200 and page.headers["content-type"] == allowed
            else:
                assert stream is None
                assert client.get(f"{prefix}/books/{book.id}/pages/0").status_code == 404
        cover = client.get(f"/komga/api/v1/books/{book.id}/thumbnail")
        assert image_media_type(cover.content) == "image/jpeg"
        internal = client.get(f"/api/books/{book.id}/cover")
        assert internal.headers["content-type"] == "image/webp"


@pytest.mark.parametrize("name", ["1.jxl", "1.png"])
def test_jxl_pages_are_rejected(files, tmp_path, name):
    from mangacouch.archives import InvalidArchive

    path = tmp_path / "unsupported-jxl.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(name, b"\xff\x0a" + bytes(100))
    with pytest.raises(InvalidArchive):
        files.import_file(path)


@pytest.mark.parametrize("format", ["bmp", "tiff", "avif"])
def test_other_formats_are_not_accepted_as_supported_pages(files, tmp_path, format):
    import pyvips

    from mangacouch.archives import InvalidArchive

    data = pyvips.Image.black(12, 8).write_to_buffer("." + format) if format != "bmp" else b"BM" + bytes(100)
    path = tmp_path / "unsupported.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("disguised.png", data)
    with pytest.raises(InvalidArchive):
        files.import_file(path)


def test_original_pages_bypass_copy_cache_and_reject_changed_archives(files, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "cache.cbz")).book
    read = ingest.read_page
    calls = []

    def render(*args, **kwargs):
        calls.append(args[2])
        return read(*args, **kwargs)

    monkeypatch.setattr(ingest, "read_page", render)
    first = files.page_bytes(book.id, 0)
    files.page_bytes(book.id, 1)
    assert files.page_bytes(book.id, 0) == first and len(calls) == 3
    assert not files.page_cache.path.exists()
    target = files.root.resolve(book.content.relative_path)
    target.write_bytes(target.read_bytes() + b"changed")
    with pytest.raises(ContentChanged):
        files.page_bytes(book.id, 0)


def test_zip_rejects_unsafe_image_members_and_ambiguous_names(files, tmp_path):
    import warnings
    import zipfile

    from helpers import png

    from mangacouch.archives import InvalidArchive

    source = tmp_path / "windows.cbz"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("001.png", png())
        archive.writestr("notes\\readme.txt", b"made on Windows")
        archive.writestr("__MACOSX/._001.png", b"resource fork")
    book = files.import_file(source).book
    assert book.content.page_count == 1
    link = zipfile.ZipInfo("link.png")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    for name, member in (("parent.cbz", "..\\x.png"), ("absolute.cbz", "/x.png"), ("link.cbz", link)):
        unsafe = tmp_path / name
        with zipfile.ZipFile(unsafe, "w") as archive:
            archive.writestr("001.png", png())
            archive.writestr(member, png())
        # A page left out must not pass for a complete import.
        with pytest.raises(InvalidArchive, match="Unsafe ZIP image member") as error:
            files.import_file(unsafe)
        assert error.value.member in {"../x.png", "/x.png", "link.png"}
    duplicate = tmp_path / "duplicate.cbz"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with zipfile.ZipFile(duplicate, "w") as archive:
            archive.writestr("001.png", png())
            archive.writestr("001.png", png())
    with pytest.raises(InvalidArchive, match="duplicate"):
        files.import_file(duplicate)


def test_pdf_validation_releases_the_pdfium_lock_between_pages(tmp_path, monkeypatch):

    import pypdfium2 as pdfium

    from mangacouch import archives

    path = tmp_path / "book.pdf"
    with archives.PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
        for _ in range(3):
            document.new_page(100, 140).close()
        document.save(path)
    free = []

    def other_reader():
        acquired = archives.PDFIUM_LOCK.acquire(timeout=1)
        if acquired:
            archives.PDFIUM_LOCK.release()
        free.append(acquired)

    def probe(_cancelled):
        thread = threading.Thread(target=other_reader)
        thread.start()
        thread.join()

    monkeypatch.setattr(archives, "check_cancel", probe)
    pages = archives.inspect_archive(path, "pdf", archives.Limits())
    assert len(pages) == 3 and free == [True, True, True]


def test_page_versions_are_immutable_only_for_the_matching_archive(api, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    login(api)
    route = f"/api/books/{book.id}/pages/0"
    versioned = api.get(route + "?v=" + book.content.sha256[:16])
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["cache-control"]
    assert "immutable" not in api.get(route + "?v=outdated").headers["cache-control"]
    assert api.get(route).content == versioned.content


def test_page_errors_use_domain_exceptions(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    with pytest.raises(InvalidInput) as error:
        files.page_bytes(book.id, book.content.page_count)
    assert error.value.code == "page_out_of_range"
    with pytest.raises(InvalidInput) as error:
        files.import_file(tmp_path / "notes.txt")
    assert error.value.code == "unsupported_format"
    with pytest.raises(NotFound):
        Scanner(files).get("missing")


def test_image_byte_size_comes_from_the_manifest(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    with files.library.connection() as db:
        pages = json.loads(db.execute("SELECT pages_json FROM archive_manifest").fetchone()[0])
    (files.root.path / book.content.relative_path).unlink()
    with files.library.connection() as db:
        assert image_bytes(db, book.id) == sum(page["byte_size"] for page in pages)


def test_reading_a_missing_book_records_missing_not_changed(api, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "away.cbz")).book
    path = files.root.resolve(book.content.relative_path)
    path.rename(path.with_suffix(".away"))
    assert api.get(f"/api/books/{book.id}/pages/0").status_code == 409
    assert files.library.get_book(book.id).availability == "available"
    assert api.get(f"/api/books/{book.id}/pages/0").status_code == 409
    assert files.library.get_book(book.id).availability == "missing"
