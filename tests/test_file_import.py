from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium
import pytest
import pyvips
from helpers import (
    CRASH_AFTER,
    CSRF,
    login,
    make_zip,
    parts,
    png,
    python,
)

from mangacouch import ingest
from mangacouch.archives import (
    PDFIUM_LOCK,
    ImportCancelled,
    InvalidArchive,
    Limits,
    _pdf_size,
    inspect_archive,
)
from mangacouch.database import Library
from mangacouch.files import (
    ROOT_MARKER,
    STAGING,
    ContentChanged,
    LibraryRoot,
    RootUnavailable,
)
from mangacouch.ingest import FileLibrary
from mangacouch.models import PersonalState


def test_import_zip_is_readable_without_metadata_and_source_is_unchanged(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "原名 😀.zip")
    original = source.read_bytes()
    result = files.import_file(source)
    digest = hashlib.sha256(original).hexdigest()
    assert result.created and result.book.content.sha256 == digest
    assert result.book.content.relative_path == f"{digest[:2]}/{digest}.cbz"
    assert source.read_bytes() == original
    assert (files.root.path / result.book.content.relative_path).read_bytes() == original
    assert result.book.review_state == "needs_review" and result.book.content.page_count == 3
    assert files.library.search("原名") == [result.book.id]
    assert files.page_bytes(result.book.id, 0) == png(22)
    assert files.page_bytes(result.book.id, 1) == png(21)
    assert files.page_bytes(result.book.id, 2) == png(20)
    assert list((files.root.path / STAGING).iterdir()) == []
    with pytest.raises(ValueError):
        files.page_bytes(result.book.id, -1)


def test_import_result_read_failure_rolls_back_before_commit(files, tmp_path, monkeypatch):
    def failed_read(book_id, db=None):
        raise sqlite3.OperationalError("Injected import result read failure")

    monkeypatch.setattr(files.library, "get_book", failed_read)
    with pytest.raises(sqlite3.OperationalError, match="Injected import result"):
        files.import_file(make_zip(tmp_path / "book.cbz"))
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"
    assert not list(files.root.path.glob("*/*.cbz"))
    assert not list((files.root.path / STAGING).iterdir())


def test_committed_import_survives_staging_cleanup_failure(files, tmp_path, monkeypatch):
    def failed_cleanup(job_id):
        raise OSError(5, "Injected staging cleanup failure")

    with monkeypatch.context() as patch:
        patch.setattr(files, "_cleanup_stage", failed_cleanup)
        result = files.import_file(make_zip(tmp_path / "book.cbz"))
    assert result.created
    assert "staging_cleanup_pending" in result.warnings
    files.recover()
    assert files.library.get_book(result.book.id) == result.book
    assert files.page_bytes(result.book.id, 0) == png(22)
    assert not list((files.root.path / STAGING).iterdir())


def test_pdf_import_and_render_close_all_native_resources(files: FileLibrary, tmp_path: Path):
    source = tmp_path / "book.pdf"
    with PDFIUM_LOCK, closing(pdfium.PdfDocument.new()) as document:
        for width, height in ((100, 150), (200, 100)):
            document.new_page(width, height).close()
        document.save(source)
    original = source.read_bytes()
    result = files.import_file(source)
    assert result.book.content.page_count == 2 and result.book.content.format == "pdf"
    for index, size in enumerate(((200, 300), (400, 200))):
        image: Any = pyvips.Image.new_from_buffer(files.page_bytes(result.book.id, index), "")
        assert (image.width, image.height) == size
    assert source.read_bytes() == original
    assert files.page_bytes(result.book.id, 0).startswith(b"RIFF")


def test_parallel_imports_publish_one_file_and_keep_personal_state(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "same.zip")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(
                lambda _: FileLibrary(Library(files.library.path), files.root.path).import_file(source),
                range(8),
            )
        )
    assert sum(result.created for result in results) == 1
    assert len({result.book.id for result in results}) == 1
    assert len(list(files.root.path.rglob("*.cbz"))) == 1
    first = results[0].book
    state = PersonalState(rating=4.5, resume_page=2, completed=True)
    files.library.save_personal(first.id, state, expected_revision=1)
    copied = tmp_path / "other.cbz"
    copied.write_bytes(source.read_bytes())
    duplicate = files.import_file(copied)
    assert not duplicate.created and duplicate.book.personal == state
    make_zip(source, ["new.png"])
    different = files.import_file(source)
    assert different.created and different.book.id != first.id and different.book.personal == PersonalState()


