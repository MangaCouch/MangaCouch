from __future__ import annotations

import sqlite3
from contextlib import contextmanager

import pytest
from helpers import download_step, import_book
from test_batch_one import locked
from test_downloads import transferring

from mangacouch import downloads
from mangacouch.files import RootUnavailable
from mangacouch.organization import Organization
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync


def test_scan_busy_keeps_item_pending(files, monkeypatch):
    scanner = Scanner(files)
    job = scanner.start()
    monkeypatch.setattr(scanner, "_discover", lambda *a: (_ for _ in ()).throw(locked()))
    scanner.step(job["id"])
    with files.library.connection() as db:
        assert db.execute("SELECT state FROM scan_item").fetchone()[0] == "pending"


def test_sidecar_busy_does_not_record_item_failure(files, tmp_path, monkeypatch):
    book = import_book(files, tmp_path, "busy.cbz")
    sync = SidecarSync(files)
    monkeypatch.setattr(sync, "_record", lambda *a: (_ for _ in ()).throw(locked()))
    assert sync.sync_pending() == 0
    assert not sync.retry.failures
    with files.library.connection() as db:
        assert (
            db.execute("SELECT last_error FROM sidecar_sync WHERE book_id=?", (book.id,)).fetchone()[0]
            is None
        )


def test_download_metadata_failure_is_deferred(queue, monkeypatch):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    apply = service.online.apply
    monkeypatch.setattr(service.online, "apply", lambda *a, **kw: (_ for _ in ()).throw(locked()))
    download_step(service)
    assert service.get(job["id"])["state"] == "done"
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_task WHERE kind='metadata'").fetchone()[0] == 1
    monkeypatch.setattr(service.online, "apply", apply)
    with service.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")
    assert service.checks.step()
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_note").fetchone()[0] == 1


def test_offline_source_checks_do_not_fetch(queue, monkeypatch):
    service, _ = queue
    monkeypatch.setattr(service.files.root, "check", lambda **kw: (_ for _ in ()).throw(RootUnavailable()))
    monkeypatch.setattr(service.checks, "_deferred", lambda *a: pytest.fail("offline source work"))
    with service.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")
    assert service.checks.step() is False


def test_idle_bulk_work_never_opens_write_transaction(files, monkeypatch):
    monkeypatch.setattr(files.library, "transaction", lambda **kw: pytest.fail("idle write transaction"))
    assert Organization(files.library).advance() is False


def test_import_recovery_busy_does_not_prevent_startup(files, monkeypatch):
    monkeypatch.setattr(files, "recover", lambda: (_ for _ in ()).throw(locked()))
    downloads.startup(files)


def test_source_halt_is_retried_after_busy(queue, monkeypatch):
    service, _ = queue
    http = service.http

    @contextmanager
    def blocked(**kwargs):
        raise locked()
        yield

    with monkeypatch.context() as patch:
        patch.setattr(service.library, "transaction", blocked)
        http._halt("https://e-hentai.org/g/1/token/", "too_fast")
    http._save(force=True)
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_halt").fetchone()[0] == 1


def test_personal_and_observation_callback_roll_back_together(files, tmp_path):
    book = import_book(files, tmp_path, "progress.cbz")
    state = book.personal.model_copy(update={"resume_page": 0})

    def fail(db):
        raise locked()

    with pytest.raises(sqlite3.OperationalError):
        files.library.save_personal(book.id, state, expected_revision=book.personal_revision, also=fail)
    assert files.library.get_book(book.id).personal_revision == book.personal_revision
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM reading_event").fetchone()[0] == 0


def test_source_error_with_busy_settlement_is_requeued(queue, monkeypatch):
    from source_fixtures import EH

    from mangacouch.source_http import SourceError

    service, _ = queue
    job = service.create(EH)
    monkeypatch.setattr(service, "_inspect", lambda *a: (_ for _ in ()).throw(SourceError("source_network")))
    monkeypatch.setattr(service, "_finish", lambda *a, **kw: (_ for _ in ()).throw(locked()))
    assert download_step(service)
    latest = service.get(job["id"])
    assert (latest["state"], latest["error_code"]) == ("queued", "database_busy")
