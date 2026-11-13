"""A touched archive with unchanged content can still be deleted with its record (A2 follow-up)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing

from helpers import import_book

from mangacouch.removals import Removals


def touch(path) -> None:
    stamp = path.stat()
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 5_000_000_000))


def test_touched_identical_archive_is_rehashed_and_can_be_deleted(files, tmp_path, monkeypatch):
    from mangacouch import ingest

    book = import_book(files, tmp_path)
    path = files.root.resolve(book.content.relative_path)
    touch(path)
    locked = []
    original = ingest.hash_file

    def outside_writes(target, *args, **kwargs):
        with closing(sqlite3.connect(files.library.path, timeout=0)) as other:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
            except sqlite3.OperationalError:
                locked.append(target)
        return original(target, *args, **kwargs)

    monkeypatch.setattr(ingest, "hash_file", outside_writes)
    removals = Removals(files)
    preview = removals.preview(book.id)
    assert preview["delete_blocked"] == [] and locked == []
    removals.remove(preview["id"], book.id, preview["token"], confirmed=True, delete_file=True)
    assert not path.exists()


def test_selected_preview_rehashes_each_touched_archive_and_still_blocks_a_changed_one(files, tmp_path):
    same = import_book(files, tmp_path, "same", ["1.png"])
    other = import_book(files, tmp_path, "other", ["1.png", "2.png"])
    touch(files.root.resolve(same.content.relative_path))
    changed = files.root.resolve(other.content.relative_path)
    changed.write_bytes(b"replacement file owned by the user")
    removals = Removals(files)
    preview = removals.preview_selected([same.id, other.id])
    blocked = {item["book_id"]: item["delete_blocked"] for item in preview["items"]}
    assert blocked == {same.id: [], other.id: ["archive_changed"]}
    # A second preview needs no hashing for the archive whose new stamp was recorded.
    with files.library.connection() as db:
        stamps = dict(db.execute("SELECT book_id,file_stamp FROM archive_manifest"))
    assert removals.preview(same.id)["delete_blocked"] == []
    with files.library.connection() as db:
        assert dict(db.execute("SELECT book_id,file_stamp FROM archive_manifest")) == stamps
    assert changed.read_bytes() == b"replacement file owned by the user"