def test_eze_auto_pairing_and_invalid_metadata_do_not_hide_valid_archive(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "with-eze.zip")
    sidecar = source.with_suffix(".eze.json")
    fixture = Path(__file__).parent / "fixtures/eze.json"
    sidecar.write_bytes(fixture.read_bytes())
    result = files.import_file(source)
    assert result.book.metadata.title == "Test translation"
    assert sidecar.read_bytes() == fixture.read_bytes()
    bad = make_zip(tmp_path / "bad-meta.zip", ["other.png"])
    bad.with_suffix(".eze.json").write_text('{"archiver_key":"DO_NOT_KEEP"}')
    invalid = files.import_file(bad)
    assert invalid.warnings == ("eze_unreadable_or_invalid",)
    assert files.page_bytes(invalid.book.id, 0).startswith(b"\x89PNG")
    with files.library.connection() as db:
        assert "DO_NOT_KEEP" not in "\n".join(db.iterdump())


def test_missing_root_empty_wrong_mount_and_restore_keep_records(files: FileLibrary, tmp_path: Path):
    book = files.import_file(make_zip(tmp_path / "sample.zip")).book
    state = PersonalState(rating=5.0, resume_page=1)
    files.library.save_personal(book.id, state, expected_revision=1)
    offline = tmp_path / "offline"
    files.root.path.rename(offline)
    for empty_mount in (False, True):
        if empty_mount:
            files.root.path.mkdir()
        with pytest.raises(RootUnavailable):
            files.refresh_availability()
        with pytest.raises(RootUnavailable):
            files.page_bytes(book.id, 0)
        assert files.library.get_book(book.id).personal == state
        assert files.library.get_book(book.id).availability == "available"
    files.root.path.rmdir()
    offline.rename(files.root.path)
    assert files.refresh_availability() == {book.id: "available"}
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")


def test_wrong_identity_does_not_write_to_another_root(files: FileLibrary, tmp_path: Path):
    other = tmp_path / "other-root"
    other.mkdir()
    LibraryRoot.bind(Library.initialize(tmp_path / "other.sqlite"), other)
    before = {path.name: path.read_bytes() for path in other.iterdir()}
    wrong = FileLibrary(files.library, other)
    with pytest.raises(RootUnavailable):
        wrong.import_file(make_zip(tmp_path / "book.zip"))
    assert {path.name: path.read_bytes() for path in other.iterdir()} == before


def test_individual_missing_and_changed_file_never_prune(files: FileLibrary, tmp_path: Path):
    book = files.import_file(make_zip(tmp_path / "sample.zip")).book
    path = files.root.path / book.content.relative_path
    original = path.read_bytes()
    path.unlink()
    assert files.refresh_availability() == {book.id: "missing"}
    path.write_bytes(b"different content")
    assert files.refresh_availability() == {book.id: "changed"}
    with pytest.raises(ContentChanged):
        files.page_bytes(book.id, 0)
    path.write_bytes(original)
    assert files.refresh_availability() == {book.id: "available"}
    assert files.library.get_book(book.id).content.sha256 == hashlib.sha256(original).hexdigest()


def test_in_place_scan_and_move_preserve_id_and_personal_state(files: FileLibrary):
    source = make_zip(files.root.path / "Existing Unicode 原件.zip")
    before = source.read_bytes()
    first = files.import_file(source, index_existing=True)
    files.library.save_personal(first.book.id, PersonalState(rating=2.5, completed=True), expected_revision=1)
    moved = source.with_name("moved.zip")
    source.rename(moved)
    second = files.import_file(moved, index_existing=True)
    assert not second.created and second.book.id == first.book.id
    assert second.book.content.relative_path == "moved.zip"
    assert second.book.personal.rating == 2.5 and second.book.personal.completed
    assert moved.read_bytes() == before and not list(files.root.path.rglob("*.cbz"))
    assert files.page_bytes(first.book.id, 1).startswith(b"\x89PNG")
    duplicate = files.root.path / "duplicate.zip"
    duplicate.write_bytes(before)
    result = files.import_file(duplicate, index_existing=True)
    assert result.warnings == ("duplicate_library_file_retained",)
    assert duplicate.exists() and result.book.content.relative_path == "moved.zip"


