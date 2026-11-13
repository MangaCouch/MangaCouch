"""Regression cases for the first audit repair batch; all source traffic is synthetic."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from typing import cast

import pytest
from helpers import action, download_step, import_book, make_zip
from test_downloads import due, run, transferring

from mangacouch import database
from mangacouch.duplicates import Duplicates
from mangacouch.ingest import FileLibrary
from mangacouch.maintenance import Maintenance
from mangacouch.page_features import PageFeatures


def locked():
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    return error


def test_background_timeout_is_thread_local(files):
    files.library.busy_timeout = 0.01
    observed = []

    def work():
        database.background_thread(1)
        with files.library.connection() as db:
            observed.append(db.execute("PRAGMA busy_timeout").fetchone()[0])

    thread = threading.Thread(target=work)
    thread.start()
    thread.join()
    assert observed == [1000]
    with files.library.connection() as db:
        assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 10


def test_maintenance_busy_does_not_log_traceback_or_skip_next_step(caplog):
    called = []

    def blocked():
        raise locked()

    worker = Maintenance(steps=[blocked, lambda: bool(called.append(True))], hourly=[])
    with caplog.at_level(logging.WARNING):
        worker.run_steps()
    assert called == [True]
    assert len(caplog.records) == 1 and caplog.records[0].exc_info is None


def test_maintenance_shutdown_cancels_operation_lock(files):
    entered = threading.Event()

    def work():
        entered.set()
        with files.root.mutation():
            pass
        return False

    worker = Maintenance(steps=[work], hourly=[])
    with files.root.mutation():
        worker.start()
        assert entered.wait(2)
        started = time.monotonic()
        # Always release the lock even on the old implementation.
        closing = threading.Thread(target=worker.close)
        closing.start()
        closing.join(1)
        stopped = not closing.is_alive()
    closing.join(3)
    assert stopped and time.monotonic() - started < 2


def test_server_keeps_wal_between_isolated_connections(api, files):
    files.library.close_keep_alive()  # only the server's own connection may keep the WAL
    with api:
        with files.library.transaction() as db:
            db.execute("UPDATE search_revision SET singleton=singleton")
        assert files.library.path.with_name(files.library.path.name + "-wal").exists()


@pytest.mark.parametrize("cancel", [True, False])
def test_failed_download_import_is_owned_and_reused(queue, monkeypatch, cancel):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    original = FileLibrary._commit_import
    failures = [True]

    def commit(self, *args, **kwargs):
        if failures:
            failures.pop()
            raise locked()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(FileLibrary, "_commit_import", commit)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued"
    if cancel:
        action(service, latest, "cancel")
        service.files.recover()
        with service.library.connection() as db:
            assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0
            assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"
        assert not list(service.files.root.path.glob(".mangacouch-staging/*.part"))
    else:
        due(service)
        run(service)
        with service.library.connection() as db:
            assert db.execute("SELECT count(*) FROM import_job").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM source_note").fetchone()[0] == 1
        assert service.get(job["id"])["state"] == "done"


def test_index_does_not_query_status(files, tmp_path, monkeypatch):
    files.import_file(make_zip(tmp_path / "pages.cbz"))
    features = PageFeatures(files)
    monkeypatch.setattr(features, "status", lambda: pytest.fail("full-library status on index hot path"))
    assert features.index(limit=1) == {"processed": 1}


def test_duplicate_comparison_finishes_whole_books(files, tmp_path):
    for n in range(3):
        import_book(files, tmp_path, f"{n}.cbz", seeds=[1, 2, 3, n + 10])
    features = PageFeatures(files)
    features.index(limit=20)
    duplicates = Duplicates(features)
    for _ in range(3):
        assert duplicates.step()
    assert not duplicates.step()
    groups = duplicates.groups()
    assert groups["comparing"] == {"done": 3, "total": 3}
    assert len(groups["items"]) == 3
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 9


def test_source_check_batch_settles_after_row_or_prefetch_failure(queue, files, tmp_path, monkeypatch):
    from mangacouch.source_checks import enqueue_check

    checks = queue[0].checks
    ids = [import_book(files, tmp_path, f"{n}.cbz", seeds=[n]).id for n in range(3)]
    with files.library.transaction() as db:
        for identifier in ids:
            enqueue_check(db, identifier)
        db.execute("UPDATE source_check SET state='queued'")
    called = []

    def fail(*args):
        called.append(args[0])
        raise locked()

    monkeypatch.setattr(checks, "_run", fail)
    monkeypatch.setattr(checks, "_prefetch", lambda *args: [])
    assert checks._step(lambda: False)
    assert len(called) == 3
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_check WHERE state='running'").fetchone()[0] == 0
    monkeypatch.setattr(checks, "_prefetch", fail)
    with files.library.transaction() as db:
        db.execute("UPDATE source_check SET retry_at=0")
    assert checks._step(lambda: False)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_check WHERE state='running'").fetchone()[0] == 0


def test_bounded_mutation_returns_503_and_clears_thread_cancel(files, monkeypatch):
    from types import SimpleNamespace

    import anyio
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mangacouch import services
    from mangacouch.files import thread_cancelled
    from mangacouch.http import install_error_handlers

    monkeypatch.setattr(services, "MUTATION_TIMEOUT", 0.05)
    app = FastAPI()
    install_error_handlers(app)
    state = cast(services.Services, SimpleNamespace(mutation_limiter=anyio.CapacityLimiter(1)))

    def work():
        with files.root.mutation():
            return {"ok": True}

    @app.get("/blocked")
    async def blocked():
        return await services.Services.bounded(state, work)

    @app.get("/clear")
    async def clear():
        return await services.Services.bounded(state, lambda: thread_cancelled() is not None)

    with TestClient(app) as client, files.root.mutation():
        started = time.monotonic()
        response = client.get("/blocked")
        assert response.status_code == 503
        assert response.json()["error"] == "library_busy"
        assert response.headers["Retry-After"] == "5"
        assert time.monotonic() - started < 1
        assert client.get("/clear").json() is True
    assert thread_cancelled() is None


def test_hash_and_copy_obey_thread_cancellation(files, tmp_path, monkeypatch):
    import errno

    from mangacouch import files as storage
    from mangacouch.limits import ImportCancelled

    source = tmp_path / "source"
    source.write_bytes(b"a" * (storage.CHUNK_SIZE * 2))
    target = tmp_path / "target"
    checks = []

    def cancelled():
        checks.append(True)
        return len(checks) >= 3

    def cross_device(*args, **kwargs):
        raise OSError(errno.EXDEV, "different device")

    monkeypatch.setattr(storage.os, "link", cross_device)
    storage.set_thread_cancel(cancelled)
    try:
        with pytest.raises(ImportCancelled):
            storage.publish_file(source, target)
        assert not target.exists()
        assert not list(tmp_path.glob(".mangacouch-publish-*"))
        with pytest.raises(ImportCancelled):
            storage.hash_file(source)
    finally:
        storage.set_thread_cancel(None)


def test_recovery_observes_persisted_download_cancellation(queue, monkeypatch):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    original = FileLibrary._commit_import
    monkeypatch.setattr(FileLibrary, "_commit_import", lambda *a, **kw: (_ for _ in ()).throw(locked()))
    download_step(service)
    monkeypatch.setattr(FileLibrary, "_commit_import", original)
    # Simulate a crash after the cancellation transaction and before its cleanup.
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET state='cancelled' WHERE id=?", (job["id"],))
    service.files.recover()
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0
        assert db.execute("SELECT state FROM import_job").fetchone()[0] == "failed"


def test_committed_import_wins_cancel_and_saves_source_notes(queue, monkeypatch):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    complete = service._complete_import
    monkeypatch.setattr(service, "_complete_import", lambda *args: (_ for _ in ()).throw(locked()))
    download_step(service)
    monkeypatch.setattr(service, "_complete_import", complete)
    latest = service.get(job["id"])
    assert latest["state"] == "queued"
    assert action(service, latest, "cancel")["state"] == "done"
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM source_note").fetchone()[0] == 1


def test_source_check_settlement_retries_a_busy_transaction(queue, monkeypatch):
    from contextlib import contextmanager

    from mangacouch import source_checks

    checks = queue[0].checks
    transaction = checks.library.transaction
    attempts = []

    @contextmanager
    def flaky():
        attempts.append(True)
        if len(attempts) == 1:
            raise locked()
        with transaction() as db:
            yield db

    monkeypatch.setattr(checks.library, "transaction", flaky)
    monkeypatch.setattr(source_checks, "SETTLE_WAIT", 0)
    saved = []
    checks._settle_with_retry(lambda db: saved.append(True), lambda: False)
    assert attempts == [True, True] and saved == [True]


def test_common_page_candidate_limit_skips_dense_bucket(files, tmp_path):
    from helpers import content, register_book

    from mangacouch.page_features import bands

    previous, _ = register_book(files.library, content("dense"))
    incoming, _ = register_book(files.library, content("incoming"))
    with files.library.transaction() as db:
        db.execute(
            "UPDATE book SET content_json=json_set(content_json,'$.page_count',501) WHERE id=?",
            (previous.id,),
        )
        db.execute(
            "UPDATE book SET content_json=json_set(content_json,'$.page_count',1) WHERE id=?", (incoming.id,)
        )
        for book, count in [(previous, 501), (incoming, 1)]:
            for page in range(count):
                feature = db.execute(
                    "INSERT INTO page_feature(book_id,page,digest,dhash,gray,aspect) "
                    "VALUES(?,?,'same','0000000000000001',?,1)",
                    (book.id, page, bytes(range(256))),
                ).lastrowid
                db.executemany(
                    "INSERT INTO page_band VALUES(?,?,?)",
                    [(segment, value, feature) for segment, value in bands(1)],
                )
        db.execute("INSERT INTO duplicate_progress VALUES(?)", (previous.id,))
    assert Duplicates(PageFeatures(files)).step()
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM duplicate_progress").fetchone()[0] == 2


def test_page_that_becomes_too_common_drops_its_earlier_edges(files):
    from helpers import content, register_book

    from mangacouch.page_features import bands

    dense, _ = register_book(files.library, content("dense"))
    early, _ = register_book(files.library, content("early"))
    incoming, _ = register_book(files.library, content("incoming"))
    unrelated = [register_book(files.library, content(f"unrelated{n}"))[0].id for n in range(2)]
    with files.library.transaction() as db:
        for book, count in [(dense, 500), (early, 1), (incoming, 1)]:
            db.execute(
                "UPDATE book SET content_json=json_set(content_json,'$.page_count',?) WHERE id=?",
                (count, book.id),
            )
            for page in range(count):
                feature = db.execute(
                    "INSERT INTO page_feature(book_id,page,digest,dhash,gray,aspect) "
                    "VALUES(?,?,'same','0000000000000001',?,1)",
                    (book.id, page, bytes(range(256))),
                ).lastrowid
                db.executemany(
                    "INSERT INTO page_band VALUES(?,?,?)",
                    [(segment, value, feature) for segment, value in bands(1)],
                )
        db.executemany("INSERT INTO duplicate_progress VALUES(?)", [(dense.id,), (early.id,)])
        # Written while the page was still rare enough to compare.
        common = (*sorted((early.id, dense.id)), 0, 0)
        db.execute("INSERT INTO duplicate_edge VALUES(?,?,?,?)", common)
        db.execute("INSERT INTO duplicate_edge VALUES(?,?,7,7)", sorted(unrelated))
    assert Duplicates(PageFeatures(files)).step()
    with files.library.connection() as db:
        left = [tuple(row) for row in db.execute("SELECT * FROM duplicate_edge")]
    assert left == [(*sorted(unrelated), 7, 7)]


def test_bounded_queue_deadline_never_starts_an_expired_operation(monkeypatch):
    from types import SimpleNamespace

    import anyio

    from mangacouch import services
    from mangacouch.errors import LibraryBusy

    monkeypatch.setattr(services, "SLOT_TIMEOUT", 0.02)
    called = []

    async def exercise():
        limiter = anyio.CapacityLimiter(1)
        limiter.acquire_on_behalf_of_nowait("occupied")
        try:
            with pytest.raises(LibraryBusy):
                await services.Services.bounded(
                    cast(services.Services, SimpleNamespace(mutation_limiter=limiter)),
                    lambda: called.append(True),
                )
        finally:
            limiter.release_on_behalf_of("occupied")

    anyio.run(exercise)
    assert not called
