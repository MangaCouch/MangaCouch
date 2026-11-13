from __future__ import annotations

import time
from contextlib import contextmanager
from types import SimpleNamespace

import httpx
import pytest
from helpers import action, download_step
from source_fixtures import EH
from test_downloads import due, transferring

from mangacouch.downloads import MAX_FAILURES, STORAGE_RECHECK
from mangacouch.source_http import SourceError


def test_bulk_enqueue_commits_once_and_wakes_after_commit(queue, monkeypatch):
    service, _ = queue
    transactions, wakes = [], []
    transaction = service.library.transaction

    @contextmanager
    def counted(**kwargs):
        transactions.append(1)
        with transaction(**kwargs) as db:
            yield db

    def wake():
        with service.library.connection() as db:
            wakes.append(db.execute("SELECT count(*) FROM download_job").fetchone()[0])

    monkeypatch.setattr(service.library, "transaction", counted)
    monkeypatch.setattr(service.wake, "set", wake)
    urls = [f"https://e-hentai.org/g/{i}/abc/" for i in range(1, 99)]
    result = service.create_many([*urls, "https://exhentai.org/g/1/abc/", "invalid"])
    assert len(transactions) == 1 and wakes == [98]
    assert result[0]["job"]["id"] == result[-2]["job"]["id"]
    assert result[-1]["error"] == "invalid_url"
    service.create_many(urls)
    assert len(transactions) == 2 and wakes == [98]


def test_bulk_failure_rolls_back_all_rows_and_does_not_wake(queue, monkeypatch):
    service, _ = queue
    wakes = []
    monkeypatch.setattr(service.wake, "set", lambda: wakes.append(1))
    with service.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER fail_batch BEFORE INSERT ON download_job "
            "WHEN NEW.input_url LIKE '%/2/%' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(Exception, match="injected"):
        service.create_many(["https://e-hentai.org/g/1/a/", "https://e-hentai.org/g/2/b/"])
    assert service.list()["total"] == 0 and not wakes


def test_numbered_pages_are_stable_on_new_arrivals_and_clamp_after_deletions(queue):
    service, _ = queue
    jobs = service.create_many([f"https://e-hentai.org/g/{i}/abc/" for i in range(1, 42)])
    first = service.list(page=1, scope="active")
    service.create("https://e-hentai.org/g/999/abc/")
    second = service.list(page=2, scope="active", through=first["through"])
    last = service.list(page=999, scope="active", through=first["through"])
    assert first["page"] == 1 and second["page"] == 2 and last["page"] == 3
    assert len(last["items"]) == 1
    ids = [job["id"] for page in (first, second, last) for job in page["items"]]
    assert len(ids) == len(set(ids)) == 41
    target = jobs[0]["job"]["id"]
    focused = service.list(page=1, scope="active", focus=target)
    assert focused["page"] == 3 and any(job["id"] == target for job in focused["items"])
    service._update(target, state="error")
    service.delete(target)
    result = service.list(page=3, scope="active", through=first["through"])
    assert result["page"] == 2 and len(result["items"]) == 20


@pytest.mark.parametrize("code,status", [("source_network", None), ("source_unavailable", 503)])
def test_network_failures_have_a_budget_and_manual_retry_resets_it(queue, monkeypatch, code, status):
    service, _ = queue
    job = transferring(service)

    def fail(*args):
        raise SourceError(code, 30, status=status)

    monkeypatch.setattr(service, "_receive", fail)
    current = job
    for attempt in range(1, MAX_FAILURES + 1):
        assert download_step(service)
        current = service.get(job["id"])
        assert current["attempts"] == attempt
        assert current["state"] == ("error" if attempt == MAX_FAILURES else "queued")
        due(service)
    assert not download_step(service)
    assert action(service, current, "resume")["attempts"] == 0


def test_same_bad_eh_link_cannot_cycle_forever_through_recovery(queue):
    service, remote = queue
    job = transferring(service)
    remote.reusable = True
    original = remote.__call__
    service.http.transport.handler = lambda request: (
        httpx.Response(403) if request.url.host == "archive.example" else original(request)
    )
    for _ in range(MAX_FAILURES * 3):
        due(service)
        if not download_step(service):
            break
    latest = service.get(job["id"])
    assert latest["state"] == "error"
    assert latest["attempts"] == MAX_FAILURES
    assert latest["details"]["phase"] == "recover"
    assert latest["error_code"] == "archive_link_invalid"


def test_low_space_only_holds_the_transfer_that_needs_it(queue, monkeypatch):
    from mangacouch import downloads

    service, remote = queue
    job = service.create(EH)
    paused = service.create("https://e-hentai.org/g/999/abc/")
    action(service, paused, "pause")
    free = [service.settings.download_reserve_bytes - 1]
    monkeypatch.setattr(downloads.shutil, "disk_usage", lambda path: SimpleNamespace(free=free[0]))
    assert service.step("meta")
    assert service.get(job["id"])["state"] == "needs_approval"
    action(service, job, "approve")
    assert service.step("meta")
    assert service.step("transfer")
    assert service.summary()["storage_wait_until"] > time.time()
    free[0] += 2_000_000_000
    due(service)
    assert service.step("transfer")
    assert service.get(paused["id"])["state"] == "paused"
    assert service.summary()["storage_wait_until"] == 0


def test_transfer_space_wait_keeps_partial_file_and_progress(queue, monkeypatch):
    from mangacouch import downloads

    service, remote = queue
    job = transferring(service)
    monkeypatch.setattr(downloads, "CHUNK_SIZE", 32)
    monkeypatch.setattr(downloads, "SPACE_CHECK_BYTES", 32)
    real_check = service._require_download_space
    checks = []

    def check(path, size):
        checks.append(size)
        if len(checks) == 4:
            raise SourceError("insufficient_storage", STORAGE_RECHECK)
        real_check(path, size)

    monkeypatch.setattr(service, "_require_download_space", check)
    assert download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["error_code"] == "insufficient_storage"
    assert service._path(job["id"]).read_bytes() == remote.payload[:64]
    assert latest["received"] == 64
    assert service.get(job["id"], private=True)["details"]["etag"]


def test_live_progress_does_not_write_periodically(queue, monkeypatch):
    from mangacouch import downloads

    service, _ = queue
    job = transferring(service)
    ticks = iter(range(10000))
    monkeypatch.setattr(downloads, "time", SimpleNamespace(time=time.time, monotonic=lambda: next(ticks)))
    monkeypatch.setattr(downloads, "CHUNK_SIZE", 32)
    writes = []
    update = service._update

    def observed(identifier, **kwargs):
        writes.append(kwargs)
        return update(identifier, **kwargs)

    monkeypatch.setattr(service, "_update", observed)
    assert download_step(service)
    assert not any(set(write) == {"received"} for write in writes)
    latest = service.get(job["id"])
    assert latest["details"]["phase"] == "import"
    assert latest["received"] == service._path(job["id"]).stat().st_size


@pytest.mark.parametrize(
    "url,body,code",
    [
        (EH, "<h1>Gallery Not Available</h1>", "gallery_unavailable"),
        (EH, "not found", "source_unavailable"),
        ("https://archive.example/file.zip", "Gallery Not Available", "source_unavailable"),
    ],
)
def test_only_eh_gallery_removal_response_gets_the_removal_code(queue, url, body, code):
    service, _ = queue
    service.http.transport.handler = lambda request: httpx.Response(404, text=body)
    with pytest.raises(SourceError) as caught:
        service.http.read("GET", url)
    assert caught.value.code == code
