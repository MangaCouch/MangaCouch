"""Regressions for replaced import files, deletion cancellation and schema 2 stamps."""

import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
from helpers import finish_scan, import_book, make_zip, png

from mangacouch import migrate
from mangacouch.errors import InvalidInput
from mangacouch.files import file_stamp, hash_file
from mangacouch.removals import Removals
from mangacouch.scanning import Scanner


def replace_content(path, *, inplace=False):
    before = path.stat()
    with zipfile.ZipFile(path) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    old = entries[0][1]
    replacement = next(png(red) for red in range(30, 255) if len(png(red)) == len(old) and png(red) != old)
    other = path.with_suffix(".replacement")
    with zipfile.ZipFile(other, "w") as archive:
        for index, (info, data) in enumerate(entries):
            archive.writestr(info, replacement if index == 0 else data)
    assert other.stat().st_size == before.st_size
    if inplace:
        path.write_bytes(other.read_bytes())
        other.unlink()
    else:
        os.replace(other, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))


@pytest.mark.parametrize("inplace", [False, True])
def test_same_size_mtime_replacement_cannot_be_deleted(files, tmp_path, inplace):
    book = import_book(files, tmp_path)
    path = files.root.resolve(book.content.relative_path)
    stamp = file_stamp(path)
    replace_content(path, inplace=inplace)
    assert file_stamp(path) != stamp
    assert files.availability(book)[0] == "changed"
    removals = Removals(files)
    plan = removals.preview(book.id)
    assert plan["delete_blocked"] == ["archive_changed"]
    with pytest.raises(InvalidInput):
        removals.remove(plan["id"], book.id, plan["token"], confirmed=True, delete_file=True)
    assert path.exists()


def test_file_stamp_matches_open_file_and_is_stable_across_reads(tmp_path):
    path = tmp_path / "readonly.bin"
    path.write_bytes(b"unchanged")
    path.chmod(0o444)
    try:
        before = file_stamp(path)
        with path.open("rb") as stream:
            value = os.fstat(stream.fileno())
            assert before[:4] == (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            assert stream.read() == b"unchanged"
        assert file_stamp(path) == before
    finally:
        path.chmod(0o666)


def test_file_stamp_rejects_missing_files_and_directories(tmp_path):
    with pytest.raises(FileNotFoundError):
        file_stamp(tmp_path / "missing")
    with pytest.raises(OSError, match="Expected a regular file"):
        file_stamp(tmp_path)


def test_file_stamp_does_not_follow_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"keep")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError, match="Expected a regular file"):
        file_stamp(link)
    assert target.read_bytes() == b"keep"


def test_new_inbox_content_with_old_size_mtime_is_imported(files, tmp_path):
    incoming = files.root.path / "import"
    incoming.mkdir()
    source = make_zip(incoming / "incoming.cbz")
    original = source.read_bytes()
    stamp = source.stat()
    scanner = Scanner(files)
    finish_scan(scanner, scanner.start()["id"])
    assert not source.exists()
    source.write_bytes(original)
    replace_content(source)
    os.utime(source, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    digest = hash_file(source)
    result = finish_scan(scanner, scanner.start()["id"])
    assert result["counts"]["indexed"] == 1
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 2
        assert db.execute("SELECT 1 FROM book WHERE sha256=?", (digest,)).fetchone()
    assert not source.exists()


def test_cancel_waits_for_current_unlink_and_returns_accurate_receipt(files, tmp_path, monkeypatch):
    book = import_book(files, tmp_path)
    archive = files.root.resolve(book.content.relative_path)
    service = Removals(files)
    plan = service.preview(book.id)
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == archive:
            raise PermissionError("transient lock")
        return unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", fail)
        with pytest.raises(PermissionError):
            service.remove(plan["id"], book.id, plan["token"], confirmed=True, delete_file=True)
    arrived, proceed, cancelling, returned = (threading.Event() for _ in range(4))
    observe = service._observe
    errors, result = [], []

    def gated(relative):
        arrived.set()
        assert proceed.wait(5)
        return observe(relative)

    monkeypatch.setattr(service, "_observe", gated)

    def run(task):
        try:
            task()
        except BaseException as error:
            errors.append(error)

    def cancel():
        cancelling.set()
        result.append(service.cancel_deletion(plan["id"]))
        returned.set()

    worker = threading.Thread(target=lambda: run(lambda: service.finish_deletion(plan["id"])))
    stopper = threading.Thread(target=lambda: run(cancel))
    worker.start()
    try:
        assert arrived.wait(5)
        stopper.start()
        assert cancelling.wait(5)
        assert not returned.wait(0.1)
    finally:
        proceed.set()
        worker.join(5)
        if stopper.ident:
            stopper.join(5)
    assert not errors
    assert returned.is_set() and not archive.exists()
    assert result[0]["files_kept"] is False and result[0]["delete_pending"] is False


def test_schema_one_stamps_are_invalidated_without_trusting_current_files(files, tmp_path):
    book = import_book(files, tmp_path)
    service = Removals(files)
    plan = service.preview(book.id)
    service.remove(plan["id"], book.id, plan["token"], confirmed=True)
    another = import_book(files, tmp_path, "another", ["1.png", "2.png"])
    with files.library.connection() as db:
        db.execute("PRAGMA user_version=1")
        db.execute("UPDATE archive_manifest SET file_stamp='[123,456]'")
        db.execute("INSERT INTO import_receipt VALUES('old','{}',?)", (another.id,))
    migrate.upgrade(files.library.path)
    with files.library.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT file_stamp FROM archive_manifest").fetchone()[0] == "null"
        assert not db.execute("SELECT * FROM import_receipt").fetchall()
        receipt, rows = db.execute("SELECT receipt_json,rows_json FROM removal").fetchone()
        assert json.loads(receipt)["items"][0]["file_stamp"] is None
        for table, records in json.loads(rows)[book.id]["tables"]:
            if table == "archive_manifest":
                assert records[0]["file_stamp"] == "null"
    assert service.restore(plan["id"])["restored"]
