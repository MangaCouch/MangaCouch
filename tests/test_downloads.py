from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    PASSCODE,
    action,
    login,
    ready,
)
from helpers import finish_download as finish
from source_fixtures import ARCHIVE, EH, NH

from mangacouch.auth import Auth
from mangacouch.database import RevisionConflict
from mangacouch.downloads import Downloads
from mangacouch.http import HTTPSettings, create_app
from mangacouch.models import PersonalState
from mangacouch.review import Review
from mangacouch.snapshots import create_snapshot, restore_snapshot
from mangacouch.source_config import SourceSettings


@pytest.mark.parametrize("url,entry", [(EH, "find_eh"), (NH, "nh_direct")])
def test_complete_download_is_durable_readable_and_deduplicated(queue, files, url, entry):
    service, remote = queue
    job = ready(service, url, entry)
    assert not any(
        r.method == "POST" and r.url.path in {"/archiver.php", "/api/v2/galleries/200/download"}
        for r in remote.requests
    )
    assert service.create(url, entry)["id"] == job["id"]
    done = finish(service, job)
    assert done["state"] == "done", done
    book = files.library.get_book(done["book_id"])
    assert book.content.page_count == 3 and files.page_bytes(book.id, 0).startswith(b"\x89PNG")
    assert book.review_state == "confirmed", Review(files).preview(book.id)
    assert not service._path(job["id"]).exists()
    assert "token=fixture" not in json.dumps(done)
    with files.library.connection() as db:
        secret = db.execute("SELECT remote_secret FROM download_job").fetchone()[0]
        assert "token=fixture" not in secret


def test_new_download_with_identical_bytes_preserves_existing_personal(queue, files, tmp_path):
    service, _ = queue
    book = files.import_file(tmp_path / "remote.zip").book
    personal = PersonalState(rating=4.5, resume_page=1, bookmarks=[1], completed=True)
    files.library.save_personal(book.id, personal, expected_revision=1)
    done = finish(service, ready(service))
    assert done["state"] == "done" and done["details"]["duplicate"]
    assert done["book_id"] == book.id and files.library.get_book(book.id).personal == personal


def test_nh_search_requires_explicit_choice_and_failure_does_not_fall_back(queue):
    service, remote = queue
    job = service.create(NH)
    service.step()
    choice = service.get(job["id"])
    assert choice["state"] == "needs_choice" and len(choice["details"]["candidates"]) == 1
    assert not any(r.url.path.endswith("/download") for r in remote.requests)
    action(service, job, "choose", url=NH)
    service.step()
    assert service.get(job["id"])["state"] == "needs_approval"
    action(service, job, "cancel")
    action(service, job, "resume")
    assert service.get(job["id"])["state"] == "queued"


def test_failed_search_remains_visible_not_empty_candidates(queue):
    service, remote = queue
    remote.search_error = True
    job = service.create(NH)
    service.step()
    state = service.get(job["id"])
    # A 5xx search page is transient: the job waits and retries by itself.
    assert state["state"] == "queued" and state["error_code"] == "source_unavailable"
    assert state["attempts"] == 1 and state["retry_at"] > time.time()
    remote.search_error = False
    remote.search_links = []
    original = remote.__call__

    def unrecognized(request):
        if request.url.path == "/" and request.url.host == "e-hentai.org":
            return httpx.Response(200, text="<html>maintenance</html>")
        return original(request)

    service.http.transport.handler = unrecognized
    service._update(job["id"], retry_at=0)
    service.step()
    state = service.get(job["id"])
    assert state["state"] == "needs_choice" and state["error_code"] == "source_search_failed"
    assert not state["details"].get("candidates") and state["attempts"] == 0
    assert not any(r.url.path.endswith("/download") for r in remote.requests)


def test_one_removed_candidate_does_not_drop_the_search_and_metadata_is_one_request(queue):
    service, remote = queue
    remote.search_links = [f"https://e-hentai.org/g/{n}/abcdef/" for n in (101, 102, 103)]
    remote.removed = {102}
    job = service.create(NH)
    service.step()
    choice = service.get(job["id"])
    assert choice["state"] == "needs_choice"
    assert [c["metadata"]["source"]["gallery_id"] for c in choice["details"]["candidates"]] == [101, 103]
    assert len([r for r in remote.requests if r.url.path == "/api.php"]) == 1


