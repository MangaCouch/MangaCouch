from __future__ import annotations

import json
import os
import socket
import threading
import time
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import socksio
from fastapi.testclient import TestClient
from helpers import (
    CSRF,
    ORIGIN,
    PASSCODE,
    content,
    login,
    make_zip,
    native_file,
    register_book,
    save_metadata,
)
from source_fixtures import EH, end_halts

from mangacouch import source_checks as source_checks_module
from mangacouch.auth import Auth
from mangacouch.database import RevisionConflict
from mangacouch.errors import InvalidInput
from mangacouch.models import Metadata, PersonalState, Source
from mangacouch.online import OnlineMetadata
from mangacouch.review import Review
from mangacouch.snapshots import create_snapshot, restore_snapshot
from mangacouch.source_adapters import Sources, parse_quote
from mangacouch.source_checks import SourceChecks, enqueue_check
from mangacouch.source_config import SourceSettings, SourceVault
from mangacouch.source_http import SourceError, SourceHTTP, ban_seconds, gallery_url, public_url


@pytest.mark.parametrize(
    "url",
    [
        "https://e-hentai.org.evil/g/1/ab/",
        "https://user@e-hentai.org/g/1/ab/",
        "http://127.0.0.1/g/1/",
        "https://e-hentai.org:22/g/1/ab/",
        "https://e-hentai.org/g/1/not-hex/",
    ],
)
def test_gallery_identity_rejects_lookalikes(url):
    with pytest.raises(ValueError):
        gallery_url(url)


def test_remote_url_rejects_private_redirect_target(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(SourceError, match="source_private_address"):
        public_url("https://public.example/archive")


def test_credentials_are_scoped_and_response_cookies_never_persist():
    seen = []

    def handler(req):
        seen.append(req)
        if len(seen) == 1:
            return httpx.Response(
                302,
                headers={
                    "location": "https://cdn.example/file",
                    "set-cookie": "leaked=secret; Domain=.example; Path=/",
                },
            )
        return httpx.Response(200, content=b"ok", headers={"set-cookie": "remember=secret"})

    http = SourceHTTP(
        SourceSettings(),
        {"eh_cookie": "eh=secret"},
        transport=httpx.MockTransport(handler),
    )
    assert http.read("GET", EH) == b"ok"
    assert seen[0].headers["cookie"] == "eh=secret; nw=1"
    assert "cookie" not in seen[1].headers and "authorization" not in seen[1].headers
    http.read("GET", "https://cdn.example/file")
    assert "cookie" not in seen[-1].headers
    http.close()


def test_gallery_pages_skip_the_content_warning_without_overriding_the_users_choice():
    seen = []

    def handler(req):
        seen.append(req.headers.get("cookie"))
        return httpx.Response(200, content=b"ok")

    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler))
    try:
        http.read("GET", EH)
        http.read("GET", "https://exhentai.org/g/100/abcdef/?hc=1")
        http.read("GET", "https://e-hentai.org/archiver.php?gid=100&token=abcdef")
        http.read("GET", "https://e-hentai.org/g.example/")
        http.secrets["eh_cookie"] = "member=1; nw=0"
        http.read("GET", EH)
    finally:
        http.close()
    assert seen == ["nw=1", "nw=1", None, None, "member=1; nw=0"]


def test_http_limits_cancellation_and_challenge():
    for response, code in [
        (httpx.Response(200, content=b"x" * 11), "source_response_too_large"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}), "verification_required"),
        (httpx.Response(429, headers={"retry-after": "10"}), "source_rate_limited"),
    ]:
        client = SourceHTTP(
            SourceSettings(), {}, transport=httpx.MockTransport(lambda req, value=response: value)
        )
        with pytest.raises(SourceError, match=code):
            client.read("GET", EH, limit=10)
        with pytest.raises(InterruptedError):
            client.read("GET", EH, cancelled=lambda: True)
        client.close()


def test_socks_proxy_failure_becomes_retryable_source_error():
    def denied(request):
        raise socksio.ProtocolError("Connection not allowed by ruleset")

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(denied))
    try:
        with pytest.raises(SourceError, match="source_network") as caught:
            client.read("GET", EH)
        assert caught.value.retry_after == 30
    finally:
        client.close()


