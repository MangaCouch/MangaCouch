"""Periodic library housekeeping: ready imports, scan history, crash leftovers, cache keys."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace

import pytest
from helpers import finish_scan, inbox, make_zip

from mangacouch.archives import Limits
from mangacouch.ingest import FileLibrary
from mangacouch.scanning import SCAN_RETENTION, Scanner
from mangacouch.services import image_etag


def test_hourly_cleanup_never_publishes_failed_imports(files: FileLibrary, tmp_path, monkeypatch):
    assert files.recover_ready() == {}
    original = files._commit_import
    error = sqlite3.OperationalError("locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    monkeypatch.setattr(files, "_commit_import", lambda *_a, **_k: (_ for _ in ()).throw(error))
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(make_zip(tmp_path / "ready.zip"))
    monkeypatch.setattr(files, "_commit_import", original)
    assert files.recover_ready() == {}  # just failed: its caller may still be handling it
    assert files.recover_ready(settle=-1) == {}
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0
    assert files.recover_ready() == {}


def test_old_finished_scans_are_pruned_but_the_latest_stays(files: FileLibrary):
    scanner = Scanner(files)
    make_zip(inbox(files) / "one.zip")
    first = finish_scan(scanner, scanner.start()["id"])
    second = finish_scan(scanner, scanner.start()["id"])
    assert scanner.purge() == 0
    with files.library.transaction() as db:
        db.execute("UPDATE scan_run SET created_at=?", (int(time.time()) - SCAN_RETENTION - 1,))
    assert scanner.purge() == 1
    with files.library.connection() as db:
        assert [row[0] for row in db.execute("SELECT id FROM scan_run")] == [second["id"]]
        assert db.execute("SELECT count(*) FROM scan_item WHERE scan_id=?", (first["id"],)).fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM import_receipt").fetchone()[0] == 1  # receipts stay
    latest = scanner.latest()
    assert latest is not None and latest["id"] == second["id"]


def test_recovery_removes_temporaries_left_by_a_crash(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "a.zip")).book
    (files.root.path / "shelf").mkdir()
    indexed = files.import_file(make_zip(files.root.path / "shelf" / "b.zip"), index_existing=True).book
    folders = [files.root.resolve(item.content.relative_path).parent for item in (book, indexed)]
    names = [
        ".mangacouch-publish-abc",
        ".mc-012345abcdef.tmp",
    ]
    leftovers = [folder / name for folder in folders for name in names]
    for path in leftovers:
        path.write_bytes(b"partial")
    unrelated = folders[1] / ".x.cbz.mangacouch.json.tmp"
    unrelated.write_bytes(b"keep")
    files.recover()
    assert not any(path.exists() for path in leftovers)
    assert unrelated.exists() and files.page_bytes(book.id, 0) and files.page_bytes(indexed.id, 0)


def test_thumbnail_keys_ignore_limits_that_do_not_change_images(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "a.zip")).book
    other = FileLibrary(files.library, files.root.path, replace(Limits(), max_archive_bytes=10, max_pages=5))
    try:
        assert other.cover_key(book) == files.cover_key(book)
    finally:
        other.page_cache.close()
    smaller = FileLibrary(files.library, files.root.path, replace(Limits(), max_render_pixels=1_000_000))
    try:
        assert smaller.cover_key(book) != files.cover_key(book)
    finally:
        smaller.page_cache.close()


def test_image_etags_ignore_limits_that_do_not_change_images(files: FileLibrary, tmp_path):
    book = files.import_file(make_zip(tmp_path / "a.zip")).book
    tags = {kind: image_etag(files, book, 0, kind) for kind in ("page", "thumbnail")}
    files.limits = replace(files.limits, max_archive_bytes=10, max_pages=5, max_page_bytes=1024)
    assert {kind: image_etag(files, book, 0, kind) for kind in tags} == tags
    files.limits = replace(files.limits, max_dimension=4096)
    assert all(image_etag(files, book, 0, kind) != tag for kind, tag in tags.items())
