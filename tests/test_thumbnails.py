from __future__ import annotations

import sqlite3
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
import pyvips
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, login, make_zip, save_metadata

from mangacouch import ingest
from mangacouch.archives import PDFIUM_LOCK, InvalidArchive
from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.files import ContentChanged, RootUnavailable
from mangacouch.http import create_app
from mangacouch.image_cache import ImageCache
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState
from mangacouch.snapshots import create_snapshot, restore_snapshot


def cache(tmp_path: Path, **kwargs) -> ImageCache:
    return ImageCache(tmp_path / "cache", **kwargs)


def image_archive(tmp_path: Path, width=1800, height=2800, bands=None) -> Path:
    image: Any = pyvips.Image.black(width, height).new_from_image(bands or [80, 140, 200])
    if bands and len(bands) == 4:
        image = image.copy(interpretation="srgb")
    path = tmp_path / "large.cbz"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("1.png", image.write_to_buffer(".png"))
    return path


@pytest.mark.parametrize("dimensions", [(1800, 2800), (2800, 900), (10, 12)])
def test_zip_cover_is_bounded_webp_and_original_and_progress_are_unchanged(
    files: FileLibrary, tmp_path, dimensions
):
    source = image_archive(tmp_path, *dimensions)
    original = source.read_bytes()
    book = files.import_file(source).book
    before = files.library.get_book(book.id)
    data = files.cover_bytes(book.id, cache(tmp_path))
    assert data.startswith(b"RIFF")
    image: Any = pyvips.Image.new_from_buffer(data, "")
    assert 0 < image.width <= 480 and 0 < image.height <= 720 and image.bands == 3
    assert image.width <= dimensions[0] and image.height <= dimensions[1]
    assert abs(image.width / image.height - dimensions[0] / dimensions[1]) < 0.01
    assert files.library.get_book(book.id) == before and source.read_bytes() == original
    assert (files.root.path / book.content.relative_path).read_bytes() == original


def test_webp_preserves_alpha_and_jpeg_cover_is_flattened_over_white(files: FileLibrary, tmp_path):
    book = files.import_file(image_archive(tmp_path, 60, 80, [0, 0, 0, 0])).book
    image: Any = pyvips.Image.new_from_buffer(files.cover_bytes(book.id, cache(tmp_path)), "")
    assert image.hasalpha() and image[3].avg() == 0
    jpeg = pyvips.Image.new_from_buffer(files.cover_bytes(book.id, cache(tmp_path), format="jpeg"), "")
    assert jpeg.bands == 3 and jpeg.avg() > 250


def test_pdf_cover_renders_small_and_keeps_full_reader_resolution(files: FileLibrary, tmp_path):
    path = tmp_path / "book.pdf"
    with PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
        document.new_page(1000, 1400).close()
        document.save(path)
    book = files.import_file(path).book
    cover: Any = pyvips.Image.new_from_buffer(files.cover_bytes(book.id, cache(tmp_path)), "")
    full: Any = pyvips.Image.new_from_buffer(files.page_bytes(book.id, 0), "")
    assert cover.width <= 480 and cover.height <= 720
    assert (full.width, full.height) == (2000, 2800)


