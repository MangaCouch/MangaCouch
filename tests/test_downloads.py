from __future__ import annotations

import errno
import json
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, PASSCODE, action, download_step, drive, login, ready
from helpers import finish_download as finish
from source_fixtures import ARCHIVE, EH, NH, archiver_posts, end_halts

from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.database import RevisionConflict, connect
from mangacouch.downloads import (
    META_WORKERS,
    WORKER_BUSY_TIMEOUT,
    WORKERS,
    Downloads,
)
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.files import RootUnavailable
from mangacouch.http import create_app
from mangacouch.models import PersonalState
from mangacouch.review import Review
from mangacouch.snapshots import create_snapshot, restore_snapshot
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceError


def transfers(remote) -> list[httpx.Request]:
    return [r for r in remote.requests if r.url.host == "archive.example"]


def transferring(service):
    """An approved EH job whose archive was requested: queued for the transfer."""
    job = ready(service)
    action(service, job, "approve")
    download_step(service)
    return job


def run(service, steps: int | None = None) -> None:
    if steps is None:
        drive(service)
    else:
        for _ in range(steps):
            if not download_step(service):
                break


# Seams into Downloads ---------------------------------------------------------------------------------
# Stored states the source fixture cannot produce on demand are seeded here; the tests below never
# touch the service's private members themselves.


def archive_path(service: Downloads, job: dict) -> Path:
    """The job's archive while it downloads."""
    service.directory.mkdir(mode=0o700, exist_ok=True)
    return service.directory / f"{job['id']}.zip"


def seed(service: Downloads, job: dict, **fields: Any) -> None:
    """Store job fields the way a worker does (state, details, progress, remote link)."""
    service._update(job["id"], **fields)


def due(service: Downloads) -> None:
    """The retry delay of every waiting job is over."""
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET retry_at=0")


def restart(service: Downloads) -> None:
    """What a new server does with the jobs a stopped one left behind, without starting workers."""
    service._resume_interrupted()


def refuse(remote, service, match, response) -> dict:
    """Answer requests that `match` with `response` (an httpx.Response or an exception to raise)
    while the returned switch's "on" is true."""
    original, switch = remote.__call__, {"on": True}

    def handler(request):
        if switch["on"] and match(request):
            remote.requests.append(request)
            if isinstance(response, Exception):
                raise response
            return response
        return original(request)

    service.http.transport.handler = handler
    return switch


def archive_post(request) -> bool:
    return request.method == "POST" and request.url.path == "/archiver.php"


def stale_cleanup(service: Downloads, job: dict, states: set[str]) -> None:
    """A cleanup that was decided while the job was in one of `states` and runs only now."""
    service._cleanup(job["id"], states)


def cancellation_probe(service: Downloads, job: dict) -> bool:
    """What a worker sees when it checks whether to stop."""
    return service._cancelled(job["id"])


def break_settlement(service: Downloads, monkeypatch) -> None:
    """Settling a stopped phase (retry, pause, cancel, failure) raises."""

    def broken(identifier, error, **kwargs):
        raise RuntimeError("injected settlement failure")

    monkeypatch.setattr(service, "_finish", broken)


def locked(service: Downloads) -> sqlite3.OperationalError:
    """A real "database is locked": a write tried while another connection holds the write lock."""
    path = service.library.path
    with closing(connect(path)) as holder, closing(connect(path, timeout=0)) as other:
        holder.execute("BEGIN IMMEDIATE")
        try:
            other.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as error:
            return error
        finally:
            holder.execute("ROLLBACK")
    raise AssertionError("The write lock was not held")


class HeldCleanup:
    """Holds a cancellation's archive cleanup, which runs under the job's lock, until `release` is set.

    `entered` is set once the cleanup holds the lock; `waiting` once another thread asks for it.
    """

    def __init__(self, service: Downloads, job: dict, monkeypatch):
        self.entered, self.release, self.waiting = threading.Event(), threading.Event(), threading.Event()
        self.holder: threading.Thread | None = None
        remove, lock_of = service._remove_files, service._job_lock
        job_lock = lock_of(job["id"])

        def held_remove(identifier):
            self.holder = threading.current_thread()
            self.entered.set()
            assert self.release.wait(5)
            remove(identifier)

        @contextmanager
        def observed(identifier):
            lock = lock_of(identifier)
            if lock is job_lock and self.entered.is_set() and threading.current_thread() is not self.holder:
                self.waiting.set()
            with lock:
                yield

        monkeypatch.setattr(service, "_remove_files", held_remove)
        monkeypatch.setattr(service, "_job_lock", observed)


# Queueing and completion ------------------------------------------------------------------------------


def test_complete_download_is_durable_readable_and_deduplicated(queue, files):
    service, remote = queue
    job = ready(service)
    assert not any(
        r.method == "POST" and r.url.path in {"/archiver.php", "/api/v2/galleries/200/download"}
        for r in remote.requests
    )
    assert service.create(EH)["id"] == job["id"]
    done = finish(service, job)
    assert done["state"] == "done", done
    book = files.library.get_book(done["book_id"])
    assert book.content.page_count == 3 and files.page_bytes(book.id, 0).startswith(b"\x89PNG")
    assert book.review_state == "confirmed", Review(files).preview(book.id)
    assert not archive_path(service, job).exists()
    assert "token=fixture" not in json.dumps(done)
    with files.library.connection() as db:
        secret = db.execute("SELECT remote_secret FROM download_job").fetchone()[0]
        assert "token=fixture" not in secret


def test_new_download_with_identical_bytes_preserves_existing_personal(queue, files, tmp_path):
    service, _ = queue
    book = files.import_file(tmp_path / "remote.zip").book
    personal = PersonalState(rating=4.5, resume_page=1, completed=True)
    files.library.save_personal(book.id, personal, expected_revision=1)
    done = finish(service, ready(service))
    assert done["state"] == "done" and done["details"]["duplicate"]
    assert done["book_id"] == book.id and files.library.get_book(book.id).personal == personal


def test_download_size_does_not_inherit_small_upload_limit(queue, files):
    from dataclasses import replace

    service, _ = queue
    files.limits = replace(files.limits, max_archive_bytes=1)
    done = finish(service, ready(service))
    assert done["state"] == "done"


def test_batch_creation_validates_collapses_and_runs_like_single_jobs(queue):
    service, remote = queue
    items = service.create_many(
        [EH, "not a url", NH, "https://example.com/g/1/", EH.rstrip("/"), NH + "?ref=x"]
    )
    assert [item["error"] for item in items] == [
        None,
        "invalid_url",
        "unsupported_source",
        "unsupported_source",
        None,
        "unsupported_source",
    ]
    assert items[0]["job"]["id"] == items[4]["job"]["id"]
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM download_job").fetchone()[0] == 1
    download_step(service)
    assert service.get(items[0]["job"]["id"])["state"] == "needs_approval"
    assert all(request.url.host != "nhentai.net" for request in remote.requests)


def test_readding_paused_download_preserves_resume_state(queue):
    service, _ = queue
    job = transferring(service)
    assert action(service, job, "pause")["state"] == "paused"
    before = service.get(job["id"], private=True)
    assert before["remote_secret"] and before["details"]["phase"] == "transfer"
    assert service.create(job["input_url"])["outcome"] == "existing"
    assert service.get(job["id"], private=True) == before