def test_change_from_direct_nh_quote_to_eh_search_requeues_without_submitting(queue):
    service, remote = queue
    job = ready(service, NH, "nh_direct")
    assert service.create(NH, "nh_direct")["state"] == "needs_approval"
    changed = service.create(NH, "find_eh")
    assert changed["id"] == job["id"] and changed["state"] == "queued"
    service.step()
    assert service.get(job["id"])["state"] == "needs_choice"
    assert not any(r.url.path.endswith("/download") for r in remote.requests)


def test_price_change_requires_new_approval_and_original_is_default(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve")
    remote.cost = 99
    service.step()
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval" and latest["details"]["quote"]["original_gp"] == 99
    assert not any(r.method == "POST" and r.url.path == "/archiver.php" for r in remote.requests)
    action(service, job, "approve")
    service.step()
    submitted = [r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]
    assert len(submitted) == 1 and b"dltype=org" in submitted[0].content


def test_unknown_remote_post_is_never_replayed_after_resume_or_restart(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    service.step()
    assert service.get(job["id"])["state"] == "needs_attention"
    service._update(job["id"], retry_at=0)
    for _ in range(2):
        action(service, job, "recover")
        service.step()
    posts = [r for r in remote.requests if r.method == "POST" and r.url.path != "/api.php"]
    assert len(posts) == 1
    # Restart sees the same persistent uncertainty, never a fresh submit.
    service.start()
    time.sleep(0.05)
    service.close()
    assert service.get(job["id"])["state"] == "needs_attention"


def test_nh_link_request_failure_retries_automatically_because_links_are_free(queue):
    service, remote = queue
    job = ready(service, NH, "nh_direct")
    remote.download_error = True
    action(service, job, "approve")
    service.step()
    retrying = service.get(job["id"])
    assert retrying["state"] == "queued" and retrying["error_code"] == "source_network"
    assert retrying["attempts"] == 1 and not retrying["details"].get("submission_uncertain")
    remote.download_error = False
    service._update(job["id"], retry_at=0)
    for _ in range(3):
        service.step()
    assert service.get(job["id"])["state"] == "done"
    assert len([r for r in remote.requests if r.url.path.endswith("/download")]) == 2


def test_nh_needs_attention_recovers_by_requesting_a_new_link(queue):
    service, remote = queue
    job = ready(service, NH, "nh_direct")
    action(service, job, "approve")
    current = service.get(job["id"], private=True)
    current["details"].update(phase="uncertain", submission_uncertain=True)
    service._update(job["id"], state="needs_attention", details=current["details"])
    action(service, job, "recover")
    for _ in range(4):
        service.step()
    assert service.get(job["id"])["state"] == "done"


def test_transient_errors_retry_with_backoff_until_the_attempt_budget_is_spent(queue):
    from mangacouch.downloads import MAX_ATTEMPTS

    service, remote = queue
    job = transferring(service)
    original = remote.__call__

    def preparing(request):
        if request.url.host == "archive.example":
            return httpx.Response(200, text="<p>preparing</p>", headers={"content-type": "text/html"})
        return original(request)

    service.http.transport.handler = preparing
    delays = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        service.step()
        latest = service.get(job["id"])
        assert latest["state"] == "queued" and latest["attempts"] == attempt
        assert latest["error_code"] == "archive_preparing"
        delays.append(latest["retry_at"] - int(time.time()))
        service._update(job["id"], retry_at=0)
    assert delays == sorted(delays) and delays[0] >= 14
    service.step()
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["attempts"] == MAX_ATTEMPTS
    action(service, job, "resume")
    service.http.transport.handler = original
    for _ in range(3):
        service.step()
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
    service.step()
    service.step()
    for job in (first, second):
        latest = service.get(job["id"])
        assert latest["state"] == "queued" and latest["error_code"] == "source_rate_limited"
        assert latest["attempts"] == 0 and latest["retry_at"] >= time.time() + 100
    # The second job never reached the network: the cooldown answered for the site.
    assert len(refused) == 1 and set(service.http.cooldowns()) == {"ehentai"}
    assert not service.step()
    limited = False
    service.http.cooldown.clear()
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET retry_at=0")
    service.step()
    service.step()
    assert {service.get(job["id"])["state"] for job in (first, second)} == {"needs_approval"}


def test_batch_creation_validates_collapses_and_runs_like_single_jobs(queue):
    service, remote = queue
    items = service.create_many(
        [EH, "not a url", NH, "https://example.com/g/1/", EH.rstrip("/"), NH + "?ref=x"], "nh_direct"
    )
    assert [item["error"] for item in items] == [None, "invalid_url", None, "unsupported_source", None, None]
    assert items[0]["job"]["id"] == items[4]["job"]["id"]
    assert items[2]["job"]["id"] == items[5]["job"]["id"]
    assert items[2]["job"]["details"]["entry"] == "nh_direct"
    assert items[0]["job"]["details"]["entry"] == "find_eh"
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM download_job").fetchone()[0] == 2
    service.step()
    service.step()
    assert {service.get(item["job"]["id"])["state"] for item in items if item["job"]} == {"needs_approval"}
    searched = service.create_many([NH], "find_eh")[0]["job"]
    service.step()
    assert service.get(searched["id"])["state"] == "needs_choice"


def test_batch_endpoint_accepts_downloads_keys_and_reports_per_url_errors(queue, files):
    service, _ = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    _, key = auth.create_key("downloads", "extension")
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    ) as client:
        result = client.post("/api/downloads/batch", json={"urls": [EH, "ftp://x", EH], "entry": "find_eh"})
        assert result.status_code == 200, result.text
        items = result.json()["items"]
        assert [item["url"] for item in items] == [EH, "ftp://x", EH]
        assert items[1] == {"url": "ftp://x", "job": None, "error": "invalid_url"}
        assert items[0]["job"]["id"] == items[2]["job"]["id"]
        assert client.post("/api/downloads/batch", json={"urls": []}).status_code == 422
        assert client.post("/api/downloads/batch", json={"urls": [EH] * 101}).status_code == 422


def test_stale_cancel_cleanup_never_deletes_a_resumed_archive(queue):
    service, _ = queue
    job = transferring(service)
    service._update(job["id"], state="paused")
    path = service._path(job["id"])
    path.write_bytes(b"partial")
    entered, release = threading.Event(), threading.Event()
    remove = service._remove_files
    resumed = []

    def slow_remove(identifier):
        entered.set()
        assert release.wait(5)
        remove(identifier)

    service._remove_files = slow_remove
    canceller = threading.Thread(target=lambda: action(service, job, "cancel"))
    canceller.start()
    assert entered.wait(5)
    resumer = threading.Thread(target=lambda: resumed.append(action(service, job, "resume")))
    resumer.start()
    time.sleep(0.1)
    # The resume waits until the cancel finished its cleanup instead of interleaving with it.
    assert not resumed
    release.set()
    canceller.join(5)
    resumer.join(5)
    service._remove_files = remove
    assert resumed and resumed[0]["state"] == "queued"
    path.write_bytes(b"new download")
    # A cleanup that lost the race only deletes files of jobs still in the expected state.
    service._cleanup(job["id"], {"cancelled"})
    assert path.read_bytes() == b"new download"


def test_resubmitted_url_waits_for_previous_cancel_cleanup(queue):
    service, _ = queue
    service.settings = service.settings.model_copy(update={"auto_free": True})
    job = service.create(NH, "nh_direct")
    entered, release = threading.Event(), threading.Event()
    remove = service._remove_files
    created = []

    def slow_remove(identifier):
        entered.set()
        assert release.wait(5)
        remove(identifier)

    service._remove_files = slow_remove
    canceller = threading.Thread(target=lambda: action(service, job, "cancel"))
    creator = threading.Thread(target=lambda: created.append(service.create(NH, "nh_direct")))
    canceller.start()
    try:
        assert entered.wait(5)
        creator.start()
        creator.join(0.1)
        assert not created
    finally:
        release.set()
        canceller.join(5)
        if creator.ident is not None:
            creator.join(5)
        service._remove_files = remove
    assert created[0]["id"] == job["id"]
    for _ in range(8):
        if not service.step():
            break
    done = service.get(created[0]["id"])
    assert done["state"] == "done"


def test_eh_unknown_result_can_recover_existing_archive_with_get(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    service.step()
    remote.reusable = True
    service._update(job["id"], retry_at=0)
    action(service, job, "recover")
    service.step()
    assert service.get(job["id"])["details"]["phase"] == "transfer"
    service.step()
    service.step()
    assert service.get(job["id"])["state"] == "done"
    assert len([r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]) == 1


def transferring(service):
    job = ready(service)
    action(service, job, "approve")
    service.step()
    return job


@pytest.mark.parametrize("response_kind", ["range", "ignored", "bad_range", "changed_etag"])
def test_partial_transfer_validates_range_and_does_not_append_full_response(queue, response_kind):
    service, remote = queue
    job = transferring(service)
    path = service._path(job["id"])
    path.write_bytes(remote.payload[:100])
    details = service.get(job["id"])["details"]
    details["etag"] = '"fixture-v1"'
    service._update(job["id"], details=details)
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
    service.step()
    latest = service.get(job["id"])
    assert seen[0].headers["range"] == "bytes=100-" and seen[0].headers["if-range"] == '"fixture-v1"'
    if response_kind in {"bad_range", "changed_etag"}:
        assert latest["state"] == "error" and latest["error_code"] == "source_range_invalid"
        assert path.read_bytes() == remote.payload[:100]
    else:
        assert latest["details"]["phase"] == "import" and path.read_bytes() == remote.payload


def test_actual_bytes_limit_and_invalid_archive_never_publish(queue, files):
    service, remote = queue
    job = transferring(service)
    service.settings = SourceSettings(download_limit_bytes=10)
    service.step()
    latest = service.get(job["id"])
    assert latest["error_code"] == "download_too_large" and not service._path(job["id"]).exists()
    assert files.library.search("") == []
    service.settings = SourceSettings()
    action(service, job, "resume")
    remote.payload = b"not an archive"
    service.step()
    service.step()
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["book_id"] is None


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
    service.step()
    assert service.get(job["id"])["state"] == "cancelled" and not service._path(job["id"]).exists()
    assert files.library.search("") == []


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


def test_m2_routes_require_management_and_csrf_and_never_return_secrets(queue, files):
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
            json={
                "expected_revision": 0,
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


def test_failed_recovery_keeps_unknown_post_protection(queue):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    service.step()
    service._update(job["id"], retry_at=0)
    action(service, job, "recover")
    original = service.sources.quote
    from mangacouch.source_http import SourceError

    def failed(*args, **kwargs):
        raise SourceError("source_network")

    service.sources.quote = failed
    service.step()
    assert service.get(job["id"])["state"] == "needs_attention"
    service.sources.quote = original
    action(service, job, "resume")
    service.step()
    assert service.get(job["id"])["state"] == "needs_attention"
    assert len([r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]) == 1


def test_cancelled_network_error_finishes_cancellation(queue):
    service, _ = queue
    job = transferring(service)

    def handler(req):
        action(service, job, "cancel")
        raise httpx.ReadTimeout("fixture")

    service.http.client.close()
    service.http.client = httpx.Client(transport=httpx.MockTransport(handler))
    service.step()
    assert service.get(job["id"])["state"] == "cancelled"


def test_expired_nh_link_recovers_with_a_fresh_free_link(queue):
    service, remote = queue
    job = ready(service, NH, "nh_direct")
    action(service, job, "approve")
    service.step()
    service._update(job["id"], remote_secret=service.vault.encrypt({"url": ARCHIVE, "expires_at": 1}))
    service.step()
    assert service.get(job["id"])["state"] == "needs_attention"
    action(service, job, "recover")
    for _ in range(4):
        service.step()
    assert service.get(job["id"])["state"] == "done"
    assert len([r for r in remote.requests if r.url.path.endswith("/download")]) == 2


def test_unknown_submission_snapshot_stays_blocked_after_recovery_phase(queue, files, tmp_path):
    service, remote = queue
    job = ready(service)
    remote.download_error = True
    action(service, job, "approve")
    service.step()
    service._update(job["id"], retry_at=0)
    action(service, job, "recover")
    snapshot = create_snapshot(files, tmp_path / "unknown.zip")
    restored = restore_snapshot(snapshot, tmp_path / "unknown-restored", root=files.root.path)
    new = Downloads(type(files)(restored.library, files.root.path))
    try:
        assert new.get(job["id"])["state"] == "needs_attention"
    finally:
        new.close()


def test_download_size_does_not_inherit_small_upload_limit(queue, files):
    from dataclasses import replace

    service, _ = queue
    files.limits = replace(files.limits, max_archive_bytes=1)
    done = finish(service, ready(service))
    assert done["state"] == "done"


def test_restart_pauses_previously_queued_work(queue):
    service, _ = queue
    job = service.create(EH)
    service.start()
    assert service.get(job["id"])["state"] == "paused"


def test_download_progress_does_not_invalidate_actions(queue):
    service, _ = queue
    job = ready(service)
    service._update(job["id"], state="downloading")
    job = service.get(job["id"])
    service._update(job["id"], received=1024)
    assert service.get(job["id"])["revision"] == job["revision"]
    paused = service.action(job["id"], "pause", job["revision"])
    assert paused["state"] == "pausing"
    assert action(service, paused, "cancel")["state"] == "cancelling"


def test_readding_paused_download_preserves_resume_state(queue):
    service, _ = queue
    job = ready(service)
    service._update(job["id"], state="paused", received=123, remote_secret="opaque")
    before = service.get(job["id"], private=True)
    service.create(job["input_url"])
    assert service.get(job["id"], private=True) == before


@pytest.mark.parametrize("uncertain", [False, True])
def test_download_unexpected_failure_logs_and_preserves_submission_uncertainty(
    queue, monkeypatch, caplog, uncertain
):
    service, _ = queue
    job = service.create(EH, "find_eh")
    if uncertain:
        current = service.get(job["id"], private=True)
        current["details"]["submission_uncertain"] = True
        service._update(job["id"], details=current["details"])

    def fail(*args, **kwargs):
        raise RuntimeError("injected download failure")

    monkeypatch.setattr(service, "_inspect", fail)
    assert service.step()
    result = service.get(job["id"])
    assert result["state"] == ("needs_attention" if uncertain else "error")
    assert result["error_code"] == "download_local_error" and not service.step()
    record = next(record for record in caplog.records if job["id"] in record.message)
    assert record.exc_info and "injected download failure" in caplog.text


@pytest.mark.parametrize("mismatch", [False, True])
def test_original_without_content_length_checks_unpacked_image_bytes(queue, mismatch):
    service, remote = queue
    original = service.http.transport.handler

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            yield remote.payload

    def handle(request):
        if request.url.host == "archive.example":
            return httpx.Response(200, stream=Body(), headers={"content-type": "application/zip"})
        return original(request)

    service.http.transport.handler = handle
    if mismatch:
        remote.gallery_bytes += 1
    done = finish(service, ready(service))
    assert done["state"] == ("error" if mismatch else "done"), done
    if mismatch:
        assert done["error_code"] == "archive_size_mismatch"
    else:
        assert done["details"]["size_verification"] == "images"
        assert done["details"]["integrity_verified"]


def test_four_download_streams_run_concurrently_and_remaining_books_wait(queue):
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
                if active == 4:
                    four.set()
            try:
                assert release.wait(10)
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
    service.start()
    try:
        for job in jobs:
            action(service, job, "approve")
        assert four.wait(5)
        summary = service.summary()
        assert summary["running"] == 4 and summary["waiting"] == 2 and summary["total"] == 6
        release.set()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(service.get(job["id"])["state"] != "done" for job in jobs):
            time.sleep(0.02)
        assert all(service.get(job["id"])["state"] == "done" for job in jobs), [
            service.get(job["id"]) for job in jobs
        ]
        assert maximum == 4
        assert service.summary()["completed"] == service.summary()["total"] == 6
    finally:
        release.set()


def test_streamed_hash_is_reused_and_cancellation_probe_reads_only_state(queue, monkeypatch):
    from mangacouch import downloads

    service, _ = queue
    job = transferring(service)
    monkeypatch.setattr(service, "get", lambda *a, **kw: pytest.fail("probe must not load the job"))
    assert not service._cancelled(job["id"])
    monkeypatch.undo()
    service.step()
    assert service.get(job["id"])["details"]["phase"] == "import"

    def rehash(*args, **kwargs):
        raise AssertionError("the archive hashed while streaming must not be read again")

    monkeypatch.setattr(downloads, "hash_file", rehash)
    service.step()
    assert service.get(job["id"])["state"] == "done"


def test_listing_omits_comments_and_source_payloads(queue):
    service, _ = queue
    job = service.create(NH)
    service.step()
    with service.library.connection() as db:
        details = json.loads(db.execute("SELECT details_json FROM download_job").fetchone()[0])
    assert details["selected"]["comments"] and details["original"]["comments"]
    item = service.list()["items"][0]
    assert item["id"] == job["id"] and item["attempts"] == 0
    for observation in (item["details"]["selected"], item["details"]["original"]):
        assert not {"comments", "notes", "gdata", "cover_url"} & observation.keys()