def test_source_contracts_comments_and_quotes(sources, remote):
    eh = sources.fetch(gallery_url(EH))
    assert eh["comments"][0]["score"] == 3 and eh["notes"]["comments_complete"]
    assert not eh["metadata"].get("comments") and not eh["metadata"].get("source_notes")
    assert sources.quote(gallery_url(EH))[0] == {"original_gp": 25, "resample_gp": 0, "balance_gp": 5000}
    assert parse_quote("unrecognized HTML")["original_gp"] is None
    remote.comments_error = True
    assert sources.fetch(gallery_url(EH))["comments_error"] == "source_login_required"


def test_unchanged_periodic_refresh_keeps_the_owner_review_decision(files, tmp_path, sources, remote):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    online = OnlineMetadata(files, sources)
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    with files.library.transaction() as db:
        db.execute(
            "UPDATE metadata_import SET decisions_json=? WHERE book_id=?",
            ('{"online:ehentai":"skipped"}', book.id),
        )
    revision = files.library.get_book(book.id).metadata_revision
    remote.comment_body = "Only the comments changed"
    online.refresh(book.id, gallery_url(EH), revision)
    assert Review(files).preview(book.id)["candidates"]["online:ehentai"]["decision"] == "skipped"
    assert files.library.get_book(book.id).metadata_revision == revision
    remote.title = "A different remote title"
    online.refresh(book.id, gallery_url(EH), revision)
    assert Review(files).preview(book.id)["candidates"]["online:ehentai"]["decision"] is None


def test_source_vault_encrypts_cas_and_restores_key(files, tmp_path):
    vault = SourceVault(files.library)
    settings = SourceSettings()
    assert (
        vault.save(
            settings, {"eh_cookie": "secret-fixture", "proxy": "http://user:pass@proxy.example:8080"}, 0
        )
        == 1
    )
    assert "secret-fixture" not in json.dumps(vault.public()) and vault.public()["revision"] == 1
    with files.library.connection() as db:
        assert "secret-fixture" not in db.execute("SELECT secrets FROM source_config").fetchone()[0]
    with pytest.raises(RevisionConflict):
        vault.save(settings, {}, 0)
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=files.root.path)
    loaded = SourceVault(restored.library)
    assert loaded.load()[1]["eh_cookie"] == "secret-fixture"
    if os.name != "nt":
        assert loaded.path.stat().st_mode & 0o077 == 0
    vault.save(settings, {"eh_cookie": ""}, 1)
    assert not vault.public()["configured"]["eh_cookie"]
    with zipfile.ZipFile(snapshot) as z:
        assert "sources.key" in z.namelist()


def test_different_gallery_never_autofills_current_book_or_imports_its_comments(files, tmp_path, sources):
    book = files.import_file(make_zip(tmp_path / "different.zip")).book
    current = book.metadata.model_copy(deep=True)
    current.source = Source(site="ehentai", gallery_id=999, gallery_token="aaaa")
    current.title = "Existing gallery"
    book = save_metadata(files.library, book.id, current, expected_revision=book.metadata_revision)
    OnlineMetadata(files, sources).refresh(book.id, gallery_url(EH), book.metadata_revision)
    updated = files.library.get_book(book.id)
    assert updated.metadata.title == "Existing gallery"
    assert updated.metadata.source == current.source
    assert updated.metadata.language is None
    assert OnlineMetadata(files, sources).notes(book.id) == {"notes": [], "comments": []}
    assert "candidates_unresolved" in Review(files).preview(book.id)["issues"]


def local_with_source(files, tmp_path, queue):
    """An imported book with a source whose check the owner queued (imports queue none)."""
    path = tmp_path / "remote.zip"
    metadata = Metadata.model_validate(queue.sources.fetch(gallery_url(EH))["metadata"])
    native_file(path, metadata)
    book = files.import_file(path).book
    with files.library.transaction() as db:
        enqueue_check(db, book.id)
    return book