@pytest.mark.parametrize("stale_after_resume", [False, True])
def test_a_pause_left_over_from_an_earlier_run_does_not_stop_the_resumed_job(queue, stale_after_resume):
    """A pause can reach the in-memory signal just after the worker parked the job; that stale
    signal must not stop the job once it is resumed."""
    service, _ = queue
    job = transferring(service)
    assert action(service, job, "pause")["state"] == "paused"
    service.interrupted.add(job["id"])
    action(service, job, "resume")
    if stale_after_resume:
        service.interrupted.add(job["id"])
    run(service)
    assert service.get(job["id"])["state"] == "done"


def test_restart_continues_previously_queued_work(queue):
    service, _ = queue
    job = service.create(EH)
    service.start()
    service.close()
    assert service.get(job["id"])["state"] in {"queued", "needs_approval"}


def test_a_queued_job_another_worker_still_owns_is_not_claimed(queue, monkeypatch):
    """A worker that queued its job again (shutdown, retry) owns it until it has parked it; a
    second worker claiming it meanwhile marked it inspecting, and the first then parked it as
    download_local_error."""
    service, _ = queue
    job = service.create(EH)
    revision = service.get(job["id"])["revision"]
    transaction = service.library.transaction

    @contextmanager
    def first_worker_queues_it_again(*args, **kwargs):
        # The other worker settles the job between this worker's look and its claim.
        with service.worker_lock:
            service.working[job["id"]] = "ehentai"
            service.meta_working.add(job["id"])
        with transaction(*args, **kwargs) as db:
            yield db

    monkeypatch.setattr(service.library, "transaction", first_worker_queues_it_again)
    assert service.step("meta") is False
    monkeypatch.undo()
    current = service.get(job["id"])
    assert (current["state"], current["revision"]) == ("queued", revision)
    service._release_worker(job["id"], busy_failure=False)
    assert service.get(job["id"])["state"] == "queued"


@pytest.mark.parametrize(
    "state,phase,uncertain,after",
    [
        ("queued", "inspect", False, "queued"),
        ("inspecting", "inspect", False, "queued"),
        ("inspecting", "submit", False, "queued"),
        ("downloading", "transfer", False, "queued"),
        ("importing", "import", False, "queued"),
        ("queued", "recover", True, "queued"),
        ("submitting", "uncertain", True, "queued"),
        ("pausing", "transfer", False, "paused"),
        ("cancelling", "transfer", False, "cancelled"),
        ("paused", "transfer", False, "paused"),
        ("needs_approval", "inspect", False, "needs_approval"),
    ],
)
def test_restart_continues_each_interrupted_job_in_its_phase(queue, state, phase, uncertain, after):
    service, _ = queue
    job = transferring(service)
    details = service.get(job["id"], private=True)["details"]
    details.update(phase=phase, submission_uncertain=uncertain)
    seed(service, job, state=state, details=details)
    restart(service)
    latest = service.get(job["id"])
    assert latest["state"] == after and latest["details"]["phase"] == ("recover" if uncertain else phase)


@pytest.mark.parametrize("interrupted", ["queued", "inspecting", "approved", "downloading", "importing"])
def test_interrupted_jobs_finish_after_a_restart_without_paying_again(queue, interrupted):
    service, remote = queue
    if interrupted in {"queued", "inspecting"}:
        job = service.create(EH)
        if interrupted == "inspecting":
            seed(service, job, state="inspecting")
    else:
        job = ready(service)
        action(service, job, "approve")
        if interrupted != "approved":
            run(service, 2 if interrupted == "importing" else 1)
            phase = service.get(job["id"])["details"]["phase"]
            assert phase == ("import" if interrupted == "importing" else "transfer")
        seed(service, job, state="inspecting" if interrupted == "approved" else interrupted)
    restart(service)
    assert service.get(job["id"])["state"] == "queued"
    run(service)
    latest = service.get(job["id"])
    if interrupted in {"queued", "inspecting"}:
        assert latest["state"] == "needs_approval" and not archiver_posts(remote)
    else:
        assert latest["state"] == "done" and len(archiver_posts(remote)) == 1


def test_restart_clears_retry_delays_that_have_passed_and_keeps_the_others(queue):
    service, _ = queue
    passed, waiting = service.create(EH), service.create("https://e-hentai.org/g/101/abcdef/")
    seed(service, passed, retry_at=int(time.time()) - 60, error="source_network")
    seed(service, waiting, retry_at=int(time.time()) + 600, error="source_rate_limited")
    restart(service)
    assert service.get(passed["id"])["retry_at"] == 0
    latest = service.get(waiting["id"])
    assert latest["state"] == "queued" and latest["retry_at"] > time.time() + 500


def test_shutdown_leaves_running_jobs_to_continue_after_the_restart(queue):
    service, remote = queue
    job = transferring(service)

    class StoppingStream(httpx.SyncByteStream):
        def __iter__(self):
            service.stopping.set()
            yield remote.payload

    refuse(
        remote,
        service,
        lambda request: request.url.host == "archive.example",
        httpx.Response(200, stream=StoppingStream(), headers={"content-type": "application/zip"}),
    )
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["details"]["phase"] == "transfer"
    assert latest["error_code"] is None


def test_shutdown_after_a_phase_settled_keeps_the_settled_state(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)
    quote = service.sources.quote

    def stop_after(*args, **kwargs):
        result = quote(*args, **kwargs)
        service.stopping.set()
        return result

    monkeypatch.setattr(service.sources, "quote", stop_after)
    download_step(service)
    assert service.get(job["id"])["state"] == "needs_approval"


# Approval and prices ----------------------------------------------------------------------------------