def test_hot_memory_and_disk_hits_do_not_decode_archive_or_update_main_db(
    files: FileLibrary, tmp_path, monkeypatch
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    service = cache(tmp_path)
    value = files.cover_bytes(book.id, service)
    service.close()
    with files.library.connection() as db:
        before = list(db.iterdump())
    disk_stamp = service.path.stat().st_mtime_ns

    def fail(*args, **kwargs):
        raise AssertionError("Cache hit must not decode the original")

    monkeypatch.setattr(ingest, "read_page", fail)
    reopened = cache(tmp_path)
    assert files.cover_bytes(book.id, reopened) == value
    assert files.cover_bytes(book.id, reopened) == value
    assert reopened.path.stat().st_mtime_ns == disk_stamp
    with files.library.connection() as db:
        assert list(db.iterdump()) == before


@pytest.mark.parametrize("failure", ["full", "corrupt", "locked", "symlink", "foreign"])
def test_cache_storage_failure_does_not_break_cover_or_main_state(
    files: FileLibrary, tmp_path, monkeypatch, failure
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    files.library.save_personal(book.id, PersonalState(rating=4.5, bookmarks=[0]), expected_revision=1)
    before = files.library.get_book(book.id)
    service = cache(tmp_path, batch_size=1)
    service.directory.mkdir()
    lock = None
    if failure == "full":

        def full(_batch):
            raise OSError("disk full fixture")

        monkeypatch.setattr(service, "_write_batch", full)
    elif failure == "corrupt":
        service.path.write_bytes(b"broken database")
    elif failure == "symlink":
        service.path.symlink_to(files.library.path)
    elif failure == "foreign":
        with closing(sqlite3.connect(service.path)) as db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
    else:
        service.put("seed", b"seed")
        lock = sqlite3.connect(service.path)
        lock.execute("BEGIN EXCLUSIVE")
    try:
        value = files.cover_bytes(book.id, service)
        assert value.startswith(b"RIFF")
        assert files.cover_bytes(book.id, service) == value
        assert files.library.get_book(book.id) == before
    finally:
        if lock:
            lock.close()
    if failure in {"full", "locked"}:
        # Disk trouble only costs re-rendering; the cache keeps trying later.
        assert service.disk_error is not None
    else:
        # A foreign, corrupt or redirected file is replaced by a fresh cache, never written through.
        assert service.disk_error is None and not service.path.is_symlink()
        with files.library.connection() as db:
            assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='image'").fetchone()[0] == 0
        service.close()
        assert cache(tmp_path).get(next(iter(cache_keys(service)))) == value


def cache_keys(service) -> list[str]:
    with closing(sqlite3.connect(service.path)) as db:
        return [row[0] for row in db.execute("SELECT key FROM image")]


def test_corrupt_cache_file_is_rebuilt_and_cache_can_be_deleted_and_rebuilt(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    service = cache(tmp_path, batch_size=1)
    original = files.cover_bytes(book.id, service)
    service.close()
    data = bytearray(service.path.read_bytes())
    data[:100] = b"\xff" * 100
    service.path.write_bytes(bytes(data))
    service = cache(tmp_path, batch_size=1)
    assert files.cover_bytes(book.id, service) == original
    service.close()
    assert len(cache_keys(service)) == 1
    service.path.unlink()
    assert files.cover_bytes(book.id, cache(tmp_path, batch_size=1)) == original
    assert service.path.is_file()


def test_cache_does_not_bypass_changed_or_missing_archive_or_offline_root(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    service = cache(tmp_path)
    files.cover_bytes(book.id, service)
    archive = files.root.path / book.content.relative_path
    original = archive.read_bytes()
    archive.write_bytes(b"changed")
    with pytest.raises(ContentChanged):
        files.cover_bytes(book.id, service)
    archive.unlink()
    with pytest.raises(ContentChanged):
        files.cover_bytes(book.id, service)
    archive.write_bytes(original)
    assert files.cover_bytes(book.id, service).startswith(b"RIFF")
    files.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        files.cover_bytes(book.id, service)


@pytest.mark.parametrize("thumbnail", [False, True])
def test_replacement_after_availability_check_cannot_be_rendered_or_cached(
    files: FileLibrary, tmp_path, monkeypatch, thumbnail
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    check = files.availability
    service = cache(tmp_path)

    def replaced(item):
        result = check(item)
        (files.root.path / item.content.relative_path).write_bytes(b"changed after check")
        return result

    monkeypatch.setattr(files, "availability", replaced)
    with pytest.raises(ContentChanged):
        if thumbnail:
            files.cover_bytes(book.id, service)
        else:
            files.page_bytes(book.id, 0)
    assert not service._pending and not service._memory


def test_metadata_edit_keeps_cover_but_tighter_decode_limits_do_not_reuse_old_cache(
    files: FileLibrary, tmp_path, monkeypatch
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    service = cache(tmp_path)
    value = files.cover_bytes(book.id, service)
    save_metadata(files.library, book.id, Metadata(title="new title"), expected_revision=1)
    assert files.cover_bytes(book.id, service) == value
    files.limits = replace(files.limits, max_dimension=5)
    with pytest.raises(InvalidArchive):
        files.cover_bytes(book.id, service)


def test_cover_http_checks_auth_and_original_before_returning_304(
    client: TestClient, files: FileLibrary, auth: Auth, tmp_path
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    url = f"/api/books/{book.id}/cover"
    assert client.get(url).status_code == 401
    login(client)
    response = client.get(url)
    assert response.status_code == 200 and response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["vary"] == "Cookie, Authorization"
    condition = {"If-None-Match": response.headers["etag"]}
    assert client.get(url, headers=condition).status_code == 304
    client.post("/api/auth/logout", headers=CSRF)
    assert client.get(url, headers=condition).status_code == 401
    identifier, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}", **condition}
    assert client.get(url, headers=headers).status_code == 304
    (files.root.path / book.content.relative_path).unlink()
    # Validators come from registered content, so a cached copy stays valid; a fresh render fails.
    assert client.get(url, headers=headers).status_code == 304
    assert client.get(url, headers={"Authorization": headers["Authorization"]}).status_code == 409
    auth.revoke(identifier)
    assert client.get(url, headers=headers).status_code == 401


def test_service_shutdown_flushes_and_cache_is_excluded_from_snapshot(
    files: FileLibrary, auth: Auth, tmp_path
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    service = cache(tmp_path)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth, thumbnail_cache=service)
    ) as client:
        login(client)
        assert client.get(f"/api/books/{book.id}/cover").status_code == 200
        assert not service.path.exists()
    assert service.path.is_file()
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(snapshot) as archive:
        assert set(archive.namelist()) == {"library.sqlite", "runtime.json", "manifest.json"}
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    restored_files = FileLibrary(restored.library, files.root.path)
    assert restored.library.get_book(book.id).personal == PersonalState()
    assert restored_files.cover_bytes(book.id, cache(tmp_path / "rebuilt")).startswith(b"RIFF")


def test_cold_cover_work_does_not_use_the_reader_operation_slot(
    files: FileLibrary, auth: Auth, tmp_path, monkeypatch
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    started, release = threading.Event(), threading.Event()
    original = files.cover_bytes

    def slow(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(files, "cover_bytes", slow)
    with TestClient(create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth)) as client:
        login(client)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(client.get, f"/api/books/{book.id}/cover")
            assert started.wait(5)
            try:
                assert client.get(f"/api/books/{book.id}/pages/0").status_code == 200
            finally:
                release.set()
            assert future.result().status_code == 200
