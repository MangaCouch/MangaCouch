"""Regression coverage for the November 12 download review; sources are fixtures only."""

from __future__ import annotations

import os
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from helpers import action, download_step, drive, import_book, ready
from source_fixtures import EH, archiver_posts
from test_downloads import archive_post, due, locked, refuse, transferring

from mangacouch import downloads
from mangacouch.errors import InvalidInput
from mangacouch.source_config import SourceVault
from mangacouch.source_http import SourceError, gallery_url


def test_completed_cleanup_failure_keeps_book_and_terminal_state(queue, monkeypatch):
    service, _ = queue
    job = ready(service)
    action(service, job, "approve")
    monkeypatch.setattr(service, "_remove_files", lambda _: (_ for _ in ()).throw(PermissionError("locked")))
    drive(service)
    latest = service.get(job["id"])
    assert latest["book_id"] and latest["state"] == "done"
    service._finish(job["id"], SourceError("download_local_error"))
    assert service.get(job["id"])["state"] == "done"


@pytest.mark.parametrize("resume", [False, True])
def test_transfer_never_writes_through_a_leftover_hardlink(queue, tmp_path, resume):
    service, remote = queue
    job = transferring(service)
    path = service._path(job["id"])
    original = tmp_path / "published.cbz"
    original.write_bytes(b"original book")
    os.link(original, path)
    if resume:
        latest = service.get(job["id"], private=True)
        latest["details"]["etag"] = '"old"'
        service._update(job["id"], details=latest["details"], total=original.stat().st_size)
    download_step(service)
    assert original.read_bytes() == b"original book"
    assert path.read_bytes() == remote.payload


@pytest.mark.parametrize(
    "body,code",
    [
        ('<a href="http://10.0.0.5:8080/archive/x">Download</a>', "unsafe_source_url"),
        ("", "source_login_required"),
    ],
)
def test_paid_response_failure_preserves_recovery(queue, body, code):
    service, remote = queue
    job = ready(service, EH.replace("e-hentai.org", "exhentai.org"))
    original = remote.__call__

    def response(request):
        result = original(request)
        return httpx.Response(200, text=body) if archive_post(request) else result

    service.http.transport.handler = response
    balance = remote.balance
    action(service, job, "approve")
    download_step(service)
    latest = service.get(job["id"])
    assert balance - remote.balance == remote.cost
    assert latest["error_code"] == code
    assert latest["details"]["phase"] == "recover"
    assert latest["details"]["submission_uncertain"]
    assert latest["state"] in {"queued", "verification_required"}


def test_unusable_paid_responses_stop_after_five_failures(queue):
    service, remote = queue
    job = ready(service)
    refuse(remote, service, archive_post, httpx.Response(200, text="No archive server available"))
    action(service, job, "approve")
    for _ in range(20):
        due(service)
        if not download_step(service):
            break
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["attempts"] == downloads.MAX_FAILURES
    assert latest["details"]["phase"] == "recover"
    assert len(archiver_posts(remote)) == downloads.MAX_FAILURES
    with pytest.raises(InvalidInput):
        service.delete(job["id"])
    action(service, job, "cancel")
    service.delete(job["id"])


def test_gp_probe_skips_unavailable_gallery_and_unaffordable_job(queue, monkeypatch):
    service, remote = queue
    first = ready(service)
    second = ready(service, "https://e-hentai.org/g/101/abcdef/")
    for job, cost in [(first, 25), (second, 5)]:
        details = job["details"] | {"phase": "submit", "approved_gp": cost}
        service._update(
            job["id"], state="queued", details=details, error="insufficient_gp", retry_at=downloads.GP_PARKED
        )
    quote = service.sources.quote
    remote.balance = 20

    def probe(source, **kwargs):
        if source.gallery_id == first["source"]["gallery_id"]:
            raise SourceError("gallery_unavailable")
        return quote(source, **kwargs)

    monkeypatch.setattr(service.sources, "quote", probe)
    assert service._gp_probe(force=True)["released"] == 1
    assert service.get(first["id"])["retry_at"] == downloads.GP_PARKED
    assert service.get(second["id"])["retry_at"] == 0
    assert service.get(second["id"])["error_code"] is None
    assert service._gp_probe(force=True)["released"] == 0


def test_low_disk_does_not_block_metadata_or_other_small_transfers(queue, monkeypatch):
    service, _ = queue
    first = transferring(service)
    service._update(first["id"], error="insufficient_storage", retry_at=int(time.time()) + 300)
    second = service.create("https://e-hentai.org/g/101/abcdef/")
    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    assert service.step("meta")
    assert service.get(second["id"])["state"] == "needs_approval"
    action(service, first, "cancel")
    assert service.summary()["storage_wait_until"] == 0


