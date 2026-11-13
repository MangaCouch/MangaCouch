from __future__ import annotations

import errno
import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers import download_step, edit, finish_scan, import_book, inbox, make_zip
from test_batch_one import locked

from mangacouch import database
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync


def test_scan_receipt_committed_before_cleanup_failure(files, monkeypatch):
    source = make_zip(inbox(files) / "retained.cbz")
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == source:
            with files.library.connection() as db:
                assert db.execute("SELECT count(*) FROM import_receipt").fetchone()[0] == 1
                assert db.execute("SELECT book_id FROM scan_item WHERE kind='archive'").fetchone()[0]
            raise OSError(errno.EROFS, "read-only")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    scanner = Scanner(files)
    result = finish_scan(scanner, scanner.start()["id"])
    assert result["counts"]["indexed"] == 1
    assert "source_cleanup_failed" in result["items"][0]["warnings"]
    result = finish_scan(scanner, scanner.start(keep_source=True)["id"])
    assert result["counts"]["unchanged"] == 1


def test_sidecar_recognizes_previous_write_after_record_failed(files, tmp_path, monkeypatch):
    book = import_book(files, tmp_path)
    sync = SidecarSync(files)
    sync.sync_one(book.id)
    edit(sync, book.id, "first")
    with monkeypatch.context() as patch:
        patch.setattr(sync, "_record", lambda *a: (_ for _ in ()).throw(locked()))
        with pytest.raises(sqlite3.OperationalError):
            sync.sync_one(book.id)
    edit(sync, book.id, "second")
    assert sync.publish(book.id)["state"] == "synced"


def test_budget_failure_preserves_dirty_count(queue, monkeypatch):
    service, _ = queue
    http = service.http
    http.unsaved = 7
    previous = http.saved_at

    @contextmanager
    def blocked(**kwargs):
        raise locked()
        yield

    with monkeypatch.context() as patch:
        patch.setattr(service.library, "transaction", blocked)
        http._save(force=True)
    assert http.unsaved == 7
    assert http.saved_at == previous
    http._save(force=True)
    assert http.unsaved == 0


def test_slow_transaction_logging_cannot_fail_committed_edit(files, monkeypatch):
    monkeypatch.setattr(database, "SLOW_TRANSACTION", -1)
    monkeypatch.setattr(database.LOGGER, "warning", lambda *a: (_ for _ in ()).throw(ValueError("logger")))
    with files.library.transaction() as db:
        db.execute("INSERT INTO maintenance_stamp VALUES('test',1)")
    with files.library.connection() as db:
        assert db.execute("SELECT updated_at FROM maintenance_stamp WHERE name='test'").fetchone()[0] == 1


def test_hourly_import_purge_cleans_abandoned_copy(files):
    stage = files._stage_path("00000000-0000-0000-0000-000000000003")
    stage.parent.mkdir(exist_ok=True)
    stage.write_bytes(b"incomplete")
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO import_job(id,state,kind) VALUES(?,'copying','copy')",
            ("00000000-0000-0000-0000-000000000003",),
        )
    files.purge_imports()
    with files.library.connection() as db:
        assert (
            db.execute(
                "SELECT state FROM import_job WHERE id='00000000-0000-0000-0000-000000000003'"
            ).fetchone()[0]
            == "failed"
        )
    assert not stage.exists()


def test_consuming_scan_commits_book_with_full_sync(files, monkeypatch):
    make_zip(inbox(files) / "durable.cbz")
    transaction = files.library.transaction
    synchronous = []

    @contextmanager
    def observe(**kwargs):
        with transaction(**kwargs) as db:

            def trace(sql):
                if sql.startswith("INSERT INTO book("):
                    synchronous.append(db.execute("PRAGMA synchronous").fetchone()[0])

            db.set_trace_callback(trace)
            yield db

    monkeypatch.setattr(files.library, "transaction", observe)
    scanner = Scanner(files)
    finish_scan(scanner, scanner.start()["id"])
    assert synchronous and set(synchronous) == {2}


def test_paid_link_gets_immediate_final_attempt_on_stop(queue, monkeypatch):
    service, _ = queue
    attempts = []

    def update(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            service.stopping.set()
            raise locked()

    monkeypatch.setattr(service, "_update", update)
    service._store_paid_link("job", "secret", {"phase": "transfer"})
    assert len(attempts) == 2
    assert not service.storing_links


def test_deferred_task_retries_only_settlement(queue, tmp_path, monkeypatch):
    from source_fixtures import EH

    from mangacouch.source_http import gallery_url

    service, _ = queue
    book = import_book(service.files, tmp_path)
    service.online.defer(book.id, gallery_url(EH), "metadata", 0)
    with service.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")
    refreshed = []
    monkeypatch.setattr(service.online, "refresh", lambda *a, **kw: refreshed.append(a))
    with monkeypatch.context() as patch:
        patch.setattr(service.checks, "_settle_task", lambda *a: (_ for _ in ()).throw(locked()))
        assert service.checks._deferred(lambda: False)
    assert service.checks._deferred(lambda: False)
    assert len(refreshed) == 1
    assert not service.checks.pending_settlements
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_task").fetchone()[0] == 0


def test_import_retry_keeps_original_duplicate_flag(queue, monkeypatch):
    from test_downloads import due, transferring

    service, _ = queue
    job = transferring(service)
    download_step(service)
    with monkeypatch.context() as patch:
        patch.setattr(service, "_complete_import", lambda *a: (_ for _ in ()).throw(locked()))
        download_step(service)
    due(service)
    assert download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "done"
    assert latest["details"]["duplicate"] is False


@pytest.mark.parametrize("bound", [False, True])
def test_serve_saves_settings_only_after_successful_bind(files, monkeypatch, bound):
    import asyncio
    import sys

    import uvicorn

    from mangacouch import __main__ as cli
    from mangacouch.auth import Auth
    from mangacouch.configuration import load_http_settings

    Auth(files.library).initialize("test-password-for-binding")
    previous = load_http_settings(files.library).max_upload_bytes

    async def startup(server, sockets=None):
        if not bound:
            raise SystemExit(3)
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", startup)
    monkeypatch.setattr(uvicorn.Server, "run", lambda server: asyncio.run(server.startup()))
    monkeypatch.setattr(cli, "print", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mangacouch",
            "--db",
            str(files.library.path),
            "--root",
            str(files.root.path),
            "serve",
            "--upload-bytes",
            "12345",
        ],
    )
    if bound:
        assert cli.main() == 0
    else:
        with pytest.raises(SystemExit):
            cli.main()
    assert load_http_settings(files.library).max_upload_bytes == (12345 if bound else previous)