def test_queued_source_check_confirms_the_book_and_preserves_personal(queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    state = PersonalState(rating=4.5, resume_page=1, completed=True)
    files.library.save_personal(book.id, state, expected_revision=1)
    assert service.checks.get(book.id)["state"] == "queued"
    remote.requests.clear()
    assert service.checks.step()
    assert service.checks.get(book.id)["state"] == "done"
    assert files.library.get_book(book.id).review_state == "confirmed"
    assert files.library.get_book(book.id).personal == state
    assert all(request.method == "GET" or request.url.path == "/api.php" for request in remote.requests)
    assert not service.checks.step()
    files.import_file(tmp_path / "remote.zip")
    assert not service.checks.step()


def test_online_rule_confirmation_is_reported_as_automatic(queue, files, tmp_path):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    assert service.checks.step()
    confirmation = Review(files).preview(book.id)["confirmation"]
    assert confirmation["action"] == "auto_confirm" and confirmation["rule"] == "online-source/1"
    assert confirmation["evidence_event_id"] is None


def test_source_retry_discards_inflight_observation_and_retry_resumes(queue, files, tmp_path):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    original = service.sources.fetch

    def paused(*args, **kw):
        observation = original(*args, **kw)
        current = service.checks.get(book.id)
        service.checks.action(book.id, "retry", current["generation"])
        return observation

    with patch.object(service.sources, "fetch", side_effect=paused):
        assert service.checks.step()
    assert service.checks.get(book.id)["state"] == "queued"
    assert files.library.get_book(book.id).metadata_revision == book.metadata_revision
    current = service.checks.get(book.id)
    service.checks.action(book.id, "retry", current["generation"])
    assert service.checks.step() and files.library.get_book(book.id).review_state == "confirmed"
    with pytest.raises(RevisionConflict):
        service.checks.action(book.id, "retry", current["generation"])


def test_source_failure_keeps_readable_archive_and_manual_edits(queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    changed = book.metadata.model_copy(deep=True)
    changed.title = "Owner title"
    Review(files).edit(book.id, changed, expected_revision=book.metadata_revision)
    remote.title = "Remote replacement"
    service.checks.step()
    assert files.library.get_book(book.id).metadata.title == "Owner title"
    assert service.checks.get(book.id)["state"] == "needs_review"
    assert files.page_bytes(book.id, 0).startswith(b"\x89PNG")


def test_http_upload_does_not_queue_a_source_check(queue, files, tmp_path):
    service, remote = queue
    source = tmp_path / "remote.zip"
    metadata = Metadata.model_validate(service.sources.fetch(gallery_url(EH))["metadata"])
    sidecar, _ = native_file(source, metadata)
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    from mangacouch.http import HTTPSettings, create_app

    remote.requests.clear()
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth, downloads=service), base_url=ORIGIN
    ) as client:
        login(client)
        response = client.post(
            "/api/import",
            params={"filename": "remote.zip"},
            headers=CSRF,
            files=[
                ("archive", ("upload.zip", source.read_bytes(), "application/zip")),
                ("native", ("upload.zip.mangacouch.json", sidecar.read_bytes(), "application/json")),
            ],
        )
        assert response.status_code == 200
        identifier = response.json()["book"]["id"]
    assert service.checks.get(identifier) is None
    assert remote.requests == []


def test_quarterly_source_refresh_is_bounded_and_retries_review_items(files):
    source = Source(site="ehentai", gallery_id=200, gallery_token="abcdef")
    identifiers = []
    now = int(time.time())
    for index in range(29):
        book, _ = register_book(files.library, content(f"scheduled-{index}"), Metadata(source=source))
        identifiers.append(book.id)
        with files.library.transaction() as db:
            db.execute(
                "INSERT INTO source_check(book_id,state,updated_at) VALUES(?,?,?)",
                (
                    book.id,
                    "needs_review" if index == 28 else "done",
                    now - (89 if index == 0 else 91) * 86400,
                ),
            )
    checks = SourceChecks(SimpleNamespace(library=files.library))
    # One hourly round queues one metadata batch.
    assert checks.schedule_refresh() == 25
    # Book 0 was checked 89 days ago: it waits for the 90-day mark. A check that needed review
    # (a failure or a mismatch) is tried again.
    assert checks.schedule_refresh() == 3
    assert checks.schedule_refresh() == 0
    first = checks.get(identifiers[0])
    assert first and first["state"] == "done"
    review = checks.get(identifiers[28])
    assert review and review["state"] == "queued"


