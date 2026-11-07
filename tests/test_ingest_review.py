"""Regression tests for import job states, cheap root checks, linked imports and review I/O."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading

import pytest
from helpers import finish_scan as finish
from helpers import inbox, make_zip, save_metadata

from mangacouch import files as files_module
from mangacouch import ingest
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.files import STAGING, ContentChanged, RootUnavailable
from mangacouch.image_cache import ImageCache
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, Source, canonical_json
from mangacouch.review import Review
from mangacouch.scanning import Scanner


def job_rows(files: FileLibrary) -> list[dict]:
    with files.library.connection() as db:
        return [dict(row) for row in db.execute("SELECT * FROM import_job ORDER BY rowid")]


def test_deterministic_failure_after_ready_marks_job_failed_and_frees_stage(
    files: FileLibrary, tmp_path, monkeypatch
):
    source = make_zip(tmp_path / "sample.zip")

    def changed(*_args, **_kwargs):
        raise ContentChanged("existing library file changed")

    monkeypatch.setattr(files, "_commit_import", changed)
    with pytest.raises(ContentChanged):
        files.import_file(source)
    [job] = job_rows(files)
    assert job["state"] == "failed" and job["error_code"] == "content_changed"
    assert job["pages_json"] is None and job["report_json"] is None
    assert list((files.root.path / STAGING).iterdir()) == []
    monkeypatch.undo()
    assert files.recover() == {}
    assert job_rows(files)[0]["state"] == "failed"


def test_transient_failure_after_ready_stays_recoverable(files: FileLibrary, tmp_path, monkeypatch):
    source = make_zip(tmp_path / "sample.zip")
    original = files._commit_import

    def locked(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(files, "_commit_import", locked)
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(source)
    assert job_rows(files)[0]["state"] == "ready"
    monkeypatch.setattr(files, "_commit_import", original)
    assert list(files.recover().values()) == ["recovered"]
    [job] = job_rows(files)
    assert job["state"] == "done" and job["pages_json"] is None and job["report_json"] is None


def test_recover_turns_a_deterministic_ready_failure_into_failed(files: FileLibrary, tmp_path, monkeypatch):
    source = make_zip(tmp_path / "sample.zip")
    monkeypatch.setattr(
        files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError())
    )
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(source)
    monkeypatch.undo()
    stage = files.root.path / STAGING / f"{job_rows(files)[0]['id']}.part"
    stage.write_bytes(b"tampered")
    assert list(files.recover().values()) == ["content_changed"]
    assert job_rows(files)[0]["state"] == "failed" and not stage.exists()
    assert files.recover() == {}


def test_recover_ignores_finished_jobs_and_clears_orphan_stages(files: FileLibrary, tmp_path, monkeypatch):
    files.import_file(make_zip(tmp_path / "sample.zip"))
    orphan = files.root.path / STAGING / "00000000-0000-4000-8000-000000000000.part"
    orphan.write_bytes(b"left over")
    calls = []
    monkeypatch.setattr(files, "_cleanup_stage", lambda job_id: calls.append(job_id))
    assert files.recover() == {}
    assert calls == [] and not orphan.exists()


def test_root_check_is_cached_briefly_but_notices_a_missing_root(files: FileLibrary, tmp_path, monkeypatch):
    calls = []
    original = files_module._check_root
    monkeypatch.setattr(
        files_module, "_check_root", lambda *a, **kw: calls.append(kw.get("writable")) or original(*a, **kw)
    )
    files.root.check()
    files.root.check()
    files.root.check()
    assert calls == [False]
    files.root.check(writable=True)
    assert calls == [False, True]
    files.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        files.root.check()
    with pytest.raises(RootUnavailable):
        files.root.check()


def test_cached_cover_skips_page_context_but_not_availability(files: FileLibrary, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    cache = ImageCache(tmp_path / "covers")
    try:
        first = files.cover_bytes(book.id, cache)
        monkeypatch.setattr(files, "_page_context", lambda *a: pytest.fail("cache hit parsed the manifest"))
        assert files.cover_bytes(book.id, cache) == first
        (files.root.path / book.content.relative_path).unlink()
        with pytest.raises(ContentChanged):
            files.cover_bytes(book.id, cache)
    finally:
        cache.close()


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
    assert files.image_byte_size(book.id) == sum(page["byte_size"] for page in pages)


def test_consumed_import_links_instead_of_copying_and_hashes_once(files: FileLibrary, monkeypatch):
    source = make_zip(inbox(files) / "linked.cbz")
    hashed = []
    original = ingest.hash_file
    monkeypatch.setattr(ingest, "hash_file", lambda path, *a: hashed.append(path) or original(path, *a))
    monkeypatch.setattr(files, "_copy", lambda *a: pytest.fail("consumed same-device import was copied"))
    book = files.import_file(source, consume=True).book
    target = files.root.path / book.content.relative_path
    assert target.stat().st_ino == source.stat().st_ino
    assert len(hashed) == 1
    assert list((files.root.path / STAGING).iterdir()) == []


def test_kept_scan_source_is_copied_and_consumed_source_is_removed(files: FileLibrary):
    kept = make_zip(inbox(files) / "kept.cbz", ["k.png"])
    scanner = Scanner(files)
    finish(scanner, scanner.start(keep_source=True)["id"])
    book = files.library.get_book(files.library.search("")[0])
    assert (files.root.path / book.content.relative_path).stat().st_ino != kept.stat().st_ino
    consumed = make_zip(inbox(files) / "consumed.cbz", ["c.png"])
    result = finish(scanner, scanner.start()["id"])
    assert not consumed.exists() and result["counts"]["indexed"] == 1


def test_retained_consumed_source_is_detached_from_library_file(files: FileLibrary, monkeypatch):
    source = make_zip(inbox(files) / "warned.cbz")
    original = files.import_file

    def with_warning(*args, **kwargs):
        result = original(*args, **kwargs)
        return ingest.ImportResult(result.book, result.created, result.job_id, ("eze_unreadable_or_invalid",))

    monkeypatch.setattr(files, "import_file", with_warning)
    scanner = Scanner(files)
    finish(scanner, scanner.start()["id"])
    book = files.library.get_book(files.library.search("")[0])
    assert source.exists()
    assert source.stat().st_ino != (files.root.path / book.content.relative_path).stat().st_ino


def test_scan_errors_have_stable_codes_and_sidecars_are_not_ignored(files: FileLibrary, tmp_path):
    folder = inbox(files)
    (folder / "broken.cbz").write_bytes(b"not a zip")
    paired = make_zip(folder / "paired.cbz")
    paired.with_name(paired.name + ".mangacouch.json").write_text("{}")
    paired.with_suffix(".eze.json").write_text("{}")
    (folder / "readme.txt").write_text("unrelated")
    scanner = Scanner(files)
    result = finish(scanner, scanner.start(keep_source=True)["id"])
    [error] = [item for item in result["items"] if item["state"] == "error"]
    assert error["error_code"] == "invalid_archive" and error["error_message"]
    assert result["ignored"] == 1


def test_start_applies_keep_source_both_ways_and_resumes_a_paused_run(files: FileLibrary):
    scanner = Scanner(files)
    identifier = scanner.start(keep_source=True)["id"]
    scanner.pause(identifier)
    restarted = scanner.start(keep_source=False)
    assert restarted["id"] == identifier and restarted["state"] == "active"
    assert restarted["keep_source"] is False
    assert scanner.start(keep_source=True)["keep_source"] is True


def test_review_never_nests_write_transactions_after_a_remount(files: FileLibrary, tmp_path, monkeypatch):
    path = make_zip(tmp_path / "eh.zip")
    book = files.import_file(path).book
    source = Source(site="ehentai", gallery_id=1, gallery_token="abc")
    metadata = Metadata(title="t", language="English", source=source, declared_byte_size=1)
    save_metadata(files.library, book.id, metadata, expected_revision=1)
    with files.library.transaction() as db:
        observation = {"content_sha256": book.content.sha256}
        db.execute(
            "INSERT INTO source_observation VALUES(?,?,?)", (book.id, source.url, canonical_json(observation))
        )
        report = {"candidates": {"online:ehentai": metadata.model_dump(mode="json")}}
        db.execute(
            "INSERT INTO metadata_import(book_id,report_json) VALUES(?,?)", (book.id, canonical_json(report))
        )
        # A remount changes st_dev, so the recorded stamp no longer matches.
        db.execute("UPDATE archive_manifest SET file_stamp='[0,0,0,0]'")
    depth = threading.local()
    original = files.library.transaction

    @contextlib.contextmanager
    def tracked():
        assert getattr(depth, "value", 0) == 0, "nested write transaction"
        depth.value = 1
        try:
            with original() as db:
                yield db
        finally:
            depth.value = 0

    monkeypatch.setattr(files.library, "transaction", tracked)
    service = Review(files)
    preview = service.preview(book.id)
    assert preview["byte_size_basis"] == "images" and preview["compared_byte_size"] == files.image_byte_size(
        book.id
    )
    service.edit(book.id, metadata.model_copy(update={"title": "new"}), expected_revision=2)
    with files.library.connection() as db:
        assert db.execute("SELECT file_stamp FROM archive_manifest").fetchone()[0] != "[0,0,0,0]"
