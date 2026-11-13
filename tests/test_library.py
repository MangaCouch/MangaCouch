from __future__ import annotations

import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from uuid import UUID

import pytest
from helpers import (
    content,
    make_zip,
    pending_sidecars,
    register_book,
    save_metadata,
)
from pydantic import ValidationError

from mangacouch import database
from mangacouch.database import Library, RevisionConflict, busy
from mangacouch.models import Content, Metadata, PersonalState


def test_explicit_initialization_does_not_create_or_overwrite_existing_database(tmp_path: Path):
    path = tmp_path / "library.sqlite"
    with pytest.raises(sqlite3.OperationalError):
        Library(path)
    assert not path.exists()
    Library.initialize(path)
    with pytest.raises(FileExistsError):
        Library.initialize(path)
    foreign = tmp_path / "foreign.sqlite"
    with closing(sqlite3.connect(foreign)) as db:
        db.execute("CREATE TABLE archive(id TEXT)")
        db.commit()
    before = foreign.read_bytes()
    with pytest.raises(ValueError, match="supported MangaCouch database"):
        Library(foreign)
    assert foreign.read_bytes() == before


def test_hash_deduplication_is_independent_of_filename_and_personal_state(library: Library):
    first, created = register_book(library, content())
    assert created and UUID(first.id).version == 4 and first.id != first.content.sha256
    first = library.save_personal(
        first.id, PersonalState(rating=4.5, resume_page=20, completed=True), expected_revision=1
    )
    duplicate, created = register_book(library, content(filename="different.cbz"))
    assert not created and duplicate.id == first.id and duplicate.personal == first.personal
    replacement, created = register_book(library, content("new version"))
    assert created and replacement.id != first.id and replacement.personal == PersonalState()
    assert library.get_book(first.id).personal.rating == 4.5


def test_concurrent_registration_has_one_record_and_separate_contexts_do_not_mix(tmp_path: Path):
    first = Library.initialize(tmp_path / "first.sqlite")
    other = Library.initialize(tmp_path / "other.sqlite")
    with ThreadPoolExecutor(max_workers=5) as pool:
        results = list(
            pool.map(lambda i: register_book(Library(first.path), content(filename=f"{i}.zip")), range(10))
        )
    assert sum(created for _, created in results) == 1
    assert len({book.id for book, _ in results}) == 1
    assert other.search("") == []
    assert len(first.search("")) == 1


def test_edit_restart_and_search_share_committed_state(library: Library):
    book, _ = register_book(library, content())
    book = save_metadata(
        library, book.id, Metadata(title="ＡＢＣ 中文 心海", language="Chinese"), expected_revision=1
    )
    book = save_metadata(
        library, book.id, Metadata(title="新版 心海", language="Chinese"), expected_revision=2
    )
    reopened = Library(library.path)
    assert reopened.get_book(book.id).metadata.title == "新版 心海"
    assert pending_sidecars(reopened) == [(book.id, 3)]
    assert reopened.search("新版 心海") == [book.id]
    assert reopened.search("abc") == []
    with pytest.raises(RevisionConflict):
        save_metadata(reopened, book.id, Metadata(title="stale"), expected_revision=2)
    same = save_metadata(reopened, book.id, book.metadata, expected_revision=3)
    assert same.metadata_revision == 3 and pending_sidecars(reopened) == [(book.id, 3)]