@pytest.mark.parametrize("phase", ["copying", "published"])
def test_process_exit_recovers_consistently(files: FileLibrary, tmp_path: Path, phase: str):
    source = make_zip(tmp_path / "sample.zip")
    source.with_suffix(".eze.json").write_bytes((Path(__file__).parent / "fixtures/eze.json").read_bytes())
    # Stop right after the copy into staging, or instead of committing the published file.
    crash = (
        'crash_after(files, "_copy", 77)'
        if phase == "copying"
        else 'crash_after(files, "_commit_import", 77, run_original=False)'
    )
    script = (
        CRASH_AFTER
        + f"""
import sys
from pathlib import Path
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary
files = FileLibrary(Library(Path(sys.argv[1])), Path(sys.argv[2]))
{crash}
files.import_file(Path(sys.argv[3]))
"""
    )
    result = python("-c", script, files.library.path, files.root.path, source)
    assert result.returncode == 77, result.stderr
    assert files.library.search("") == []
    assert list(files.recover().values()) == ["interrupted; retry source import"]
    assert files.library.search("") == [] and source.exists()
    assert list((files.root.path / STAGING).iterdir()) == []
    assert not list(files.root.path.glob("*/*.cbz"))


def test_cleanup_failure_does_not_turn_a_committed_import_back_into_ready(
    files: FileLibrary, tmp_path: Path, monkeypatch
):
    source = make_zip(tmp_path / "source.zip")

    def fail_cleanup(_):
        raise OSError("injected cleanup failure")

    monkeypatch.setattr(files, "_cleanup_stage", fail_cleanup)
    result = files.import_file(source)
    assert result.created and "staging_cleanup_pending" in result.warnings
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "done"
    reopened = FileLibrary(Library(files.library.path), files.root.path)
    assert reopened.recover() == {}
    assert len(reopened.library.search("")) == 1 and list((files.root.path / STAGING).iterdir()) == []


def test_cancel_and_space_limit_leave_no_half_import(files: FileLibrary, tmp_path: Path, monkeypatch):
    source = make_zip(tmp_path / "sample.zip")
    calls = 0

    def cancel() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 2

    with pytest.raises(ImportCancelled):
        files.import_file(source, cancelled=cancel)
    limited = FileLibrary(files.library, files.root.path, Limits(max_archive_bytes=10))
    with pytest.raises(ValueError, match="byte limit"):
        limited.import_file(source)
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage", lambda _: type(usage)(usage.total, usage.total, 0))
    with pytest.raises(OSError, match="space"):
        files.import_file(source)
    assert source.exists() and files.library.search("") == []
    assert not list(files.root.path.rglob("*.cbz"))
    assert list((files.root.path / STAGING).iterdir()) == []


@pytest.mark.parametrize(
    "kind", ["traversal", "symlink", "not_image", "empty", "corrupt_pdf", "too_many", "comment_signature"]
)
def test_invalid_archives_are_not_registered(files: FileLibrary, tmp_path: Path, kind: str):
    source = tmp_path / ("bad.pdf" if kind == "corrupt_pdf" else "bad.zip")
    if kind == "corrupt_pdf":
        source.write_bytes(b"not a pdf")
    else:
        with zipfile.ZipFile(source, "w") as archive:
            if kind == "traversal":
                archive.writestr("../escape.png", png())
            elif kind == "symlink":
                entry = zipfile.ZipInfo("link.png")
                entry.create_system = 3
                entry.external_attr = 0o120777 << 16
                archive.writestr(entry, b"/outside")
            elif kind == "not_image":
                archive.writestr("bad.png", b"not an image")
            elif kind == "too_many":
                for index in range(3):
                    archive.writestr(f"{index}.png", png())
            elif kind == "comment_signature":
                archive.writestr("1.png", png())
                archive.comment = b"Comment with PK\x05\x06 inside"
    if kind == "too_many":
        files = FileLibrary(files.library, files.root.path, Limits(max_entries=2))
    with pytest.raises(InvalidArchive):
        files.import_file(source)
    assert source.exists() and files.library.search("") == []
    assert not list(files.root.path.rglob("*.cbz"))


def test_symlink_directory_and_target_collision_do_not_overwrite(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "source.zip")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    outside = tmp_path / "outside"
    outside.mkdir()
    bucket = files.root.path / digest[:2]
    bucket.symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match="symbolic"):
        files.import_file(source)
    assert list(outside.iterdir()) == []
    bucket.unlink()
    bucket.mkdir()
    target = bucket / f"{digest}.cbz"
    target.write_bytes(b"DO NOT OVERWRITE")
    assert files.import_file(source).created
    assert target.with_name(target.name + ".changed").read_bytes() == b"DO NOT OVERWRITE"
    assert target.read_bytes() == source.read_bytes()


