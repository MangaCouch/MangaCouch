"""Deterministic fault injection and seeded queue fuzzing; all source traffic is synthetic."""

from __future__ import annotations

import random
import sqlite3
import threading
from contextlib import closing, contextmanager

import httpx
import pytest
from helpers import action, download_step, ready
from source_fixtures import EH, archiver_posts
from test_downloads import archive_post, due, refuse, restart, run, transferring

from mangacouch import downloads as downloads_module
from mangacouch.database import connect
from mangacouch.files import LibraryRoot


def test_gp_wait_keeps_approval_and_resumes_after_top_up(queue):
    service, remote = queue
    remote.balance = 0
    job = ready(service)
    assert action(service, job, "approve")["state"] == "queued"
    for _ in range(7):  # more often than the retry limit downloads once had
        due(service)
        assert download_step(service)
        latest = service.get(job["id"])
        assert (latest["state"], latest["error_code"]) == ("queued", "insufficient_gp")
        assert latest["details"]["phase"] == "submit"
        assert latest["details"]["approved_gp"] == 25
        assert latest["attempts"] == 0 and latest["retry_at"] > 0
        assert not download_step(service)
    assert not archiver_posts(remote)
    restart(service)
    remote.balance = 100
    due(service)
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


def test_gp_exhausted_between_quote_and_post_is_safe_to_retry(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    switch = refuse(remote, service, archive_post, httpx.Response(200, text="Insufficient funds"))
    download_step(service)
    latest = service.get(job["id"])
    assert latest["error_code"] == "insufficient_gp"
    assert latest["state"] == "queued" and latest["details"]["phase"] == "submit"
    assert not latest["details"]["submission_uncertain"]
    switch["on"] = False
    due(service)
    run(service)
    assert service.get(job["id"])["state"] == "done"


def test_price_increase_while_waiting_for_gp_requires_new_approval(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    remote.balance = 0
    download_step(service)
    remote.balance, remote.cost = 5000, 99
    due(service)
    download_step(service)
    assert service.get(job["id"])["state"] == "needs_approval"
    assert not archiver_posts(remote)


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_file_lock_wait_is_interruptible_before_import_publication(queue, files, monkeypatch, command):
    service, remote = queue
    pending, settled = ("pausing", "paused") if command == "pause" else ("cancelling", "cancelled")
    job = transferring(service)
    download_step(service)  # transfer is complete, import is next
    entered, errors = threading.Event(), []
    mutation = LibraryRoot.mutation

    @contextmanager
    def observed(self, **options):
        if options.get("cancelled"):
            entered.set()
        with mutation(self, **options):
            yield

    monkeypatch.setattr(LibraryRoot, "mutation", observed)

    def work():
        try:
            download_step(service)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=work)
    try:
        with files.root.mutation():
            thread.start()
            assert entered.wait(3)
            assert action(service, job, command)["state"] in {pending, settled}
            thread.join(3)
            assert not thread.is_alive(), "pause/cancel must settle while the file lock is still held"
    finally:
        thread.join(5)
    assert not errors
    assert service.get(job["id"])["state"] == settled
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 0
    if command == "pause":
        action(service, job, "resume")
        run(service)
        assert service.get(job["id"])["state"] == "done"
        assert len(archiver_posts(remote)) == 1


@pytest.mark.parametrize("command", ["pause", "cancel"])
def test_database_lock_outlasting_settlement_does_not_strand_job(queue, monkeypatch, command):
    service, _ = queue
    job = service.create(EH)
    service.library.busy_timeout = 0.01
    monkeypatch.setattr(downloads_module, "PARK_TRIES", 1)
    with closing(connect(service.library.path)) as holder:

        def interrupted(_job):
            action(service, job, command)
            holder.execute("BEGIN IMMEDIATE")
            raise InterruptedError

        monkeypatch.setattr(service, "_inspect", interrupted)
        try:
            assert download_step(service)
            assert job["id"] in service.pending_parks
            assert job["id"] in service.working
        finally:
            holder.execute("ROLLBACK")
        download_step(service)  # settlement is retried without restarting the server
    assert not service.pending_parks and not service.working
    assert service.get(job["id"])["state"] == ("paused" if command == "pause" else "cancelled")


def test_failed_claim_commit_does_not_leak_worker_ownership(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)
    transaction = service.library.transaction

    @contextmanager
    def failed_commit(**options):
        with transaction(**options) as db:
            yield db
            raise sqlite3.OperationalError("injected commit failure")

    with monkeypatch.context() as patch:
        patch.setattr(service.library, "transaction", failed_commit)
        with pytest.raises(sqlite3.OperationalError):
            download_step(service)
    assert not service.working and not service.meta_working
    assert service.get(job["id"])["state"] == "queued"
    assert download_step(service)
    assert service.get(job["id"])["state"] == "needs_approval"


@pytest.mark.parametrize("random_seed", range(6))
def test_seeded_download_fault_sequences(queue, random_seed):
    service, remote = queue
    rng = random.Random(random_seed)
    service.library.busy_timeout = 0.001
    job = service.create(EH)
    trace = []
    for _ in range(60):
        latest = service.get(job["id"], private=True)
        state = latest["state"]
        command = rng.choice(["step", "step", "funds", "restart", "pause", "resume", "approve", "locked"])
        trace.append((state, command, remote.balance))
        if command == "funds":
            remote.balance = rng.choice([0, 24, 25, 100])
        elif command == "restart":
            restart(service)
        elif command == "pause" and state == "queued":
            action(service, latest, "pause")
        elif command == "resume" and state == "paused":
            action(service, latest, "resume")
        elif command == "approve" and state == "needs_approval":
            action(service, latest, "approve")
        elif command == "locked" and state == "queued":
            due(service)
            with closing(connect(service.library.path)) as holder:
                holder.execute("BEGIN IMMEDIATE")
                try:
                    with pytest.raises(sqlite3.OperationalError):
                        download_step(service)
                finally:
                    holder.execute("ROLLBACK")
        elif command == "step":
            due(service)
            download_step(service)
        latest = service.get(job["id"], private=True)
        assert not service.working and not service.pending_parks, trace
        assert latest["state"] not in downloads_module.ACTIVE | {"pausing", "cancelling"}, trace
        if latest["error_code"] == "insufficient_gp":
            assert latest["state"] == "queued" and latest["details"]["approved_gp"] == 25, trace
        assert len(archiver_posts(remote)) <= 1, trace
    remote.balance = 5000
    latest = service.get(job["id"])
    if latest["state"] == "paused":
        action(service, latest, "resume")
    for _ in range(6):
        latest = service.get(job["id"])
        if latest["state"] == "needs_approval":
            action(service, latest, "approve")
        due(service)
        download_step(service)
    assert service.get(job["id"])["state"] == "done", trace


def test_cleared_record_does_not_keep_a_pending_worker_slot(queue):
    service, _ = queue
    job = service.create(EH)
    action(service, job, "cancel")
    service.working[job["id"]] = "ehentai"
    service.pending_parks[job["id"]] = False
    service.delete(job["id"])
    download_step(service)
    assert not service.pending_parks and not service.working
