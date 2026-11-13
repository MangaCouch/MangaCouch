from __future__ import annotations

import errno
import json
import zipfile

import pytest
from helpers import finish_scan, inbox, make_zip

from mangacouch.archives import InvalidArchive
from mangacouch.configuration import load_profile
from mangacouch.files import ContentChanged, hash_file
from mangacouch.image_cache import ImageCache
from mangacouch.limits import Limits
from mangacouch.scanning import Scanner
from mangacouch.warming import Warmer


def test_legacy_runtime_defaults_follow_current_limits_but_custom_values_stay(tmp_path):
    path = tmp_path / "runtime.json"
    original = {
        "schema": "mangacouch.runtime/1",
        "library_root_hint": str(tmp_path / "books"),
        "limits": {**vars(Limits()), "max_pixels": 100_000_000, "max_pages": 123},
    }
    path.write_text(json.dumps(original))
    profile = load_profile(tmp_path / "library.sqlite")
    assert profile is not None
    assert profile.limits.max_pixels == Limits().max_pixels
    assert profile.limits.max_pages == 123
    saved = json.loads(profile.model_dump_json(by_alias=True, exclude_defaults=True))
    assert saved["limits"] == {"max_pages": 123}


@pytest.mark.parametrize("suffix", ["jfif", "jpe", "avif", "jxl", "bmp", "tif"])
def test_image_suffixes_are_validated_instead_of_silently_dropped(files, tmp_path, suffix):
    path = make_zip(tmp_path / "image.zip", ["1.png"])
    with zipfile.ZipFile(path, "a") as archive:
        archive.writestr("2." + suffix, b"not a valid image")
    with pytest.raises(InvalidArchive, match="2." + suffix):
        files.import_file(path)
    assert files.library.search("") == []