def test_root_disappears_during_scan_without_persisting_false_missing(
    files: FileLibrary, tmp_path: Path, monkeypatch
):
    book = files.import_file(make_zip(tmp_path / "source.zip")).book

    def offline_root(*, writable=False):
        raise RootUnavailable("fixture root disconnected")

    def disappearing(value):
        monkeypatch.setattr(files.root, "check", offline_root)
        return "missing", None

    monkeypatch.setattr(files, "availability", disappearing)
    with pytest.raises(RootUnavailable):
        files.refresh_availability()
    assert files.library.get_book(book.id).availability == "available"


def test_root_permission_error_keeps_database(files: FileLibrary, tmp_path: Path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "source.zip")).book
    marker = files.root.path / ROOT_MARKER
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path == marker:
            raise PermissionError("fixture root marker unreadable")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(RootUnavailable):
        files.refresh_availability()
    assert files.library.get_book(book.id).availability == "available"


def test_image_budget_and_svg_disguised_as_png_are_rejected(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "raster.zip")
    limited = FileLibrary(files.library, files.root.path, Limits(max_pixels=90))
    with pytest.raises(InvalidArchive, match="dimensions"):
        limited.import_file(source)
    disguised = tmp_path / "svg.zip"
    with zipfile.ZipFile(disguised, "w") as archive:
        archive.writestr("page.png", '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>')
    with pytest.raises(InvalidArchive, match="Expected GIF"):
        files.import_file(disguised)
    assert files.library.search("") == []


def test_small_zip64_member_is_supported(files: FileLibrary, tmp_path: Path):
    source = tmp_path / "zip64.zip"
    with zipfile.ZipFile(source, "w") as archive:
        with archive.open("1.png", "w", force_zip64=True) as member:
            member.write(png())
        archive.comment = b"Small ZIP64 fixture"
    result = files.import_file(source)
    assert files.page_bytes(result.book.id, 0) == png()


def test_corrupt_page_crc_is_rejected(files: FileLibrary, tmp_path: Path):
    source = make_zip(tmp_path / "crc.zip")
    with zipfile.ZipFile(source) as archive:
        first = archive.infolist()[0]
        data_offset = first.header_offset + 30 + len(first.filename.encode()) + len(first.extra)
    data = bytearray(source.read_bytes())
    data[data_offset + 20] ^= 1
    source.write_bytes(data)
    with pytest.raises(InvalidArchive):
        files.import_file(source)
    assert files.library.search("") == []