def test_budgets_are_per_site_and_cdn_hosts_spend_none():
    hosts: list[str] = []

    def handler(request):
        hosts.append(request.url.host)
        return httpx.Response(200, content=b"ok")

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler))
    try:
        for _ in range(3):
            client.read("GET", "https://covers.example/a.jpg")
        assert client.bucket.level == 0
        # All hosts of E(x)Hentai spend one bucket: two searches and an API call.
        client.read("GET", "https://e-hentai.org/")
        client.read("GET", "https://g.e-hentai.org/")
        client.read("GET", "https://api.e-hentai.org/api.php")
        assert client.bucket.level == pytest.approx(4.5 + 4.5 + 1.5, abs=0.05)
        assert len(hosts) == 6
    finally:
        client.close()


def test_waiting_for_the_bucket_holds_no_lock_and_observes_cancellation():
    settings = SourceSettings(eh_budget_capacity=1000, eh_budget_leak_per_min=10)
    client = SourceHTTP(settings, {}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        with client.lock:
            # Six points over the capacity: a page waits 36 s for the bucket to drain.
            client.bucket.level, client.bucket.updated = 1005, time.monotonic()
        cancelled = threading.Event()
        outcome = []

        def waiter():
            try:
                client.read("GET", "https://e-hentai.org/g/1/abc/", cancelled=cancelled.is_set)
            except InterruptedError:
                outcome.append("cancelled")

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        # Other hosts proceed while one thread waits for the e-hentai bucket.
        assert client.read("GET", "https://cdn.example/file") == b""
        cancelled.set()
        thread.join(2)
        assert outcome == ["cancelled"]
    finally:
        client.close()


def test_rate_limit_starts_a_site_cooldown_that_fails_fast():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "60"})

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("GET", "https://exhentai.org/")
        with pytest.raises(SourceError) as caught:
            client.read("GET", "https://exhentai.org/g/1/a/")
        assert len(calls) == 1 and 55 <= caught.value.retry_after <= 60
        # One backend: a 429 from ExHentai pauses E-Hentai as well.
        assert set(client.cooldowns()) == {"ehentai", "exhentai"}
        assert client.cooldowns()["exhentai"] >= time.time() + 55
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("GET", "https://api.e-hentai.org/api.php")
        assert len(calls) == 1
    finally:
        client.close()


def test_eh_ban_page_cools_down_both_eh_sites():
    calls = []
    ban = (
        b"This IP address has been temporarily banned due to an excessive request rate. This probably "
        b"means you are using automated mirroring/harvesting software or share the IP address with "
        b"someone who does. The ban expires in 4 minutes and 9 seconds"
    )

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=ban)

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceError, match="source_rate_limited") as caught:
            client.json("POST", "https://api.e-hentai.org/api.php", json={})
        assert caught.value.retry_after == 4 * 60 + 9 + 5
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("GET", "https://exhentai.org/g/1/a/")
        assert len(calls) == 1 and set(client.cooldowns()) == {"ehentai", "exhentai"}
        # Archive hosts are not cooled down.
        assert client.read("GET", "https://archive.example/file") == ban
    finally:
        client.close()


@pytest.mark.parametrize(
    ("tail", "seconds"),
    [
        (b"1 day, 2 hours, 3 minutes and 4 seconds", 86400),
        (b"2 hours and 1 second", 7206),
        (b"59 seconds", 64),
        (b"soon", 3600),
    ],
)
def test_ban_seconds_parses_durations(tail, seconds):
    body = b"temporarily banned due to an excessive request rate. The ban expires in " + tail
    assert ban_seconds(body) == seconds
    assert ban_seconds(b"<html>gallery</html>") is None


def test_redirect_keeps_resume_headers_but_not_credentials_or_referer():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "e-hentai.org":
            return httpx.Response(302, headers={"location": "https://node.hath.example/archive"})
        return httpx.Response(206, content=b"rest")

    client = SourceHTTP(SourceSettings(), {"eh_cookie": "secret"}, transport=httpx.MockTransport(handler))
    try:
        headers = {"Range": "bytes=10-", "If-Range": '"v1"', "Referer": "https://e-hentai.org/g/1/a/"}
        with client.stream("GET", "https://e-hentai.org/archive", headers=headers) as response:
            assert response.status_code == 206
        final = seen[-1].headers
        assert final["range"] == "bytes=10-" and final["if-range"] == '"v1"'
        assert "cookie" not in final and "referer" not in final
    finally:
        client.close()