def test_covers_still_warm_after_thumbnail_budget_is_full(files, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    cache = ImageCache(tmp_path / "cache")
    monkeypatch.setattr(cache, "stored_bytes", lambda: cache.data_bytes)
    warmer = Warmer(files, cache, "covers")
    while warmer.step():
        pass
    assert cache.has(files.cover_key(book))
    assert warmer.summary()["rendered"] == 1
    cache.close()


def test_orphan_wrong_managed_file_is_preserved_and_original_imported(files, tmp_path):
    source = make_zip(tmp_path / "book.zip")
    digest = hash_file(source)
    target = files.root.path / digest[:2] / (digest + ".cbz")
    target.parent.mkdir()
    target.write_bytes(b"foreign content")
    result = files.import_file(source)
    assert result.created and hash_file(target) == digest
    assert target.with_name(target.name + ".changed").read_bytes() == b"foreign content"


def test_single_frontend_read_failure_does_not_mark_book_unavailable(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    files.root.resolve(book.content.relative_path).unlink()
    with pytest.raises(ContentChanged):
        files.page_bytes(book.id, 0)
    assert files.library.get_book(book.id).availability == "available"
    with pytest.raises(ContentChanged):
        files.page_bytes(book.id, 0)
    assert files.library.get_book(book.id).availability == "missing"


def test_failed_ready_import_does_not_publish_on_recovery(files, tmp_path, monkeypatch):
    source = make_zip(tmp_path / "book.zip")
    original = files._commit_import
    monkeypatch.setattr(
        files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
    )
    with pytest.raises(OSError):
        files.import_file(source)
    monkeypatch.setattr(files, "_commit_import", original)
    files.recover()
    assert files.library.search("") == []
    assert not list(files.root.path.glob("*/*.cbz"))
    assert not list((files.root.path / ".mangacouch-staging").glob("*.part"))


def test_appledouble_is_ignored_and_bad_file_does_not_block_scan(files, monkeypatch):
    folder = inbox(files)
    make_zip(folder / "a.cbz", ["a.png"])
    make_zip(folder / "b.cbz", ["b.png"])
    (folder / "._a.cbz").write_bytes(b"AppleDouble")
    original = files.import_file

    def imported(path, **kwargs):
        if path.name == "a.cbz":
            raise OSError(errno.EIO, "bad sector")
        return original(path, **kwargs)

    monkeypatch.setattr(files, "import_file", imported)
    scanner = Scanner(files)
    result = finish_scan(scanner, scanner.start()["id"])
    assert result["state"] == "done"
    assert result["counts"]["error"] == 1 and result["counts"]["indexed"] == 1
    assert result["ignored"] == 1


def test_storage_scan_retries_but_manual_pause_stays_paused(files, monkeypatch):
    make_zip(inbox(files) / "one.cbz")
    scanner = Scanner(files)
    run = scanner.start()
    scanner.step(run["id"])
    original = files.import_file

    def no_space(*a, **k):
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(files, "import_file", no_space)
    scanner.step(run["id"])
    assert scanner.get(run["id"])["error_code"] == "insufficient_storage"
    assert not scanner.resume_storage_wait(run["id"])
    with files.library.transaction() as db:
        db.execute("UPDATE scan_run SET retry_at=0")
    scanner.pause(run["id"])
    assert not scanner.resume_storage_wait(run["id"])
    with files.library.transaction() as db:
        db.execute("UPDATE scan_run SET error_code='insufficient_storage',retry_at=0")
    monkeypatch.setattr(files, "import_file", original)
    assert scanner.resume_storage_wait(run["id"])
    assert finish_scan(scanner, run["id"])["counts"]["indexed"] == 1


def test_scan_replay_keeps_original_import_outcome(files, monkeypatch):
    make_zip(inbox(files) / "one.cbz")
    scanner = Scanner(files)
    run = scanner.start(keep_source=True)
    scanner.step(run["id"])
    sync = scanner.sync.sync_one
    monkeypatch.setattr(scanner.sync, "sync_one", lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        scanner.step(run["id"])
    monkeypatch.setattr(scanner.sync, "sync_one", sync)
    result = finish_scan(scanner, run["id"])
    assert result["counts"]["indexed"] == 1 and result["counts"]["duplicate"] == 0
    assert len(files.library.search("")) == 1


def test_image_cache_counts_rows_once_across_write_batches(tmp_path):
    cache = ImageCache(tmp_path / "cache", batch_size=1)
    cache.put("a", b"a")
    statements = []
    assert cache._db is not None
    cache._db.set_trace_callback(statements.append)
    for n in range(10):
        cache.put(str(n), b"x")
    assert not any("count(*)" in sql.lower() for sql in statements)
    assert cache._count == 11
    cache.close()


def test_pdf_document_reused_and_closed_before_file_deletion(files, tmp_path, monkeypatch):
    from test_images import pdf_book

    from mangacouch import archives
    from mangacouch.removals import Removals

    book, _ = pdf_book(files, tmp_path)
    path = files.root.resolve(book.content.relative_path)
    with archives.PDFIUM_LOCK:
        first = archives._pdf_document(path)
        assert archives._pdf_document(path) is first
    service = Removals(files)
    preview = service.preview(book.id)
    service.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    assert not path.exists()
    assert not any(key[0] == str(path) for key in archives._PDF_DOCUMENTS)


def test_manifest_for_20000_pages_is_parsed_once(files, tmp_path, monkeypatch):
    from mangacouch import ingest

    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    with files.library.transaction() as db:
        original = json.loads(db.execute("SELECT pages_json FROM archive_manifest").fetchone()[0])
        db.execute("UPDATE archive_manifest SET pages_json=?", (json.dumps(original[:1] * 20000),))
    loads = ingest.json.loads
    reads = []

    def tracked(value, *args, **kwargs):
        if isinstance(value, str) and len(value) > 100000:
            reads.append(len(value))
        return loads(value, *args, **kwargs)

    monkeypatch.setattr(ingest.json, "loads", tracked)
    for _ in range(1000):
        files._page_context(book.id, 0, book)
    assert len(reads) == 1


def test_upload_import_has_its_own_long_work_deadline(api, files, tmp_path, monkeypatch):
    import time

    from mangacouch import services
    from mangacouch.files import check_cancel, thread_cancelled

    source = make_zip(tmp_path / "upload.zip")
    original = files.import_file

    def slow(*args, **kwargs):
        time.sleep(0.03)
        check_cancel(thread_cancelled())
        return original(*args, **kwargs)

    monkeypatch.setattr(services, "MUTATION_TIMEOUT", 0.001)
    monkeypatch.setattr(files, "import_file", slow)
    response = api.post(
        "/api/import?filename=upload.cbz",
        content=source.read_bytes(),
        headers={"Content-Type": "application/octet-stream"},
    )
    assert response.status_code == 200, response.text


def test_crash_during_fallback_copy_cleans_only_owned_partial_target(files, tmp_path, monkeypatch):
    from mangacouch import ingest

    original = ingest._rename_new

    def interrupted(source, target, *, claimed=None):
        assert claimed is not None
        target.write_bytes(b"partial")
        claimed(target)
        raise KeyboardInterrupt()

    monkeypatch.setattr(ingest, "_rename_new", interrupted)
    source = make_zip(tmp_path / "book.zip")
    with pytest.raises(KeyboardInterrupt):
        files.import_file(source)
    targets = list(files.root.path.glob("*/*.cbz"))
    assert len(targets) == 1 and targets[0].read_bytes() == b"partial"
    monkeypatch.setattr(ingest, "_rename_new", original)
    files.recover()
    assert not targets[0].exists()
    assert files.library.search("") == [] and source.exists()


def test_einval_hardlink_falls_back_to_copy(files, tmp_path, monkeypatch):
    import os

    monkeypatch.setattr(
        os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported"))
    )
    source = make_zip(tmp_path / "book.zip")
    result = files.import_file(source, consume=True)
    assert result.created
    assert files.root.resolve(result.book.content.relative_path).read_bytes() == source.read_bytes()
