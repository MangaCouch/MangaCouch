"""An abandoned import removes only the file it published itself (decision 8 and F-FILE-5).

Equal content is not ownership: the recycle bin keeps the archive of a book removed with
"record only", and a failed or cancelled import of the same bytes must leave that file alone.
"""

from __future__ import annotations

import errno
import sqlite3
from contextlib import closing, contextmanager

import pytest
from helpers import action, download_step, drive, make_zip, ready

from mangacouch.files import hash_file
from mangacouch.ingest import FileLibrary
from mangacouch.limits import ImportCancelled
from mangacouch.removals import Removals
from mangacouch.scanning import Scanner


def record_only_removal(files: FileLibrary, book_id: str) -> tuple[Removals, dict]:
    service = Removals(files)
    preview = service.preview(book_id)
    service.remove(preview["id"], book_id, preview["token"], confirmed=True, delete_file=False)
    return service, preview


def assert_restorable(files: FileLibrary, service: Removals, receipt_id: str, book_id: str) -> None:
    service.restore(receipt_id)
    assert files.library.get_book(book_id).id == book_id


def test_failed_import_keeps_same_content_file_held_by_recycle_bin(files, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    target = files.root.resolve(book.content.relative_path)
    service, preview = record_only_removal(files, book.id)
    assert target.exists()

    def fail(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(files, "_commit_import", fail)
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(make_zip(tmp_path / "again.zip"))
    monkeypatch.undo()
    files.recover()
    assert target.exists()
    assert_restorable(files, service, preview["id"], book.id)


def test_failed_import_keeps_unowned_same_content_file_without_hashing_it(files, tmp_path, monkeypatch):
    """A file placed by hand at the managed path is not the import's to delete, and deciding so
    never hashes a whole archive inside the write transaction."""
    from mangacouch import ingest

    source = make_zip(tmp_path / "book.zip")
    digest = hash_file(source)
    target = files.root.path / digest[:2] / (digest + ".cbz")
    target.parent.mkdir()
    target.write_bytes(source.read_bytes())
    locked = []
    original_hash = ingest.hash_file

    def tracked(path, *args, **kwargs):
        with closing(sqlite3.connect(files.library.path, timeout=0)) as other:
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
            except sqlite3.OperationalError:
                locked.append(str(path))
        return original_hash(path, *args, **kwargs)

    monkeypatch.setattr(ingest, "hash_file", tracked)
    monkeypatch.setattr(
        files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    with pytest.raises(OSError):
        files.import_file(source)
    assert locked == []
    assert target.read_bytes() == source.read_bytes()


def test_failed_import_still_removes_the_file_it_published(files, tmp_path, monkeypatch):
    monkeypatch.setattr(
        files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    with pytest.raises(OSError):
        files.import_file(make_zip(tmp_path / "book.zip"), consume=True)
    assert not list(files.root.path.glob("*/*.cbz"))
    assert (tmp_path / "book.zip").exists()


def test_copied_publication_is_claimed_and_removed_when_the_commit_fails(files, tmp_path, monkeypatch):
    """Without a no-replace rename or hard links the published copy still carries a claim."""
    import os

    from mangacouch import files as storage

    monkeypatch.setattr(storage, "_rename_exclusive", lambda *a: errno.ENOTSUP)
    monkeypatch.setattr(
        os, "link", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EINVAL, "unsupported"))
    )
    monkeypatch.setattr(
        files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    with pytest.raises(OSError):
        files.import_file(make_zip(tmp_path / "book.zip"))
    assert not list(files.root.path.glob("*/*.cbz"))


def test_cancelled_scan_keeps_same_content_file_held_by_recycle_bin(files, tmp_path, monkeypatch):
    archive = make_zip(tmp_path / "book.zip")
    book = files.import_file(archive).book
    target = files.root.resolve(book.content.relative_path)
    service, preview = record_only_removal(files, book.id)
    inbox = files.root.path / "import"
    inbox.mkdir(exist_ok=True)
    (inbox / "again.cbz").write_bytes(archive.read_bytes())
    scanner = Scanner(files)
    run = scanner.start()
    original = type(files.root).mutation

    @contextmanager
    def pausing(root, *args, cancelled=None, **kwargs):
        if cancelled is not None:
            with files.library.connection() as db:
                if db.execute("SELECT 1 FROM import_job WHERE state='ready'").fetchone():
                    scanner.pause(run["id"])  # the pause lands while the import waits to publish
        with original(root, *args, cancelled=cancelled, **kwargs):
            yield

    monkeypatch.setattr(type(files.root), "mutation", pausing)
    for _ in range(5):
        scanner.step(run["id"])
    monkeypatch.undo()
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job WHERE state='ready'").fetchone() is None
        assert db.execute("SELECT count(*) FROM import_job WHERE state='failed'").fetchone()[0] == 1
    assert target.exists() and (inbox / "again.cbz").exists()
    assert_restorable(files, service, preview["id"], book.id)


def test_cancelled_redownload_keeps_same_content_file_held_by_recycle_bin(queue, monkeypatch):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    drive(service)
    book_id = service.get(job["id"])["book_id"]
    files = service.files
    target = files.root.resolve(files.library.get_book(book_id).content.relative_path)
    removals, preview = record_only_removal(files, book_id)

    remote.prepared.clear()
    action(service, service.get(job["id"]), "redownload")
    download_step(service)
    assert service.get(job["id"])["state"] == "needs_approval"
    action(service, service.get(job["id"]), "approve")

    def cancelled(*_args, **_kwargs):
        raise ImportCancelled()

    monkeypatch.setattr(FileLibrary, "_commit_import", cancelled)
    for _ in range(6):
        if not download_step(service):
            break
    monkeypatch.undo()
    latest = service.get(job["id"])
    assert latest["state"] != "done", latest
    assert latest["details"].get("import_job")
    action(service, latest, "cancel")
    assert target.exists()
    assert_restorable(files, removals, preview["id"], book_id)


def test_failed_download_import_keeps_paid_archive_and_resumes_without_paying_again(queue, monkeypatch):
    from source_fixtures import archiver_posts
    from test_downloads import archive_path, due

    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    monkeypatch.setattr(
        FileLibrary, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EIO, "io"))
    )
    for _ in range(3):
        if not download_step(service):
            break
    assert archive_path(service, job).exists()
    assert not list(service.files.root.path.glob("*/*.cbz"))
    monkeypatch.undo()
    due(service)
    action(service, job, "resume")
    drive(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1