def test_cli_initializes_imports_renders_and_diagnoses_offline_root(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    source = make_zip(tmp_path / "sample.zip")
    db = tmp_path / "library.sqlite"

    def run(*arguments: str):
        return python("-m", "mangacouch", "--db", db, "--root", root, *arguments)

    assert run("init").returncode == 0
    imported = run("import", str(source))
    assert imported.returncode == 0, imported.stdout
    book_id = json.loads(imported.stdout)["book"]["id"]
    page = tmp_path / "page.png"
    assert run("page", book_id, "0", str(page)).returncode == 0
    assert page.read_bytes() == png(22)
    assert json.loads(run("doctor").stdout)["ready"]
    root.rename(tmp_path / "offline")
    diagnosed = run("doctor")
    assert diagnosed.returncode == 2
    assert json.loads(diagnosed.stdout) == {"ready": False, "error": "root_unavailable"}
    assert json.loads(run("list").stdout)["items"][0]["id"] == book_id


def test_corrupt_deflate_is_invalid_archive(tmp_path):
    path = tmp_path / "bad.cbz"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("1.png", png())
    data = bytearray(path.read_bytes())
    # Deflate BTYPE=3 is reserved and rejected before CRC handling.
    data[30 + len("1.png")] = 7
    path.write_bytes(data)
    with pytest.raises(InvalidArchive):
        inspect_archive(path, "cbz", Limits())


def test_embedded_pdf_image_is_checked_before_render():
    class Image:
        def get_px_size(self):
            return 20000, 20000

    class Page:
        def get_size(self):
            return 612, 792

        def get_objects(self, **kwargs):
            return [Image()]

    with pytest.raises(InvalidArchive, match="embedded image"):
        _pdf_size(Page(), Limits())


def test_archive_integrity_checks_non_image_entries(tmp_path):
    archive = make_zip(tmp_path / "bad.cbz")
    with zipfile.ZipFile(archive, "a") as zipped:
        zipped.writestr("readme.txt", b"UNIQUE AUXILIARY PAYLOAD", compress_type=zipfile.ZIP_STORED)
    data = archive.read_bytes().replace(b"UNIQUE AUXILIARY PAYLOAD", b"BROKEN AUXILIARY PAYLOAD", 1)
    archive.write_bytes(data)
    with pytest.raises(InvalidArchive):
        inspect_archive(archive, "cbz", Limits())


@pytest.mark.parametrize(
    "failure,status",
    [("database trigger", None), ("upload I/O error", 503), ("upload disk full", 507)],
)
def test_failed_commit_removes_publication_and_never_recovers_it(
    client, files: FileLibrary, tmp_path: Path, monkeypatch, failure, status
):
    name = "長原名😀" * 100 + ".cbz"
    if status is None:
        with files.library.connection() as db:
            db.execute(
                "CREATE TRIGGER fail_manifest BEFORE INSERT ON archive_manifest "
                "BEGIN SELECT RAISE(ABORT, 'injected'); END"
            )
        source = make_zip(tmp_path / "upload.cbz")
        with pytest.raises(sqlite3.IntegrityError):
            files.import_file(source, original_filename=name)
        source.unlink()
        with files.library.connection() as db:
            db.execute("DROP TRIGGER fail_manifest")
    else:

        def fail(*_args, **_kwargs):
            error = sqlite3.OperationalError("injected commit storage failure")
            error.sqlite_errorcode = sqlite3.SQLITE_FULL if status == 507 else sqlite3.SQLITE_IOERR
            raise error

        login(client)
        with monkeypatch.context() as patch:
            patch.setattr(files, "_commit_import", fail)
            result = client.post(
                "/api/import", params={"filename": name}, files=parts(tmp_path), headers=CSRF
            )
        assert result.status_code == status
        assert result.json()["error"] == ("insufficient_storage" if status == 507 else "storage_unavailable")
    assert files.library.search("") == [] and not list(files.root.path.rglob("*.cbz"))
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"
    recovered = FileLibrary(Library(files.library.path), files.root.path)
    assert recovered.recover() == {}
    assert recovered.library.search("") == []
    assert list((files.root.path / STAGING).iterdir()) == []


def test_terminal_imports_expire_without_dropping_recovery_jobs(files):
    import uuid

    identifiers = {state: str(uuid.uuid4()) for state in ("done", "failed", "copying", "ready", "new")}
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO import_job(id,state,kind,created_at) VALUES(?,?,'copy',?)",
            [(identifiers[state], state, 0) for state in ("done", "failed", "copying", "ready")],
        )
        db.execute("INSERT INTO import_job(id,state,kind) VALUES(?,'done','copy')", (identifiers["new"],))
    assert files.purge_imports() == 3
    with files.library.connection() as db:
        assert {row[0] for row in db.execute("SELECT id FROM import_job")} == {
            identifiers["ready"],
            identifiers["new"],
        }


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


def test_transient_failure_after_ready_is_cleaned_without_publication(
    files: FileLibrary, tmp_path, monkeypatch
):
    source = make_zip(tmp_path / "sample.zip")
    original = files._commit_import

    def locked(*_args, **_kwargs):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        raise error

    monkeypatch.setattr(files, "_commit_import", locked)
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(source)
    assert job_rows(files)[0]["state"] == "failed"
    monkeypatch.setattr(files, "_commit_import", original)
    assert files.recover() == {}
    [job] = job_rows(files)
    assert job["state"] == "failed" and job["pages_json"] is None and job["report_json"] is None


def test_recover_turns_a_deterministic_ready_failure_into_failed(files: FileLibrary, tmp_path, monkeypatch):
    source = make_zip(tmp_path / "sample.zip")
    error = sqlite3.OperationalError("disk I/O error")
    error.sqlite_errorcode = sqlite3.SQLITE_IOERR
    monkeypatch.setattr(files, "_commit_import", lambda *a, **k: (_ for _ in ()).throw(error))
    with pytest.raises(sqlite3.OperationalError):
        files.import_file(source)
    monkeypatch.undo()
    stage = files.root.path / STAGING / f"{job_rows(files)[0]['id']}.part"
    stage.write_bytes(b"tampered")
    assert files.recover() == {}
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


def test_archive_validation_runs_without_the_library_lock_and_recovery_skips_it(files, tmp_path, monkeypatch):

    seen = []
    inspect = ingest.inspect_archive

    def observed(*args, **kwargs):
        with files.root.mutation(blocking=False):  # other edits may take the lock meanwhile
            pass
        seen.append(files.recover())  # and recovery leaves the running import alone
        return inspect(*args, **kwargs)

    monkeypatch.setattr(ingest, "inspect_archive", observed)
    result = files.import_file(make_zip(tmp_path / "busy.cbz"))
    assert seen == [{}] and result.created
    assert not list((files.root.path / ".mangacouch-staging").iterdir())