@pytest.mark.parametrize(
    "site,status,body,expected",
    [
        ("ehentai", 200, b"<p>Image Limits</p><p>You are currently at 10</p>", (True, True, None)),
        ("ehentai", 200, b"This page requires you to log on.", (True, False, "source_login_required")),
        ("exhentai", 200, b"<p>Image Limits</p>", (True, True, None)),
        ("exhentai", 200, b"", (True, False, "source_login_required")),
    ],
)
def test_verify_checks_credentials_not_just_reachability(site, status, body, expected):
    client = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body)),
    )
    try:
        result = Sources(client).verify(site)
        assert (result["reachable"], result["authenticated"], result["detail"]) == expected
    finally:
        client.close()


@pytest.mark.parametrize(
    "error,status",
    [
        (SourceError("source_rate_limited", 30), 429),
        (SourceError("source_network", 30), 502),
        (SourceError("source_unavailable", 30), 502),
        (SourceError("archive_preparing", 15), 502),
        (SourceError("source_login_required"), 409),
        (SourceError("unsafe_source_url"), 422),
        (SourceError("source_protocol"), 502),
    ],
)
def test_source_error_status_mapping(queue, files, monkeypatch, error, status):
    service, _ = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)

    def fail(_site, **kwargs):
        raise error

    monkeypatch.setattr(service.sources, "verify", fail)
    from mangacouch.http import HTTPSettings, create_app

    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        login(client)
        response = client.post("/api/sources/verify/ehentai", headers=CSRF)
        assert response.status_code == status
        body = response.json()
        # A source halt keeps its own code: it is not the sign-in throttle (rate_limited).
        assert body["error"] == error.code
        assert body["message"]
        assert ("retry_at" in body) == (error.code == "source_rate_limited")
        assert ("retry-after" in response.headers) == bool(error.retry_after)


@pytest.mark.usefixtures("without_maintenance")
def test_comment_endpoints(queue, files, tmp_path):
    service, remote = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    from mangacouch.http import HTTPSettings, create_app

    book = local_with_source(files, tmp_path, service)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        login(client)
        assert client.get(f"/api/books/{book.id}/source-comments").json()["comments"] == []
        response = client.post(f"/api/books/{book.id}/source-comments", json={"url": EH}, headers=CSRF)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["comments"][0]["body"] == "Fixture comment" and result["comments_error"] is None
        assert (
            client.get(f"/api/books/{book.id}/source-comments").json()["notes"][0]["source"]["gallery_id"]
            == 100
        )


def test_malformed_translation_database_is_reported_as_unsupported(files):
    from mangacouch.translations import Translations

    for data in [{"version": 7, "data": [{"data": {}}]}, {"version": 7, "data": [{"namespace": "a"}]}, []]:
        with pytest.raises(InvalidInput, match="Unsupported EhTagTranslation database"):
            Translations(files.library).import_data(data)