def test_sidecar_queue_failure_rolls_back_metadata_and_search(library: Library):
    book, _ = register_book(library, content(), Metadata(title="before"))
    with library.connection() as db:
        db.execute(
            "CREATE TRIGGER inject_failure BEFORE UPDATE ON sidecar_sync "
            "BEGIN SELECT RAISE(ABORT, 'injected failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        save_metadata(library, book.id, Metadata(title="after"), expected_revision=1)
    reopened = Library(library.path)
    assert reopened.get_book(book.id).metadata.title == "before"
    assert reopened.search("after") == [] and reopened.search("before") == [book.id]
    assert pending_sidecars(reopened) == [(book.id, 1)]


def test_personal_state_is_separate_and_revision_checked(library: Library):
    book, _ = register_book(library, content())
    before = library.native_sidecar(book.id).canonical_bytes()
    state = PersonalState(rating=1.5, resume_page=29, completed=True)
    library.save_personal(book.id, state, expected_revision=1)
    state.resume_page = 3
    library.save_personal(book.id, state, expected_revision=2)
    with pytest.raises(RevisionConflict):
        library.save_personal(book.id, PersonalState(), expected_revision=1)
    assert library.native_sidecar(book.id).canonical_bytes() == before
    assert Library(library.path).get_book(book.id).personal.completed
    with pytest.raises(ValueError, match="outside this book"):
        library.save_personal(book.id, PersonalState(resume_page=30), expected_revision=3)


@pytest.mark.parametrize("rating", [-0.5, 1.25, 5.5, True, "4", float("nan")])
def test_invalid_personal_ratings_are_rejected(rating):
    with pytest.raises(ValidationError):
        PersonalState(rating=rating)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/book.zip",
        "../book.zip",
        "C:/book.zip",
        "C:book.zip",
        "folder/../book.zip",
        "folder\\book.zip",
        "./book.zip",
        "book\x00.zip",
    ],
)
def test_relative_paths_cannot_escape_the_library(path: str):
    data = content().model_dump()
    data["relative_path"] = path
    with pytest.raises(ValidationError):
        Content.model_validate(data)


def test_managed_name_does_not_depend_on_the_original_name():
    item = content()
    item.original_filename = "CON/原名 😀:<>?\x00.zip" + "长" * 300
    assert item.managed_path == f"{item.sha256[:2]}/{item.sha256}.cbz"
    assert "😀" in item.original_filename
    assert Content.model_validate_json(item.model_dump_json()).original_filename == item.original_filename


def test_plain_sqlite_client_can_write_without_application_functions(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    with closing(sqlite3.connect(files.library.path)) as db:
        db.execute("UPDATE book SET metadata_json=metadata_json WHERE id=?", (book.id,))
        db.commit()
    assert files.library.get_book(book.id).metadata == book.metadata


def test_library_storage_details(tmp_path, files):
    with files.library.connection() as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 1
    with files.library.connection(durable=True) as db:
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    target = tmp_path / "broken.sqlite"

    def fail(*_args):
        raise sqlite3.OperationalError("fixture")

    original = sqlite3.connect

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.create_function("fixture_fail", 0, fail)
        return connection

    import mangacouch.database as database_module

    schema = database_module.SCHEMA
    database_module.SCHEMA = schema + "\nSELECT fixture_fail();"
    try:
        database_module.sqlite3.connect = connect  # type: ignore[assignment]
        with pytest.raises(sqlite3.OperationalError):
            Library.initialize(target)
    finally:
        database_module.sqlite3.connect = original  # type: ignore[assignment]
        database_module.SCHEMA = schema
    assert not any(tmp_path.glob("broken.sqlite*"))


def test_a_transaction_reports_the_error_sqlite_already_rolled_back(files):
    with pytest.raises(ValueError, match="original"), files.library.transaction() as db:
        db.execute("ROLLBACK")  # as SQLite does by itself after SQLITE_FULL
        raise ValueError("original")


def hold_write_lock(library: Library, seconds: float) -> threading.Event:
    """Another connection holds the write lock for `seconds`; the event is set once it holds it."""
    held = threading.Event()

    def hold():
        with library.transaction():
            held.set()
            time.sleep(seconds)

    threading.Thread(target=hold, daemon=True).start()
    assert held.wait(5)
    return held


def test_background_thread_outwaits_a_writer_that_a_request_gives_up_on(library: Library):
    library.busy_timeout = 0.05
    hold_write_lock(library, 1)
    with pytest.raises(sqlite3.OperationalError) as refused, library.transaction():
        pass
    assert busy(refused.value)
    done = []

    def work():
        database.background_thread(10)
        with library.transaction() as db:
            db.execute("UPDATE search_revision SET singleton=singleton")
        done.append(True)

    thread = threading.Thread(target=work)
    thread.start()
    thread.join(3)
    assert done == [True]


def test_slow_write_transaction_is_logged_with_its_caller(library: Library, monkeypatch, caplog):
    monkeypatch.setattr(database, "SLOW_TRANSACTION", 0.05)

    def lingering_writer():
        with library.transaction():
            time.sleep(0.1)

    with caplog.at_level(logging.WARNING, logger="mangacouch.database"):
        with library.transaction():
            pass
        assert not caplog.records
        lingering_writer()
    (record,) = caplog.records
    assert "held the database" in record.getMessage() and "lingering_writer" in record.getMessage()
