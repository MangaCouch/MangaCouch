from __future__ import annotations

import errno
import json
import os
import sqlite3
import struct
from contextlib import closing
from dataclasses import replace

import pytest
from helpers import make_zip

from mangacouch.archives import InvalidArchive, check_zip_directory, inspect_archive
from mangacouch.cursors import decode_cursor, encode_cursor
from mangacouch.files import file_stamp, live_stamp
from mangacouch.image_cache import ImageCache
from mangacouch.limits import Limits
from mangacouch.removals import Removals
from mangacouch.searching import SearchOptions, search_ids


def test_zip_lying_entry_count_is_rechecked(tmp_path):
    path = make_zip(tmp_path / "lying.cbz", ["1.png", "2.png", "3.png"])
    data = bytearray(path.read_bytes())
    end = data.rfind(b"PK\x05\x06")
    struct.pack_into("<HH", data, end + 8, 1, 1)
    path.write_bytes(data)
    limits = replace(Limits(), max_entries=2)
    check_zip_directory(path, limits)
    with pytest.raises(InvalidArchive, match="directory"):
        inspect_archive(path, "cbz", limits)


def test_zip_undeclared_zip64_locator_is_rejected(tmp_path):
    path = make_zip(tmp_path / "lying.cbz")
    data = path.read_bytes()
    end = data.rfind(b"PK\x05\x06")
    path.write_bytes(data[:end] + struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1) + data[end:])
    with pytest.raises(InvalidArchive, match="Undeclared"):
        check_zip_directory(path, Limits())


def test_persisted_stamp_and_removal_fast_path(files, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "sample.cbz")).book
    path = files.root.resolve(book.content.relative_path)
    stamp = file_stamp(path)
    assert stamp[:4] == live_stamp(path)
    if os.name != "nt":  # Windows st_ctime is creation time; the stamp uses native ChangeTime.
        assert stamp[4] == path.stat().st_ctime_ns
    service = Removals(files)
    preview = service.preview(book.id)
    receipt = service.remove(preview["id"], book.id, preview["token"], confirmed=True)

    def no_hash(*args, **kwargs):
        pytest.fail("Unchanged file must not be hashed")

    monkeypatch.setattr("mangacouch.removals.hash_file", no_hash)
    assert service.restore(receipt["id"])["restored"]
    preview = service.preview(book.id)
    removed = service.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    assert removed["items"][0]["file"] == "deleted"


def test_failed_import_never_retries_publication(files, tmp_path, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(files, "_commit_import", fail)
    with pytest.raises(OSError):
        files.import_file(make_zip(tmp_path / "sample.cbz"))
    assert files.recover() == {}
    assert files.recover_ready(0) == {}
    assert calls == [1]
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"
    assert not list(files.root.path.glob("*/*.cbz"))


def test_recent_cursor_retains_sort_key_when_book_moves(files, tmp_path):
    books = [files.import_file(make_zip(tmp_path / f"{n}.cbz", [f"{n}.png"])).book for n in range(3)]
    with files.library.transaction() as db:
        db.executemany(
            "UPDATE book SET recent_order=? WHERE id=?", [(-3 + n, b.id) for n, b in enumerate(books)]
        )
        cursor = encode_cursor(-3, books[0].id)
        db.execute("UPDATE book SET recent_order=0 WHERE id=?", (books[0].id,))
        order, identifier = decode_cursor(cursor, int, str, signed=True)
        ids = search_ids(
            db, "", limit=2, after_id=identifier, after_order=order, options=SearchOptions(order="recent")
        )
        assert ids == [b.id for b in books[1:]]


def test_cache_keeps_covers_before_page_thumbnails(tmp_path):
    cache = ImageCache(tmp_path, disk_bytes=65536, batch_size=1, memory_bytes=1)
    cache.put("cover", b"x" * 2100, cover=True)
    for n in range(30):
        cache.put(str(n), b"x" * 2100)
    assert cache.get("cover") is not None
    assert cache.get("0") is None
    assert cache.get("29") is not None
    with closing(sqlite3.connect(cache.path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_tag_counts_follow_remove_restore(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "sample.cbz")).book
    with files.library.transaction() as db:
        from mangacouch.models import Metadata, Tag

        files.library.write_metadata(
            db, book.id, Metadata(tags=[Tag(namespace="artist", value="test")]), book.metadata_revision
        )
    service = Removals(files)
    preview = service.preview(book.id)
    receipt = service.remove(preview["id"], book.id, preview["token"], confirmed=True)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM tag_count").fetchone()[0] == 0
    service.restore(receipt["id"])
    with files.library.connection() as db:
        assert tuple(db.execute("SELECT * FROM tag_count").fetchone()) == ("artist", "test", 1)
        assert len(json.loads(db.execute("SELECT file_stamp FROM archive_manifest").fetchone()[0])) == 5