def test_saved_paid_link_survives_restart_with_original_quality(queue):
    service, remote = queue
    job = transferring(service)
    service._update(job["id"], state="submitting")
    service._resume_interrupted()
    assert service.get(job["id"])["details"]["phase"] == "transfer"
    drive(service)
    assert service.get(job["id"])["details"]["archive_variant"] == "original"
    assert len(archiver_posts(remote)) == 1


def test_link_only_vault_detects_missing_key(queue):
    service, _ = queue
    transferring(service)
    with service.library.transaction() as db:
        db.execute("UPDATE source_config SET secrets=''")
    service.vault.path.unlink()
    assert SourceVault(service.library).public()["key_lost"]
    restarted = downloads.Downloads(service.files)
    try:
        assert restarted.summary()["blocked"] == "source_key_lost"
    finally:
        restarted.close()


def test_old_job_details_cannot_overwrite_a_new_balance(queue):
    service, _ = queue
    job = ready(service)
    old = service.get(job["id"], private=True)
    with service.library.transaction() as db:
        db.execute("UPDATE source_account SET balance_gp=123")
    service._update(job["id"], details=old["details"])
    assert service.get(job["id"])["balance_gp"] == 123


@pytest.mark.parametrize("approved", [False, True])
def test_cancelled_requires_explicit_retry_and_keeps_approval(queue, approved):
    service, remote = queue
    job = ready(service)
    if approved:
        action(service, job, "approve")
    action(service, job, "cancel")
    with pytest.raises(InvalidInput, match="Action is not available"):
        action(service, job, "resume")
    assert service.create(EH)["state"] == "cancelled"
    action(service, job, "redownload")
    drive(service)
    latest = service.get(job["id"])
    assert latest["state"] == ("done" if approved else "needs_approval")
    assert len(archiver_posts(remote)) == int(approved)


def test_balance_probe_returns_cached_value_while_another_probe_runs(queue):
    service, remote = queue
    ready(service)
    before = len(remote.requests)
    with service.gp_lock:
        result = service._gp_probe(force=True)
    assert result["balance_gp"] == remote.balance and result["probing"]
    assert len(remote.requests) == before