def test_price_change_requires_new_approval_and_original_is_default(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    remote.cost = 99
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["error_code"] == "source_price_changed"
    assert latest["details"]["quote"]["original_gp"] == 99
    assert not archiver_posts(remote)
    action(service, job, "approve")
    download_step(service)
    submitted = archiver_posts(remote)
    assert len(submitted) == 1 and b"dltype=org" in submitted[0].content


def test_prepared_archive_that_appears_after_approval_is_offered_instead_of_paying(queue):
    service, remote = queue
    job = ready(service)
    assert job["details"]["reusable"] is False
    action(service, job, "approve")
    remote.reusable = True
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["error_code"] == "source_price_changed"
    assert latest["details"]["mode"] == "reuse" and latest["details"]["reusable"] is True
    assert not archiver_posts(remote)
    action(service, latest, "approve", mode="reuse")
    run(service)
    done = service.get(job["id"])
    assert done["state"] == "done" and done["details"]["archive_variant"] == "unknown"
    assert not archiver_posts(remote)


@pytest.mark.parametrize("still_offered", [True, False])
def test_approved_prepared_archive_is_checked_again_before_anything_is_paid(queue, still_offered):
    service, remote = queue
    remote.reusable = remote.priced_link = True
    job = ready(service)
    assert job["details"]["reusable"] and job["details"]["mode"] == "org"
    # Choosing a new original archive drops the prepared link; a price change asks again, and this
    # time the prepared archive is chosen without a link in hand.
    action(service, job, "approve")
    remote.cost = 99
    download_step(service)
    assert service.get(job["id"])["error_code"] == "source_price_changed"
    action(service, job, "approve", mode="reuse")
    remote.reusable = still_offered
    download_step(service)
    latest = service.get(job["id"])
    if still_offered:
        assert latest["state"] == "queued" and latest["details"]["phase"] == "transfer"
        run(service)
        assert service.get(job["id"])["state"] == "done"
    else:
        assert latest["state"] == "needs_approval" and latest["error_code"] == "source_price_changed"
        assert latest["details"]["mode"] == "org" and latest["details"]["reusable"] is False
        assert service.get(job["id"], private=True)["remote_secret"] is None
    assert not archiver_posts(remote)


@pytest.mark.parametrize("interruption", ["cancel", "pause", "crash", "paused_by_restart"])
def test_a_prepared_archive_an_inspection_offers_is_never_fetched_without_approval(
    queue, monkeypatch, interruption
):
    """The inspection stores the prepared link before it asks for approval; whatever stops the
    job in between, resuming it asks again instead of transferring the unapproved archive."""
    service, remote = queue
    remote.reusable = True
    job = service.create(EH)
    if interruption == "pause":
        quote = service.sources.quote

        def paused(*args, **kwargs):
            result = quote(*args, **kwargs)
            action(service, job, "pause")
            return result

        monkeypatch.setattr(service.sources, "quote", paused)
        download_step(service)
        monkeypatch.undo()
        assert service.get(job["id"])["state"] == "paused"
    else:
        download_step(service)
        assert service.get(job["id"])["state"] == "needs_approval"
        if interruption == "cancel":
            action(service, job, "cancel")
        elif interruption == "crash":
            # The server stopped after storing the offered link, before asking for approval.
            seed(service, job, state="inspecting")
            restart(service)
        else:
            seed(service, job, state="paused")
    assert service.get(job["id"], private=True)["remote_secret"]
    if interruption != "crash":
        assert (
            action(service, job, "redownload" if interruption == "cancel" else "resume")["details"]["phase"]
            == "inspect"
        )
    run(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["details"]["mode"] == "reuse"
    assert not transfers(remote) and not archiver_posts(remote)
    action(service, latest, "approve", mode="reuse")
    run(service)
    assert service.get(job["id"])["state"] == "done" and not archiver_posts(remote)


@pytest.mark.parametrize("cause", ["recovered", "appeared"])
def test_a_prepared_archive_offered_after_approval_is_never_fetched_without_approval(queue, cause):
    service, remote = queue
    job = ready(service)
    if cause == "recovered":
        remote.download_error = True
        action(service, job, "approve")
        download_step(service)
        remote.download_error, remote.reusable = False, True
        due(service)
    else:
        action(service, job, "approve")
        remote.reusable = True
    download_step(service)
    if cause == "recovered":
        run(service)
        latest = service.get(job["id"])
        assert latest["state"] == "done"
        assert latest["details"]["archive_variant"] == "unknown"
        return
    assert service.get(job["id"])["state"] == "needs_approval"
    action(service, job, "cancel")
    action(service, job, "redownload")
    run(service)
    assert service.get(job["id"])["state"] == "needs_approval" and not transfers(remote)
    assert service.get(job["id"])["details"]["reusable"]
    assert service.get(job["id"])["details"]["mode"] == "reuse"


@pytest.mark.parametrize(
    "stop,mode,cost,after",
    [
        ("pause", "org", 25, "done"),
        ("cancel", "org", 25, "done"),
        ("cancel", "org", 0, "done"),
        ("cancel", "res", 25, "done"),
        ("cancel", "org", 99, "needs_approval"),
    ],
)
def test_paused_resume_and_cancelled_retry_keep_the_approved_price(queue, stop, mode, cost, after):
    """A cancelled retry retains the price ceiling and asks again only for a higher price."""
    service, remote = queue
    job = ready(service)
    action(service, job, "approve", mode=mode)
    action(service, job, stop)
    remote.cost = cost
    assert action(service, job, "resume" if stop == "pause" else "redownload")["details"]["phase"] == "submit"
    run(service)
    latest = service.get(job["id"])
    assert latest["state"] == after and not service.settings.auto_free
    assert len(archiver_posts(remote)) == (1 if after == "done" else 0)
    if after == "done":
        assert latest["details"]["mode"] == mode and "requote" not in latest["details"]
    else:
        assert latest["details"]["mode"] == mode and latest["details"]["approved_gp"] == 25
        assert latest["details"]["quote"]["original_gp"] == 99


@pytest.mark.parametrize("price_changes", [False, True])
def test_an_approved_submission_refused_by_a_rate_limit_continues_by_itself(queue, price_changes):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    limit = refuse(remote, service, archive_post, httpx.Response(429, headers={"retry-after": "120"}))
    download_step(service)
    waiting = service.get(job["id"])
    assert waiting["state"] == "queued" and waiting["error_code"] == "source_rate_limited"
    assert waiting["details"]["phase"] == "recover" and waiting["details"]["submission_uncertain"]
    assert waiting["retry_at"] >= time.time() + 100 and waiting["attempts"] == 0
    assert not download_step(service)
    limit["on"] = False
    end_halts(service.http)
    due(service)
    if price_changes:
        remote.cost = 99
    run(service)
    latest = service.get(job["id"])
    if price_changes:
        assert latest["state"] == "needs_approval" and latest["error_code"] == "source_price_changed"
        assert len(archiver_posts(remote)) == 1
    else:
        assert latest["state"] == "done" and len(archiver_posts(remote)) == 2


def test_an_approved_submission_that_never_connected_is_sent_again(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    down = refuse(remote, service, archive_post, httpx.ConnectError("fixture: no route"))
    download_step(service)
    waiting = service.get(job["id"])
    assert waiting["state"] == "queued" and waiting["error_code"] == "source_network"
    assert waiting["details"]["phase"] == "submit" and waiting["attempts"] == 1
    down["on"] = False
    due(service)
    run(service)
    assert service.get(job["id"])["state"] == "done"


def test_recovery_waits_out_a_rate_limit_and_continues_by_itself(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    download_step(service)
    assert service.get(job["id"])["state"] == "queued"
    limit = refuse(
        remote,
        service,
        lambda request: request.method == "GET" and request.url.path == "/archiver.php",
        httpx.Response(429, headers={"retry-after": "60"}),
    )
    due(service)
    download_step(service)
    waiting = service.get(job["id"])
    assert waiting["state"] == "queued" and waiting["error_code"] == "source_rate_limited"
    assert waiting["details"]["phase"] == "recover" and waiting["retry_at"] > time.time()
    limit["on"] = False
    end_halts(service.http)
    due(service)
    remote.reusable = True
    download_step(service)
    recovered = service.get(job["id"])
    assert recovered["state"] == "queued" and recovered["details"]["mode"] == "reuse"
    assert len(archiver_posts(remote)) == 1


def test_paying_one_job_shares_the_remaining_balance_with_the_others(queue):
    service, _ = queue
    first = ready(service)
    second = ready(service, EH.replace("/100/", "/101/"))
    assert second["details"]["quote"]["balance_gp"] == 5000
    action(service, first, "approve")
    latest = service.get(second["id"])
    assert latest["details"]["quote"]["balance_gp"] == 5000  # approval alone spends nothing
    # The balance is informational: a pending approve of the other job still carries a valid revision.
    assert latest["revision"] == second["revision"]
    assert download_step(service)
    # The archiver page quoted 5000 again right before the charge; the rest is still 4975.
    assert service.get(second["id"])["details"]["quote"]["balance_gp"] == 4975
    assert service.get(first["id"])["details"]["quote"]["balance_gp"] == 4975
    action(service, second, "approve")
    assert service.get(second["id"])["state"] == "queued"


# Actions ----------------------------------------------------------------------------------------------


def test_actions_outside_their_states_are_refused_and_leave_the_job_alone(queue):
    service, _ = queue
    job = ready(service)
    for command in ("search", "choose", "resume", "recover", "redownload", "pause"):
        with pytest.raises(InvalidInput) as caught:
            action(service, job, command)
        assert caught.value.code == "action_unavailable", command
    with pytest.raises(InvalidInput) as caught:
        action(service, job, "approve", mode="invalid")
    assert caught.value.code == "invalid_mode"
    assert service.get(job["id"]) == job
    done = finish(service, job)
    with pytest.raises(InvalidInput) as caught:
        action(service, done, "cancel")
    assert caught.value.code == "download_done"
    assert service.get(job["id"]) == done


def test_stale_revision_refuses_every_action_but_cancel(queue):
    service, _ = queue
    job = ready(service)
    with pytest.raises(RevisionConflict):
        service.action(job["id"], "approve", job["revision"] - 1)
    assert service.get(job["id"]) == job
    cancelled = service.action(job["id"], "cancel", job["revision"] - 1)
    assert cancelled["state"] == "cancelled"


def test_download_progress_does_not_invalidate_actions(queue):
    service, _ = queue
    job = ready(service)
    seed(service, job, state="downloading")
    job = service.get(job["id"])
    service._progress(job["id"], 1024)
    assert service.get(job["id"])["revision"] == job["revision"]
    paused = service.action(job["id"], "pause", job["revision"])
    assert paused["state"] == "pausing"
    assert action(service, paused, "cancel")["state"] == "cancelling"


def test_finished_downloads_can_be_cleared_while_live_ones_cannot(queue, files):
    service, _ = queue
    job = ready(service)
    with pytest.raises(InvalidInput):
        service.delete(job["id"])
    done = finish(service, job)
    assert done["state"] == "done" and done["book_id"]
    service.delete(job["id"])
    with pytest.raises(NotFound):
        service.get(job["id"])
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book WHERE id=?", (done["book_id"],)).fetchone()[0] == 1
    # The URL is free again: a new request is a new job rather than a reset of the old row.
    again = service.create(EH)
    assert again["id"] != job["id"] and again["state"] == "queued"
    # A failed job is cleared in one step; one that may hold a paid request is not.
    failed = service.create("https://e-hentai.org/g/101/abcdef/")
    for state in ("paused", "verification_required"):
        seed(service, failed, state=state)
        with pytest.raises(InvalidInput):
            service.delete(failed["id"])
    seed(service, failed, state="error", error="source_unavailable")
    service.delete(failed["id"])
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        login(client)
        assert client.delete(f"/api/downloads/{again['id']}").status_code == 403
        assert client.delete(f"/api/downloads/{again['id']}", headers=CSRF).status_code == 422
        action(service, again, "cancel")
        # The app's worker may have started the job: cancelling then settles once it stops.
        deadline = time.monotonic() + 10
        while service.get(again["id"])["state"] != "cancelled":
            assert time.monotonic() < deadline, service.get(again["id"])["state"]
            time.sleep(0.02)
        assert client.delete(f"/api/downloads/{again['id']}", headers=CSRF).status_code == 204
        assert client.get(f"/api/downloads/{again['id']}").status_code == 404
        assert client.delete(f"/api/downloads/{again['id']}", headers=CSRF).status_code == 404


# Uncertain submissions --------------------------------------------------------------------------------


@pytest.mark.parametrize("charged", [True, False])
def test_unknown_remote_post_is_automatically_recovered_at_the_approved_price(queue, charged):
    """The approved archive is requested again: free if EH charged the first request (it unlocks
    a bought archive), at the approved price if not. A higher price needs another approval."""
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    remote.charge_before_error = charged
    action(service, job, "approve")
    download_step(service)
    assert service.get(job["id"])["state"] == "queued"
    # A restart preserves the short recovery delay.
    restart(service)
    assert service.get(job["id"])["state"] == "queued" and not download_step(service)
    assert len(archiver_posts(remote)) == 1
    remote.download_error = False
    due(service)
    run(service)
    latest = service.get(job["id"])
    assert latest["state"] == "done" and latest["details"]["archive_variant"] == "original"
    assert len(archiver_posts(remote)) == 2

    assert remote.balance == 4975


def test_recovery_waits_for_the_user_when_the_archive_now_costs_more(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    download_step(service)
    remote.download_error, remote.cost = False, 99
    due(service)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["error_code"] == "source_price_changed"
    assert latest["details"]["may_be_charged"]
    assert len(archiver_posts(remote)) == 1


def test_failed_recovery_reads_again_before_requesting_anything(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    download_step(service)
    due(service)
    original = service.sources.quote

    def failed(*args, **kwargs):
        raise SourceError("source_network")

    service.sources.quote = failed
    download_step(service)
    # A network failure of the archiver page read retries by itself, without posting.
    retrying = service.get(job["id"])
    assert retrying["state"] == "queued" and retrying["details"]["phase"] == "recover"
    assert retrying["attempts"] == 2 and retrying["details"]["submission_uncertain"]
    assert len(archiver_posts(remote)) == 1
    service.sources.quote = original
    remote.download_error = False
    due(service)
    run(service)
    assert service.get(job["id"])["state"] == "done" and len(archiver_posts(remote)) == 2


def test_eh_unknown_result_can_recover_existing_archive_with_get(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    download_step(service)
    remote.reusable = True
    due(service)
    download_step(service)
    recovered = service.get(job["id"])
    assert recovered["state"] == "queued"
    assert recovered["details"]["mode"] == "reuse"
    run(service, 3)
    assert service.get(job["id"])["state"] == "done"
    assert service.get(job["id"])["details"]["archive_variant"] == "unknown"
    assert len(archiver_posts(remote)) == 1


def test_unknown_submission_snapshot_pauses_recovery_until_explicit_resume(queue, files, tmp_path):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    download_step(service)
    due(service)
    snapshot = create_snapshot(files, tmp_path / "unknown.zip")
    restored = restore_snapshot(snapshot, tmp_path / "unknown-restored", root=files.root.path)
    new = Downloads(type(files)(restored.library, files.root.path))
    try:
        assert new.get(job["id"])["state"] == "paused"
        assert new.http.blocked == "source_restore_paused"
        assert not download_step(new)
        settings, _, revision = new.vault.load()
        new.save_settings(settings.model_copy(update={"online_paused": False}), {}, revision)
        assert not download_step(new)
        action(new, new.get(job["id"]), "resume")
        assert new.get(job["id"])["state"] == "queued"
        assert new.get(job["id"])["details"]["phase"] == "recover"
    finally:
        new.close()


@pytest.mark.parametrize("uncertain", [False, True])
def test_download_unexpected_failure_logs_and_preserves_submission_uncertainty(
    queue, monkeypatch, caplog, uncertain
):
    service, _ = queue

    def fail(*args, **kwargs):
        raise RuntimeError("injected download failure")

    if uncertain:
        job = ready(service)
        action(service, job, "approve")
        monkeypatch.setattr(service.sources, "submit", fail)
    else:
        job = service.create(EH)
        monkeypatch.setattr(service.sources, "fetch", fail)
    assert download_step(service)
    result = service.get(job["id"])
    assert result["state"] == ("queued" if uncertain else "error")
    assert result["error_code"] == "download_local_error" and not download_step(service)
    record = next(record for record in caplog.records if job["id"] in record.message)
    assert record.exc_info and "injected download failure" in caplog.text


@pytest.mark.parametrize("cancelled", [False, True])
def test_failed_settlement_parks_the_job_instead_of_stranding_it(queue, monkeypatch, caplog, cancelled):
    service, _ = queue
    job = service.create(EH)

    def refuse(*args, **kwargs):
        if cancelled:
            assert action(service, job, "cancel")["state"] == "cancelling"
        raise SourceError("source_unavailable")

    monkeypatch.setattr(service.sources, "fetch", refuse)
    break_settlement(service, monkeypatch)
    with pytest.raises(RuntimeError, match="injected settlement failure"):
        download_step(service)
    latest = service.get(job["id"])
    if cancelled:
        assert latest["state"] == "cancelled"
    else:
        assert latest["state"] == "error" and latest["error_code"] == "download_local_error"
        assert action(service, job, "cancel")["state"] == "cancelled"
    assert not download_step(service) and "could not be parked" not in caplog.text


def test_workers_wait_longer_for_the_database_than_requests(queue, files):
    service, _ = queue
    assert service.library.path == files.library.path
    assert service.library is files.library
    assert files.library.busy_timeout < WORKER_BUSY_TIMEOUT


def test_busy_database_requeues_the_job_instead_of_failing_it(queue, monkeypatch, caplog):
    service, _ = queue
    job = service.create(EH)
    fetch = service.sources.fetch

    def refuse(*args, **kwargs):
        raise locked(service)

    monkeypatch.setattr(service.sources, "fetch", refuse)
    assert download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["error_code"] == "database_busy"
    assert latest["attempts"] == 0 and latest["retry_at"] - int(time.time()) >= 14
    assert not download_step(service) and "failed" not in caplog.text and "Traceback" not in caplog.text
    monkeypatch.setattr(service.sources, "fetch", fetch)
    due(service)
    run(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["error_code"] is None


@pytest.mark.parametrize("uncertain", [False, True])
def test_busy_settlement_requeues_unless_a_charge_is_uncertain(queue, monkeypatch, caplog, uncertain):
    from mangacouch import downloads

    service, _ = queue
    monkeypatch.setattr(downloads, "BUSY_RETRY", 0)
    if uncertain:
        job = ready(service)
        action(service, job, "approve")
        phase = "submit"
    else:
        job = service.create(EH)
        phase = "fetch"
    still_busy = [True]
    get = service.get

    def refuse(*args, **kwargs):
        raise locked(service)

    def flaky_get(identifier, **kwargs):
        # The phase and its settlement fail; the first read while parking the job does too.
        if not still_busy:
            return get(identifier, **kwargs)
        still_busy.clear()
        raise locked(service)

    def broken(identifier, error, **kwargs):
        monkeypatch.setattr(service, "get", flaky_get)
        raise locked(service)

    monkeypatch.setattr(service.sources, phase, refuse)
    monkeypatch.setattr(service, "_finish", broken)
    assert download_step(service)
    monkeypatch.setattr(service, "get", get)
    latest = service.get(job["id"])
    if uncertain:
        # The archive request may have been charged: inspect its remote result before resubmission.
        assert latest["state"] == "queued" and latest["details"]["phase"] == "recover"
    else:
        assert latest["state"] == "queued" and latest["error_code"] == "database_busy"
    assert "could not be parked" not in caplog.text


# Retries and source refusals --------------------------------------------------------------------------


def test_archive_preparation_waits_without_spending_failure_budget(queue, monkeypatch):
    service, remote = queue
    job = transferring(service)
    remote.archive_page = "<p>preparing</p>"
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    delays = []
    for _attempt in range(1, 6):
        download_step(service)
        latest = service.get(job["id"])
        assert latest["state"] == "queued" and latest["attempts"] == 0
        assert latest["error_code"] == "archive_preparing"
        delays.append(latest["retry_at"] - int(time.time()))
        due(service)
    assert delays == sorted(delays) and delays[0] >= 14
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["attempts"] == 0
    due(service)
    remote.archive_page = None
    run(service, 3)
    assert service.get(job["id"])["state"] == "done" and service.get(job["id"])["attempts"] == 0


def test_rate_limit_sets_a_site_cooldown_and_jobs_wait_without_spending_attempts(queue):
    service, remote = queue
    original = remote.__call__
    limited, refused = True, []

    def handler(request):
        if limited and request.url.path == "/archiver.php":
            refused.append(request)
            return httpx.Response(429, headers={"retry-after": "120"})
        return original(request)

    service.http.transport.handler = handler
    first = service.create(EH)
    second = service.create("https://e-hentai.org/g/101/abcdef/")
    download_step(service)
    download_step(service)
    for job in (first, second):
        latest = service.get(job["id"])
        assert latest["state"] == "queued" and latest["error_code"] == "source_rate_limited"
        assert latest["attempts"] == 0 and latest["retry_at"] >= time.time() + 100
    # The second job never reached the network: the cooldown answered for the site group.
    assert len(refused) == 1 and set(service.http.cooldowns()) == {"ehentai", "exhentai"}
    assert not download_step(service)
    limited = False
    end_halts(service.http)
    due(service)
    download_step(service)
    download_step(service)
    assert {service.get(job["id"])["state"] for job in (first, second)} == {"needs_approval"}


@pytest.mark.parametrize(
    "body,code",
    [
        ("This page requires you to log on.", "source_login_required"),
        ('<div id="cf-chl-widget-1"></div>', "verification_required"),
    ],
)
def test_credential_errors_wait_for_the_user_and_resume_after_new_cookies(queue, body, code):
    service, remote = queue
    job = service.create(EH)
    original = remote.__call__

    def refused(request):
        if request.url.path == "/archiver.php":
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return original(request)

    service.http.transport.handler = refused
    assert download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "verification_required" and latest["error_code"] == code
    assert not download_step(service)  # nothing retries on its own; the user updates the cookies first
    service.http.transport.handler = original
    assert action(service, job, "resume")["state"] == "queued"
    assert download_step(service) and service.get(job["id"])["state"] == "needs_approval"


@pytest.mark.parametrize(
    "page,code",
    [
        ("<p>This page requires you to log on.</p>", "source_login_required"),
        ("<html><img src='/img/sad panda.jpg' alt='Sad Panda'></html>", "source_login_required"),
        ("<html><img src='/img/kokomade.jpg'></html>", "source_login_required"),
        ('<script src="/cdn-cgi/challenge-platform/h/b/orchestrate"></script>', "verification_required"),
    ],
)
def test_archive_server_login_or_challenge_page_waits_for_the_user_without_paying_again(queue, page, code):
    service, remote = queue
    job = transferring(service)
    remote.archive_page = page
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "verification_required" and latest["error_code"] == code
    assert latest["details"]["phase"] == "transfer" and not download_step(service)
    remote.archive_page = None
    resumed = action(service, latest, "resume")
    assert resumed["state"] == "queued" and resumed["details"]["phase"] == "transfer"
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


# Transfer ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("response_kind", ["range", "ignored", "bad_range", "changed_etag"])
def test_partial_transfer_validates_range_and_does_not_append_full_response(queue, response_kind):
    service, remote = queue
    job = transferring(service)
    path = archive_path(service, job)
    path.write_bytes(remote.payload[:100])
    details = service.get(job["id"], private=True)["details"]
    details["etag"] = '"fixture-v1"'
    seed(service, job, details=details)
    seen = []

    def handler(req):
        seen.append(req)
        if response_kind == "ignored":
            return httpx.Response(200, content=remote.payload)
        headers = {
            "content-range": (
                f"bytes {101 if response_kind == 'bad_range' else 100}-"
                f"{len(remote.payload) - 1}/{len(remote.payload)}"
            ),
            "etag": '"changed"' if response_kind == "changed_etag" else '"fixture-v1"',
        }
        return httpx.Response(206, content=remote.payload[100:], headers=headers)

    service.http.client.close()
    service.http.client = httpx.Client(transport=httpx.MockTransport(handler))
    download_step(service)
    latest = service.get(job["id"])
    assert seen[0].headers["range"] == "bytes=100-" and seen[0].headers["if-range"] == '"fixture-v1"'
    if response_kind in {"bad_range", "changed_etag"}:
        assert latest["state"] == "error" and latest["error_code"] == "source_range_invalid"
        assert path.read_bytes() == remote.payload[:100]
        service.http.client.close()
        service.http.client = httpx.Client(transport=httpx.MockTransport(remote))
        action(service, job, "resume")
        run(service)
        assert service.get(job["id"])["state"] == "done"
        assert "range" not in transfers(remote)[-1].headers
        assert len(archiver_posts(remote)) == 1
    else:
        assert latest["details"]["phase"] == "import" and path.read_bytes() == remote.payload


@pytest.mark.parametrize("answer", ["declared", "streamed", "longer_than_declared"])
def test_oversized_archive_stops_is_deleted_and_resume_transfers_again(queue, files, answer):
    service, remote = queue
    job = transferring(service)
    if answer == "declared":
        service.settings = SourceSettings(download_limit_bytes=10)
    elif answer == "streamed":
        service.settings = SourceSettings(download_limit_bytes=10)
        remote.stream_archive = True
    else:
        remote.declared_length = 10
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["error_code"] == "download_too_large"
    assert not archive_path(service, job).exists() and files.library.search("") == []
    service.settings = SourceSettings()
    remote.stream_archive, remote.declared_length = False, None
    assert action(service, job, "resume")["details"]["phase"] == "transfer"
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1 and len(transfers(remote)) == 2


def test_transfer_shorter_than_declared_is_not_imported_and_resume_starts_it_over(queue, files):
    service, remote = queue
    job = transferring(service)
    remote.declared_length = len(remote.payload) + 10
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["error_code"] == "source_truncated"
    assert latest["details"]["phase"] == "transfer" and files.library.search("") == []
    remote.declared_length = None
    action(service, job, "resume")
    run(service)
    done = service.get(job["id"])
    assert done["state"] == "done"
    # The source ignored the range request for the rest, so the archive was fetched whole again.
    requests = transfers(remote)
    assert len(requests) == 2 and requests[1].headers["range"] == f"bytes={len(remote.payload)}-"


@pytest.mark.parametrize("status", [403, 404, 410])
def test_archive_server_refusing_the_link_gets_a_new_one_without_paying_again(queue, status):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    download_step(service)
    gone = refuse(
        remote, service, lambda request: request.url.host == "archive.example", httpx.Response(status)
    )
    download_step(service)
    latest = service.get(job["id"])
    assert latest["error_code"] == "archive_link_invalid"
    gone["on"] = False
    assert latest["state"] == "queued" and latest["details"]["phase"] == "recover"
    remote.reusable = True
    due(service)
    download_step(service)
    assert service.get(job["id"])["state"] == "queued"
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


@pytest.mark.parametrize(
    "failure,code",
    [
        (OSError(errno.ENOSPC, "No space left on device"), "insufficient_storage"),
        (OSError(errno.EDQUOT, "Disk quota exceeded"), "insufficient_storage"),
        (RootUnavailable("Library root is unavailable"), "root_unavailable"),
        (OSError(errno.EIO, "I/O error"), "download_local_error"),
    ],
)
def test_full_disk_and_offline_library_have_their_own_error_codes(queue, monkeypatch, failure, code):

    service, _ = queue
    job = transferring(service)

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(service, "_require_download_space", fail)
    download_step(service)
    latest = service.get(job["id"])
    assert (
        latest["state"] == ("queued" if code in {"root_unavailable", "insufficient_storage"} else "error")
        and latest["error_code"] == code
    )
    assert latest["details"]["phase"] == "transfer"


def test_cancel_active_transfer_cleans_up_and_no_publish(queue, files):
    service, remote = queue
    job = transferring(service)

    class CancelStream(httpx.SyncByteStream):
        def __iter__(self):
            current = service.get(job["id"])
            service.action(job["id"], "cancel", current["revision"])
            yield remote.payload

    service.http.client.close()
    service.http.client = httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, stream=CancelStream()))
    )
    download_step(service)
    assert service.get(job["id"])["state"] == "cancelled" and not archive_path(service, job).exists()
    assert files.library.search("") == []


def test_cancelled_network_error_finishes_cancellation(queue):
    service, _ = queue
    job = transferring(service)

    def handler(req):
        action(service, job, "cancel")
        raise httpx.ReadTimeout("fixture")

    service.http.client.close()
    service.http.client = httpx.Client(transport=httpx.MockTransport(handler))
    download_step(service)
    assert service.get(job["id"])["state"] == "cancelled"


def test_four_download_streams_per_site_run_concurrently_and_remaining_books_wait(queue, monkeypatch):
    service, remote = queue
    jobs = [ready(service, f"https://e-hentai.org/g/{100 + n}/abcdef/") for n in range(6)]
    release, four = threading.Event(), threading.Event()
    lock = threading.Lock()
    active, maximum = 0, 0
    original = service.http.transport.handler

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                if active == WORKERS:
                    four.set()
            try:
                assert release.wait(60)
                yield remote.payload
            finally:
                with lock:
                    active -= 1

    def handle(request):
        if request.url.host == "archive.example":
            return httpx.Response(
                200,
                stream=Body(),
                headers={"content-length": str(len(remote.payload)), "content-type": "application/zip"},
            )
        return original(request)

    service.http.transport.handler = handle
    # Every worker step announces itself, so the test waits for progress instead of polling the clock.
    progressed = threading.Condition()
    step = service.step

    def announced_step(*args):
        try:
            return step(*args)
        finally:
            with progressed:
                progressed.notify_all()

    monkeypatch.setattr(service, "step", announced_step)

    def states():
        return [
            (
                current["input_url"].split("/")[-3],
                current["state"],
                current["details"].get("phase"),
                current["error_code"],
                current["attempts"],
            )
            for current in (service.get(job["id"]) for job in jobs)
        ]

    def settled():
        # Stop early if any job leaves the happy path instead of waiting out a retry.
        current = states()
        return all(state == "done" for _, state, *_ in current) or any(code for *_, code, _ in current)

    service.start()
    try:
        for job in jobs:
            action(service, job, "approve")
        assert four.wait(5)

        def four_transfer_and_two_wait():
            # The meta workers may still be requesting the last archives when the fourth starts.
            summary = service.summary()
            return (summary["running"], summary["waiting"], summary["total"]) == (4, 2, 6)

        with progressed:
            assert progressed.wait_for(four_transfer_and_two_wait, timeout=10), service.summary()
        release.set()
        # Windows CI runners are several times slower than a laptop, so allow plenty of time.
        with progressed:
            assert progressed.wait_for(settled, timeout=60), states()
        assert all(state == "done" for _, state, *_ in states()), states()
        assert maximum == WORKERS
        assert service.summary()["completed"] == service.summary()["total"] == 6
    finally:
        release.set()


# Import verification ----------------------------------------------------------------------------------


@pytest.mark.parametrize("mismatch", [False, True])
def test_original_without_content_length_checks_unpacked_image_bytes(queue, mismatch):
    service, remote = queue
    remote.stream_archive = True
    if mismatch:
        remote.gallery_bytes += 1
    done = finish(service, ready(service))
    assert done["state"] == ("error" if mismatch else "done"), done
    if mismatch:
        assert done["error_code"] == "archive_size_mismatch"
    else:
        assert "size_verification" not in done["details"]
        assert "integrity_verified" not in done["details"]


@pytest.mark.parametrize("change", ["changed", "deleted"])
def test_archive_changed_before_import_is_transferred_again_through_the_same_link(queue, files, change):
    service, remote = queue
    job = transferring(service)
    download_step(service)
    assert service.get(job["id"])["details"]["phase"] == "import"
    path = archive_path(service, job)
    if change == "changed":
        path.write_bytes(b"changed on disk")
    else:
        path.unlink()
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "paused" and latest["error_code"] == "download_file_missing"
    assert latest["details"]["phase"] == "transfer" and files.library.search("") == []
    assert action(service, job, "resume")["details"]["phase"] == "transfer"
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1 and len(transfers(remote)) == 2


def test_streamed_hash_is_reused_and_cancellation_probe_reads_only_state(queue, monkeypatch):
    from mangacouch import downloads

    service, _ = queue
    job = transferring(service)
    monkeypatch.setattr(service, "get", lambda *a, **kw: pytest.fail("probe must not load the job"))
    assert not cancellation_probe(service, job)
    monkeypatch.undo()
    download_step(service)
    assert service.get(job["id"])["details"]["phase"] == "import"

    def rehash(*args, **kwargs):
        raise AssertionError("the archive hashed while streaming must not be read again")

    monkeypatch.setattr(downloads, "hash_file", rehash)
    download_step(service)
    assert service.get(job["id"])["state"] == "done"


# Cleanup races ----------------------------------------------------------------------------------------


def test_resume_waits_for_cancel_cleanup_and_stale_cleanup_spares_the_resumed_archive(queue, monkeypatch):
    service, _ = queue
    job = transferring(service)
    assert action(service, job, "pause")["state"] == "paused"
    path = archive_path(service, job)
    path.write_bytes(b"partial")
    held = HeldCleanup(service, job, monkeypatch)
    resumed = []
    canceller = threading.Thread(target=lambda: action(service, job, "cancel"))
    resumer = threading.Thread(target=lambda: resumed.append(action(service, job, "redownload")))
    canceller.start()
    try:
        assert held.entered.wait(5)
        resumer.start()
        assert held.waiting.wait(5)
        # The resume waits for the job's lock until the cancel finished its cleanup.
        assert not resumed
    finally:
        held.release.set()
        canceller.join(5)
        if resumer.ident is not None:
            resumer.join(5)
    assert resumed and resumed[0]["state"] == "queued"
    path.write_bytes(b"new download")
    # A cleanup that lost the race only deletes files of jobs still in the expected state.
    stale_cleanup(service, job, {"cancelled"})
    assert path.read_bytes() == b"new download"


def test_resubmitted_url_waits_for_previous_cancel_cleanup(queue, monkeypatch):
    service, remote = queue
    remote.cost = 0
    service.settings = service.settings.model_copy(update={"auto_free": True})
    job = service.create(EH)
    held = HeldCleanup(service, job, monkeypatch)
    created = []
    canceller = threading.Thread(target=lambda: action(service, job, "cancel"))
    creator = threading.Thread(target=lambda: created.append(service.create(EH)))
    canceller.start()
    try:
        assert held.entered.wait(5)
        creator.start()
        assert held.waiting.wait(5)
        assert not created
    finally:
        held.release.set()
        canceller.join(5)
        if creator.ident is not None:
            creator.join(5)
    assert created[0]["id"] == job["id"] and created[0]["outcome"] == "existing"
    run(service, 8)
    assert service.get(job["id"])["state"] == "cancelled"


# Persistence and workers ------------------------------------------------------------------------------


def test_snapshot_restores_tasks_paused_with_matching_remote_key(queue, files, tmp_path):
    service, _ = queue
    job = transferring(service)
    snapshot = create_snapshot(files, tmp_path / "task.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=files.root.path)
    new = Downloads(type(files)(restored.library, files.root.path))
    try:
        result = new.get(job["id"], private=True)
        assert result["state"] == "paused"
        assert new.vault.decrypt(result["remote_secret"])["url"].startswith(ARCHIVE)
    finally:
        new.close()


def test_queue_revision_and_duplicate_worker_lock(queue):
    service, _ = queue
    job = ready(service)
    with pytest.raises(RevisionConflict):
        service.action(job["id"], "approve", 1)
    service.start()
    # A second server on the same library must not recover imports or drop upload spools.
    database = service.files.library.path
    spool = database.parent / f"mangacouch-upload-{database.name}-active"
    spool.mkdir()
    with service.files.library.transaction() as db:
        db.execute("INSERT INTO import_job(id,state,kind) VALUES('in-flight','copying','copy')")
    other = Downloads(service.files)
    try:
        with pytest.raises(ValueError, match="Another download worker"):
            other.start()
    finally:
        other.close()
    assert spool.is_dir()
    with service.files.library.connection() as db:
        assert db.execute("SELECT state FROM import_job WHERE id='in-flight'").fetchone()[0] == "copying"


def test_listing_omits_comments_and_source_payloads(queue):
    service, _ = queue
    job = service.create(EH)
    download_step(service)
    with service.library.connection() as db:
        details = json.loads(db.execute("SELECT details_json FROM download_job").fetchone()[0])
    assert details["selected"]["comments"]
    item = service.list()["items"][0]
    assert item["id"] == job["id"] and item["attempts"] == 0
    for observation in (item["details"]["selected"],):
        assert not {"comments", "notes", "gdata", "cover_url"} & observation.keys()


# HTTP -------------------------------------------------------------------------------------------------


def test_download_routes_require_management_and_csrf_and_never_return_secrets(queue, files):
    service, _ = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        assert client.get("/api/downloads").status_code == 401
        _, key = auth.create_key("readonly", "reader")
        assert (
            client.get("/api/sources/settings", headers={"Authorization": "Bearer " + key}).status_code == 403
        )
        login(client)
        assert client.post("/api/downloads", json={"url": EH}).status_code == 403
        config = client.get("/api/sources/settings").json()
        result = client.put(
            "/api/sources/settings",
            params={"expected_revision": 0},
            json={
                "settings": config["settings"],
                "secrets": {"eh_cookie": "fixture-secret"},
            },
            headers=CSRF,
        )
        assert result.status_code == 200 and "fixture-secret" not in result.text
        assert result.json()["revision"] == 1
        assert service.http.secrets.get("eh_cookie") == "fixture-secret"
        result = client.post("/api/downloads", json={"url": EH}, headers=CSRF)
        assert result.status_code == 200 and result.json()["id"]


def test_batch_endpoint_accepts_manager_keys_and_reports_per_url_errors(queue, files):
    service, _ = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    _, key = auth.create_key("management", "extension")
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        result = client.post("/api/downloads/batch", json={"urls": [EH, "ftp://x", EH]})
        assert result.status_code == 200, result.text
        items = result.json()["items"]
        assert [item["url"] for item in items] == [EH, "ftp://x", EH]
        assert items[1] == {"url": "ftp://x", "job": None, "error": "invalid_url"}
        assert items[0]["job"]["id"] == items[2]["job"]["id"]
        assert client.post("/api/downloads/batch", json={"urls": []}).status_code == 422
        assert client.post("/api/downloads/batch", json={"urls": [EH] * 101}).status_code == 422


def test_speed_drops_to_zero_when_stalled_or_finished(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)
    service._update(job["id"], state="downloading")
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    service._progress(job["id"], 1024)
    clock[0] += 0.5
    service._progress(job["id"], 3072)
    # Progress is stored only every few seconds; listings show the live figure.
    assert service.get(job["id"])["bytes_per_second"] == 4096
    assert service.get(job["id"])["received"] == service.list()["items"][0]["received"] == 3072
    clock[0] += 5
    assert service.get(job["id"])["bytes_per_second"] == 0
    service._update(job["id"], state="paused")
    assert service.get(job["id"])["bytes_per_second"] == 0


def test_galleries_are_inspected_while_every_transfer_slot_of_their_site_is_taken(queue):
    service, _ = queue
    service.working.update({f"busy-{n}": "ehentai" for n in range(WORKERS)})
    job = service.create(EH)
    assert not service.step("transfer")  # transfer workers never inspect
    assert service.get(job["id"])["state"] == "queued"
    assert service.step("meta")
    assert service.get(job["id"])["state"] == "needs_approval"


@pytest.mark.parametrize("failing", ["link", "balance"])
def test_a_busy_database_after_the_paid_archive_request_keeps_its_link(queue, monkeypatch, failing):
    from mangacouch import downloads

    service, remote = queue
    monkeypatch.setattr(downloads, "BUSY_RETRY", 0)
    job = ready(service)
    action(service, job, "approve")
    update, share = service._update, service._share_balance
    refused, error = [], locked(service)

    def busy_once(identifier, **fields):
        if "remote_secret" in fields and not refused:
            refused.append(fields)
            raise error
        return update(identifier, **fields)

    def busy_share(*args):
        if archiver_posts(remote):
            refused.append(args)
            raise error
        return share(*args)

    if failing == "link":
        monkeypatch.setattr(service, "_update", busy_once)
    else:
        monkeypatch.setattr(service, "_share_balance", busy_share)
    assert download_step(service)
    monkeypatch.setattr(service, "_update", update)
    monkeypatch.setattr(service, "_share_balance", share)
    latest = service.get(job["id"])
    assert refused and latest["state"] == "queued" and latest["details"]["phase"] == "transfer"
    assert not latest["details"]["submission_uncertain"]
    run(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


def test_galleries_added_together_get_their_metadata_in_batches_of_25(queue):
    service, remote = queue
    jobs = [service.create(f"https://e-hentai.org/g/{100 + n}/abcdef/") for n in range(30)]
    while service.step("meta"):
        pass
    api = [request for request in remote.requests if request.url.path == "/api.php"]
    assert [len(json.loads(request.content)["gidlist"]) for request in api] == [25, 5]
    assert {service.get(job["id"])["state"] for job in jobs} == {"needs_approval"}


def test_meta_workers_neither_transfer_nor_take_transfer_slots(queue):
    service, _ = queue
    job = transferring(service)  # approved and requested: queued for the transfer
    assert service.get(job["id"])["details"]["phase"] == "transfer"
    assert not service.step("meta")
    # Jobs the meta workers hold do not count toward the site's WORKERS transfers.
    service.working.update({f"meta-{n}": "ehentai" for n in range(WORKERS)})
    service.meta_working.update(f"meta-{n}" for n in range(WORKERS))
    assert service.step("transfer")
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["details"]["phase"] == "import"


def test_workers_are_split_into_transfer_and_meta_pools(queue):
    service, _ = queue
    service.start()
    names = [thread.name for thread in service.threads]
    assert sum(name.startswith("mangacouch-download-") for name in names) == WORKERS
    assert sum(name.startswith("mangacouch-meta-") for name in names) == META_WORKERS
    assert "mangacouch-source-checks" in names


def test_each_site_works_on_at_most_four_downloads_at_once(queue, monkeypatch):
    """E-Hentai and ExHentai share the WORKERS archive transfer slots."""
    service, _ = queue
    assert WORKERS == 4 and META_WORKERS == 2
    eh = service.create(EH)
    ex = service.create("https://exhentai.org/g/101/abcdef/")
    for job in (eh, ex):
        seed(service, job, details={"phase": "transfer"})
    monkeypatch.setattr(service, "_transfer", lambda job: service._update(job["id"], state="needs_approval"))
    service.working.update({f"busy-{n}": "ehentai" for n in range(WORKERS)})
    assert not service.step("transfer")
    assert [service.get(job["id"])["state"] for job in (eh, ex)] == ["queued", "queued"]
    assert not service.step("transfer")

    def waiting():
        return [job["id"] for job in (eh, ex) if service.get(job["id"])["state"] == "queued"]

    service.working.pop("busy-0")
    assert service.step("transfer")
    assert len(waiting()) == 1  # one slot: one of the two
    assert service.step("transfer")
    assert waiting() == []