def test_rate_limited_source_check_waits_for_the_cooldown_instead_of_needing_review(queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    original = service.http.transport.handler
    service.http.transport.handler = lambda request: httpx.Response(429, headers={"retry-after": "90"})
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check is not None and check["state"] == "queued" and check["error_code"] == "source_rate_limited"
    assert check["retry_at"] >= time.time() + 80 and check["reasons"] == []
    assert not service.checks.step()
    service.http.transport.handler = original
    end_halts(service.http)
    with files.library.transaction() as db:
        db.execute("UPDATE source_check SET retry_at=0")
    assert service.checks.step()
    assert service.checks.get(book.id)["state"] == "done"


def test_waiting_request_observes_cooldown_before_sending():
    first_entered, release_first, stopping = threading.Event(), threading.Event(), threading.Event()
    calls, errors = [], []

    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            first_entered.set()
            assert release_first.wait(10)
            return httpx.Response(429, headers={"Retry-After": "120"})
        return httpx.Response(200, content=b"ok")

    settings = SourceSettings(eh_budget_capacity=1000, eh_budget_leak_per_min=10)
    client = SourceHTTP(settings, {}, transport=httpx.MockTransport(handler))

    def fetch(url):
        try:
            client.read("GET", url, cancelled=stopping.is_set)
        except SourceError as error:
            errors.append(error.code)
        except InterruptedError:
            pass  # release a failed test's waiting thread in finally

    first = threading.Thread(target=fetch, args=("https://e-hentai.org/g/1/abc/",))
    waiting = threading.Thread(target=fetch, args=("https://e-hentai.org/g/2/abc/",))
    first.start()
    try:
        assert first_entered.wait(5)
        with client.lock:
            # The second request now waits about 36 s for the bucket.
            client.bucket.level = 1005
        waiting.start()
        deadline = time.monotonic() + 5
        while client.waiting != 1:
            assert time.monotonic() < deadline, "the second request never waited for the bucket"
            time.sleep(0.005)
        release_first.set()
        first.join(3)
        waiting.join(3)
        assert not first.is_alive() and not waiting.is_alive()
        assert calls == ["https://e-hentai.org/g/1/abc/"]
        assert errors == ["source_rate_limited", "source_rate_limited"]
    finally:
        stopping.set()
        release_first.set()
        first.join(5)
        if waiting.ident is not None:
            waiting.join(5)
        client.close()


def test_queued_checks_share_one_metadata_request_and_respect_a_retry_mid_batch(queue, files, tmp_path):
    service, remote = queue
    sources = [Source(site="ehentai", gallery_id=100 + n, gallery_token="abcdef") for n in range(4)]
    books = []
    for index, source in enumerate(sources):
        archive = make_zip(tmp_path / f"batch-{index}.zip", [f"{index}-{n}.png" for n in range(2)])
        native_file(archive, Metadata(title=f"Batch {index}", source=source))
        books.append(files.import_file(archive).book)
    with files.library.transaction() as db:
        for book in books:
            enqueue_check(db, book.id)
    # The last book is paused while it waits for its turn in the batch.
    original = service.online.refresh

    def pause_last(identifier, *args, **kwargs):
        if identifier == books[0].id:
            current = service.checks.get(books[-1].id)
            service.checks.action(books[-1].id, "retry", current["generation"])
        return original(identifier, *args, **kwargs)

    remote.requests.clear()
    with patch.object(service.online, "refresh", side_effect=pause_last):
        assert service.checks.step()
    api = [request for request in remote.requests if request.url.path == "/api.php"]
    assert len(api) == 1
    assert sorted(gid for gid, _ in json.loads(api[0].content)["gidlist"]) == [100, 101, 102, 103]
    checks = [service.checks.get(book.id) for book in books]
    # The synthetic gallery declares other page counts, so the refreshed books await review.
    assert [(c["state"], c["error_code"]) for c in checks[:3]] == [("needs_review", None)] * 3
    assert all("declared_page_count_mismatch" in c["reasons"] for c in checks[:3])
    assert checks[3]["state"] == "queued"
    # Its prefetched entry is unused; a later retry still gets fresh metadata of its own.
    current = service.checks.get(books[-1].id)
    service.checks.action(books[-1].id, "retry", current["generation"])
    remote.requests.clear()
    assert service.checks.step()
    retried = service.checks.get(books[-1].id)
    assert retried["state"] == "needs_review" and retried["error_code"] is None
    assert len([r for r in remote.requests if r.url.path == "/api.php"]) == 1


def resolver(table: dict[str, str]):
    def getaddrinfo(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (table[host], 443))]

    return getaddrinfo


def test_gallery_links_accept_www_and_g_hosts_but_not_lookalikes():
    assert gallery_url("https://g.e-hentai.org/g/1/ab/").url == "https://e-hentai.org/g/1/ab/"
    assert gallery_url("https://www.exhentai.org/g/1/ab/").url == "https://exhentai.org/g/1/ab/"
    for url in ("https://x.e-hentai.org/g/1/ab/", "https://e-hentai.org.evil/g/1/ab/"):
        with pytest.raises(ValueError):
            gallery_url(url)