def test_credential_reset_keeps_cancellation_and_completed_local_archive(queue):
    service, remote = queue
    job = transferring(service)
    download_step(service)
    service._update(job["id"], state="paused")
    other = ready(service, "https://e-hentai.org/g/101/abcdef/")
    service._update(other["id"], state="cancelling")
    service.working[other["id"]] = "ehentai"
    service.stop_requests[other["id"]] = "cancelling"
    service.reset_credentials()
    assert service.stop_requests[other["id"]] == "cancelling"
    assert service.get(other["id"])["state"] == "cancelling"
    action(service, job, "resume")
    drive(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


def test_transfer_finishes_while_library_volume_is_offline(queue):
    service, _ = queue
    job = transferring(service)
    root = service.files.root.path
    offline = root.with_name("offline-books")
    root.rename(offline)
    try:
        assert download_step(service)
        latest = service.get(job["id"])
        assert latest["state"] == "queued" and latest["details"]["phase"] == "import"
        assert latest["error_code"] is None
    finally:
        offline.rename(root)


def test_restart_with_cancelled_import_defers_cleanup_when_root_offline(queue):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    latest = service.get(job["id"])
    import_id = str(uuid.uuid4())
    service.files._stage_path(import_id).parent.mkdir(exist_ok=True)
    with service.library.transaction() as db:
        db.execute("INSERT INTO import_job(id,state,kind) VALUES(?,'copying','copy')", (import_id,))
    service._update(job["id"], state="cancelling", details=latest["details"] | {"import_job": import_id})
    root = service.files.root.path
    offline = root.with_name("offline-books")
    root.rename(offline)
    try:
        service._resume_interrupted()
        assert service.get(job["id"])["state"] == "cancelled"
    finally:
        offline.rename(root)
    service._resume_interrupted()
    assert not service._path(job["id"]).exists()


def test_cancelled_state_wins_before_in_memory_stop_is_published(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)

    def interrupt(_job):
        service._update(job["id"], state="cancelling")
        raise InterruptedError

    monkeypatch.setattr(service, "_inspect", interrupt)
    download_step(service)
    assert service.get(job["id"])["state"] == "cancelled"


def test_busy_interruption_settlement_requeues_without_local_error(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)
    finish = service._finish

    def interrupted(_job):
        raise InterruptedError

    error = locked(service)

    def settlement_busy(*args, **kwargs):
        raise error

    monkeypatch.setattr(service, "_inspect", interrupted)
    monkeypatch.setattr(service, "_finish", settlement_busy)
    download_step(service)
    monkeypatch.setattr(service, "_finish", finish)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["error_code"] == "database_busy"


@pytest.mark.parametrize("bulk", [False, True])
@pytest.mark.parametrize("has_source", [False, True])
def test_failed_manual_refresh_retries_the_pasted_source(queue, tmp_path, bulk, has_source):
    service, remote = queue
    book = import_book(service.files, tmp_path)
    if has_source:
        service.online.refresh(
            book.id, gallery_url("https://e-hentai.org/g/101/abcdef/"), book.metadata_revision
        )
    source = gallery_url(EH)
    service.online.defer(book.id, source, "metadata", 1)
    remote.removed.add(100)
    with service.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and check["state"] == "needs_review"
    assert not service.checks.step()
    remote.removed.clear()
    if bulk:
        assert service.checks.retry_failed() == 1
    else:
        service.checks.action(book.id, "retry", check["generation"])
    assert service.checks.step()
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_task WHERE kind='metadata'").fetchone()[0] == 0
        assert db.execute(
            "SELECT count(*) FROM source_note WHERE book_id=? AND source_url=?", (book.id, source.key)
        ).fetchone()[0]


def test_recovery_credential_failure_waits_for_user_without_losing_paid_state(queue, monkeypatch):
    service, _ = queue
    job = ready(service)
    action(service, job, "approve")

    def fail(*args, **kwargs):
        raise SourceError("source_protocol")

    monkeypatch.setattr(service.sources, "submit", fail)
    download_step(service)

    def credentials(*args, **kwargs):
        raise SourceError("source_login_required")

    monkeypatch.setattr(service.sources, "quote", credentials)
    due(service)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "verification_required"
    assert latest["details"]["phase"] == "recover" and latest["details"]["submission_uncertain"]
    assert not download_step(service)


def test_successful_manual_refresh_replaces_a_failed_pasted_link(queue, tmp_path):
    service, remote = queue
    book = import_book(service.files, tmp_path)
    service.online.defer(book.id, gallery_url(EH), "metadata", 1)
    remote.removed.add(100)
    with service.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")
    assert service.checks.step()
    service.online.refresh(
        book.id, gallery_url("https://e-hentai.org/g/101/abcdef/"), book.metadata_revision, link_source=True
    )
    with service.library.transaction() as db:
        assert db.execute("SELECT count(*) FROM source_task WHERE kind='metadata'").fetchone()[0] == 0
        db.execute("UPDATE source_check SET updated_at=0")
    assert service.checks.schedule_refresh() == 1


def test_repeated_source_network_failures_have_bounded_reason_storage(queue, tmp_path, monkeypatch):
    service, _ = queue
    book = import_book(service.files, tmp_path)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision, link_source=True)
    check = service.checks.get(book.id)
    assert check
    service.checks.action(book.id, "retry", check["generation"])

    def unavailable(*args, **kwargs):
        raise SourceError("source_network")

    monkeypatch.setattr(service.online, "refresh", unavailable)
    for _ in range(12):
        with service.library.transaction() as db:
            db.execute("UPDATE source_check SET retry_at=0")
        assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and check["state"] == "queued" and len(check["reasons"]) == 7


@pytest.mark.parametrize("phase", ["transfer", "import", "recover"])
def test_cancelled_paid_job_retries_once_with_original_approval(queue, phase):
    service, remote = queue
    remote.priced_link = True
    job = transferring(service)
    if phase == "import":
        download_step(service)
    elif phase == "recover":
        latest = service.get(job["id"])
        service._update(
            job["id"], details=latest["details"] | {"phase": "recover", "submission_uncertain": True}
        )
    action(service, job, "cancel")
    action(service, job, "redownload")
    drive(service)
    latest = service.get(job["id"])
    assert latest["state"] == "done"
    assert latest["details"]["archive_variant"] == "original"
    assert not latest["details"].get("may_be_charged")
    assert len(archiver_posts(remote)) == 2


def test_expired_prepared_link_does_not_claim_a_charge(queue):
    service, remote = queue
    remote.reusable = True
    job = ready(service)
    action(service, job, "approve", mode="reuse")
    download_step(service)
    current = service.get(job["id"], private=True)
    link = service.vault.decrypt(current["remote_secret"])
    service._update(job["id"], remote_secret=service.vault.encrypt(link | {"expires_at": 0}))
    remote.reusable = False
    download_step(service)
    due(service)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval"
    assert latest["error_code"] == "source_link_expired"
    assert not latest["details"].get("may_be_charged")
    assert not archiver_posts(remote)


def test_import_hash_verification_is_displayed_as_importing(queue, monkeypatch):
    service, _ = queue
    job = transferring(service)
    download_step(service)
    service.hashed.clear()
    original = downloads.hash_file
    observed = []

    def check(*args, **kwargs):
        observed.append(service.get(job["id"])["state"])
        return original(*args, **kwargs)

    monkeypatch.setattr(downloads, "hash_file", check)
    drive(service)
    assert observed and set(observed) == {"importing"}
    assert service.get(job["id"])["state"] == "done"