def test_private_dns_answers_are_refused_by_default(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", resolver({"e-hentai.org": "198.18.0.42"}))
    with pytest.raises(SourceError, match="source_private_address"):
        public_url("https://e-hentai.org/g/1/ab/")
    assert SourceSettings().allow_fake_ip is False


@pytest.mark.parametrize(
    "url,address,allowed",
    [
        ("https://e-hentai.org/", "198.18.0.42", True),
        ("https://archive.example/a", "198.19.255.1", True),
        ("https://e-hentai.org/", "100.64.0.1", False),
        ("https://e-hentai.org/", "10.0.0.1", False),
        ("https://e-hentai.org/", "127.0.0.1", False),
        ("https://e-hentai.org/", "192.168.1.2", False),
        ("https://198.18.0.5/archive", "198.18.0.5", False),
    ],
)
def test_fake_ip_setting_allows_only_that_range_for_host_names(monkeypatch, url, address, allowed):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", (address, 443))])
    if allowed:
        assert public_url(url, fake_ip=True) == url
    else:
        with pytest.raises(SourceError, match="source_private_address"):
            public_url(url, fake_ip=True)


def test_fake_ip_setting_still_checks_every_redirect(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        resolver({"e-hentai.org": "198.18.0.1", "fake.example": "198.18.0.2", "lan.example": "10.0.0.7"}),
    )
    targets = {"/to-lan": "https://lan.example/x", "/to-fake": "https://fake.example/x"}

    def handler(request):
        address = {"e-hentai.org": "198.18.0.1", "fake.example": "198.18.0.2"}[request.url.host]
        extensions = {"network_stream": SimpleNamespace(get_extra_info=lambda name: (address, 443))}
        if request.url.path in targets:
            return httpx.Response(302, headers={"location": targets[request.url.path]}, extensions=extensions)
        return httpx.Response(200, content=b"ok", extensions=extensions)

    def client(settings):
        http = SourceHTTP(settings, {}, transport=httpx.MockTransport(handler))
        http.transport = None  # Resolve host names as without a test transport.
        return http

    default, fake_ip = client(SourceSettings()), client(SourceSettings(allow_fake_ip=True))
    try:
        with pytest.raises(SourceError, match="source_private_address"):
            default.read("GET", "https://e-hentai.org/")
        assert fake_ip.read("GET", "https://e-hentai.org/to-fake") == b"ok"
        with pytest.raises(SourceError, match="source_private_address"):
            fake_ip.read("GET", "https://e-hentai.org/to-lan")
    finally:
        default.close()
        fake_ip.close()


def offline(_request):
    raise httpx.ConnectError("down")


def make_due(files) -> None:
    with files.library.transaction() as db:
        db.execute("UPDATE source_check SET retry_at=0")


def test_a_passing_failure_retries_the_check_with_capped_backoff(queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    service.http.transport.handler = offline
    waits = []
    for attempt in range(1, 5):
        assert service.checks.step()
        check = service.checks.get(book.id)
        assert check and (check["state"], check["error_code"]) == ("queued", "source_network")
        assert check["reasons"] == ["source_network"] * attempt
        waits.append(check["retry_at"] - int(time.time()))
        assert not service.checks.step()  # it waits for its time
        make_due(files)
    assert waits == [pytest.approx(wait, abs=2) for wait in (60, 240, 960, 3600)]
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"]) == ("queued", "source_network")
    # Retried automatically once the source is back, it confirms the book.
    service.http.transport.handler = remote
    make_due(files)
    assert service.checks.step()
    assert service.checks.get(book.id)["state"] == "done"


def test_a_lasting_failure_needs_review_at_once(queue, files, tmp_path):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    service.http.transport.handler = lambda request: httpx.Response(404)
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"], check["retry_at"]) == (
        "needs_review",
        "source_unavailable",
        0,
    )


def test_a_check_stopped_midway_is_queued_again(queue, files, tmp_path, monkeypatch):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)

    def stopped(*_args, **_kwargs):
        raise InterruptedError

    monkeypatch.setattr(service.online, "refresh", stopped)
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"], check["reasons"]) == ("queued", None, [])


def test_recover_queues_checks_a_restart_interrupted_and_keeps_the_rest(files):
    later = int(time.time()) + 600
    states = [
        ("running", None, 0),
        ("queued", "source_rate_limited", later),
        ("needs_review", "source_unavailable", 0),
    ]
    identifiers = []
    for index, (state, code, retry_at) in enumerate(states):
        book, _ = register_book(
            files.library,
            content(f"recover-{index}"),
            Metadata(source=Source(site="ehentai", gallery_id=200, gallery_token="abcdef")),
        )
        identifiers.append(book.id)
        with files.library.transaction() as db:
            db.execute(
                "INSERT INTO source_check(book_id,state,error_code,retry_at,updated_at) VALUES(?,?,?,?,0)",
                (book.id, state, code, retry_at),
            )
    checks = SourceChecks(SimpleNamespace(library=files.library))
    checks.recover()
    after = [checks.get(identifier) or {} for identifier in identifiers]
    assert [(check["state"], check["error_code"], check["retry_at"]) for check in after] == [
        ("queued", None, 0),
        ("queued", "source_rate_limited", later),
        ("needs_review", "source_unavailable", 0),
    ]


def test_a_running_check_reads_its_state_at_most_once_a_second(queue, files, tmp_path, monkeypatch):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    reads, seen = [], []
    original = service.checks.get
    # A fake clock: a slow runner must not let CANCEL_POLL pass between the asks.
    clock = [1000.0]

    def counted(identifier):
        reads.append(identifier)
        return original(identifier)

    def waiting(book_id, source, revision, *, cancelled, **_kwargs):
        # A request waiting for the bucket asks every POLL (0.1 s).
        asks = []
        for _ in range(50):
            asks.append(cancelled())
            clock[0] += source_checks_module.CANCEL_POLL / 100
        seen.append(asks)
        seen.append(len(reads))
        service.checks.action(book_id, "retry", original(book_id)["generation"])
        seen.append(cancelled())
        clock[0] += source_checks_module.CANCEL_POLL
        seen.append(cancelled())
        raise InterruptedError

    monkeypatch.setattr(
        source_checks_module, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0])
    )
    monkeypatch.setattr(service.checks, "get", counted)
    monkeypatch.setattr(service.online, "refresh", waiting)
    assert service.checks.step()
    # One read before the run, none for 50 asks within CANCEL_POLL.
    assert seen == [[False] * 50, 1, False, True]
    # The pause stands: the stopped run does not queue the check again.
    assert original(book.id)["state"] == "queued"


def test_source_check_routes_list_and_retry_checks(api, queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    service.http.transport.handler = lambda request: httpx.Response(404)
    assert service.checks.step()
    service.http.transport.handler = remote
    listed = api.get("/api/source-checks").json()
    assert listed["total"] == 1
    [item] = listed["items"]
    check = item["source_check"]
    assert item["id"] == book.id
    assert (check["state"], check["error_code"]) == ("needs_review", "source_unavailable")

    def act(action, generation):
        return api.post(
            f"/api/books/{book.id}/source-check",
            params={"expected_generation": generation},
            json={"action": action},
        )

    stale = act("retry", check["generation"] + 1)
    assert stale.status_code == 409 and stale.json()["error"] == "revision_conflict"
    retried = act("retry", check["generation"])
    assert retried.status_code == 200
    assert (retried.json()["state"], retried.json()["error_code"]) == ("queued", None)
    assert api.get("/api/source-checks").json() == {"items": [], "total": 0}
    assert act("pause", retried.json()["generation"]).status_code == 422
    missing = api.post(
        "/api/books/missing/source-check", params={"expected_generation": 1}, json={"action": "retry"}
    )
    assert missing.status_code == 404
    with files.library.transaction() as db:
        db.execute(
            "UPDATE book SET metadata_json=json_remove(metadata_json,'$.source') WHERE id=?", (book.id,)
        )
    absent = act("retry", retried.json()["generation"])
    assert absent.status_code == 422 and absent.json()["error"] == "source_absent"


def test_malformed_comments_keep_the_metadata_and_request_bugs_stay_errors(sources, monkeypatch):
    from mangacouch import source_adapters

    def malformed(*args):
        raise KeyError("c3")

    monkeypatch.setattr(source_adapters, "parse_eh_comments", malformed)
    eh = sources.fetch(gallery_url(EH))
    assert eh["metadata"]["title"] and eh["comments_state"] == "failed"
    assert eh["comments_error"] == "source_protocol"

    def bug(*args, **kwargs):
        raise TypeError("not a response problem")

    monkeypatch.setattr(sources.http, "json", bug)
    with pytest.raises(TypeError):
        sources.fetch(gallery_url(EH))
