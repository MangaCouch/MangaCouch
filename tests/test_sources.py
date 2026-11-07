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
from source_fixtures import EH, NH, SourceFixture

from mangacouch.auth import Auth
from mangacouch.database import RevisionConflict
from mangacouch.models import Metadata, PersonalState, Source, SourceComment
from mangacouch.online import OnlineMetadata
from mangacouch.review import Review
from mangacouch.snapshots import create_snapshot, restore_snapshot
from mangacouch.source_adapters import Sources, bounded_comments, parse_quote
from mangacouch.source_checks import SourceChecks
from mangacouch.source_config import SourceSettings, SourceVault
from mangacouch.source_http import SourceError, SourceHTTP, gallery_url, public_url


@pytest.fixture
def remote(tmp_path):
    return SourceFixture(make_zip(tmp_path / "source.zip").read_bytes())


@pytest.fixture
def sources(remote):
    http = SourceHTTP(
        SourceSettings(), {"nh_key": "fixture-key"}, transport=httpx.MockTransport(remote), pace=0
    )
    yield Sources(http)
    http.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://nhentai.net.evil/g/1/",
        "https://user@nhentai.net/g/1/",
        "http://127.0.0.1/g/1/",
        "https://nhentai.net:22/g/1/",
        "https://e-hentai.org/g/1/not-hex/",
    ],
)
def test_gallery_identity_rejects_lookalikes(url):
    with pytest.raises(ValueError):
        gallery_url(url)


def test_remote_url_rejects_private_redirect_target(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(SourceError, match="unsafe_source_url"):
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
        {"eh_cookie": "eh=secret", "nh_key": "nh-secret"},
        transport=httpx.MockTransport(handler),
        pace=0,
    )
    assert http.read("GET", EH) == b"ok"
    assert seen[0].headers["cookie"] == "eh=secret"
    assert "cookie" not in seen[1].headers and "authorization" not in seen[1].headers
    http.read("GET", "https://cdn.example/file")
    assert "cookie" not in seen[-1].headers
    http.read("GET", "https://nhentai.net/api/v2/galleries/200")
    assert seen[-1].headers["authorization"] == "Key nh-secret"
    http.read("GET", NH)
    assert "authorization" not in seen[-1].headers
    http.close()


def test_http_limits_cancellation_and_challenge():
    for response, code in [
        (httpx.Response(200, content=b"x" * 11), "source_response_too_large"),
        (httpx.Response(403, headers={"cf-mitigated": "challenge"}), "verification_required"),
        (httpx.Response(429, headers={"retry-after": "10"}), "source_rate_limited"),
    ]:
        client = SourceHTTP(
            SourceSettings(), {}, transport=httpx.MockTransport(lambda req, value=response: value), pace=0
        )
        with pytest.raises(SourceError, match=code):
            client.read("GET", EH, limit=10)
        with pytest.raises(InterruptedError):
            client.read("GET", EH, cancelled=lambda: True)
        client.close()


def test_socks_proxy_failure_becomes_retryable_source_error():
    def denied(request):
        raise socksio.ProtocolError("Connection not allowed by ruleset")

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(denied), pace=0)
    try:
        with pytest.raises(SourceError, match="source_network") as caught:
            client.read("GET", EH)
        assert caught.value.retry_after == 30
    finally:
        client.close()


def test_source_contracts_comments_and_quotes(sources, remote):
    eh = sources.fetch(gallery_url(EH))
    nh = sources.fetch(gallery_url(NH))
    assert eh["comments"][0]["score"] == 3 and eh["notes"]["comments_complete"]
    assert not eh["metadata"].get("comments") and not eh["metadata"].get("source_notes")
    assert nh["comments"][0]["comment_id"] == "1"
    assert nh["cover_url"] == "https://t.nhentai.net/galleries/1/cover.jpg"
    assert sources.quote(gallery_url(EH))[0] == {"original_gp": 25, "resample_gp": 0, "balance_gp": 5000}
    assert parse_quote("unrecognized HTML")["original_gp"] is None
    assert sources.search_eh(nh)[0]["metadata"]["source"]["site"] == "ehentai"
    remote.comments_error = True
    assert sources.fetch(gallery_url(EH))["comments_error"] == "source_login_required"
    assert sources.fetch(gallery_url(NH))["comments_error"] == "source_rate_limited"


def test_comment_pages_and_bytes_are_bounded():
    pages = []
    fixture = SourceFixture(b"zip")

    def handler(req):
        if req.url.path.endswith("/comments"):
            page = int(req.url.params["page"])
            pages.append(page)
            return httpx.Response(
                200,
                json={
                    "num_pages": 100,
                    "result": [
                        {
                            "id": page,
                            "gallery_id": 200,
                            "poster": {"username": "Example"},
                            "body": "bounded",
                            "post_date": page,
                        }
                    ],
                },
            )
        return fixture(req)

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler), pace=0)
    result = Sources(client).fetch(gallery_url(NH))
    assert pages == [1, 2, 3, 4, 5] and not result["notes"]["comments_complete"]
    comments = [
        SourceComment(source=gallery_url(NH), comment_id=str(i), author="x", body="中" * 8192)
        for i in range(250)
    ]
    kept, complete = bounded_comments(comments)
    assert len(kept) <= 5 and not complete
    client.close()


def test_refresh_preserves_manual_and_partial_comments_and_removes_stale_tags(
    files, tmp_path, sources, remote
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    online = OnlineMetadata(files, sources)
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    book = files.library.get_book(book.id)
    assert online.notes(book.id)["comments"][0]["body"] == "Fixture comment"
    edited = book.metadata.model_copy(deep=True)
    edited.title = "Manual title"
    book = Review(files).edit(book.id, edited, expected_revision=book.metadata_revision)
    remote.tags = ["language:english", "artist:New"]
    remote.comment_body = "Edited source comment"
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    book = files.library.get_book(book.id)
    assert book.metadata.title == "Manual title" and "title" in book.metadata.protected_fields
    comments = online.notes(book.id)["comments"]
    assert len(comments) == 1 and comments[0]["body"] == "Edited source comment"
    preview = Review(files).preview(book.id)
    candidate = preview["candidates"]["online:ehentai"]
    assert {t["value"] for t in candidate["metadata"]["tags"]} == {"english", "New"}
    remote.tags = ["language:english"]
    remote.comments_error = True
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    book = files.library.get_book(book.id)
    assert online.notes(book.id)["comments"][0]["body"] == "Edited source comment"
    candidate = Review(files).preview(book.id)["candidates"]["online:ehentai"]
    assert {t["value"] for t in candidate["metadata"]["tags"]} == {"english"}
    with pytest.raises(RevisionConflict):
        online.refresh(book.id, gallery_url(EH), 1)


def test_comment_refresh_never_touches_curated_metadata_review_or_sidecar(files, tmp_path, sources, remote):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    online = OnlineMetadata(files, sources)
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    before = files.library.get_book(book.id)
    with files.library.connection() as db:
        sidecar = db.execute(
            "SELECT pending_revision FROM sidecar_sync WHERE book_id=?", (book.id,)
        ).fetchone()
    remote.comment_body = "A brand new comment"
    result = online.refresh_comments(book.id, gallery_url(EH))
    assert [c["body"] for c in result["comments"]] == ["A brand new comment"]
    assert result["notes"][0]["source"]["gallery_id"] == 100
    after = files.library.get_book(book.id)
    assert after.metadata_revision == before.metadata_revision and after.review_state == before.review_state
    with files.library.connection() as db:
        assert (
            db.execute("SELECT pending_revision FROM sidecar_sync WHERE book_id=?", (book.id,)).fetchone()
            == sidecar
        )
    with pytest.raises(ValueError):
        online.refresh_comments(book.id, gallery_url(NH))


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
    path = tmp_path / "remote.zip"
    metadata = Metadata.model_validate(queue.sources.fetch(gallery_url(EH))["metadata"])
    native_file(path, metadata)
    return files.import_file(path).book


def test_source_check_is_enqueued_atomically_and_preserves_personal(queue, files, tmp_path):
    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    state = PersonalState(rating=4.5, resume_page=1, completed=True, bookmarks=[0])
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


def test_source_pause_discards_inflight_observation_and_retry_resumes(queue, files, tmp_path):
    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    original = service.sources.fetch

    def paused(*args, **kw):
        observation = original(*args, **kw)
        current = service.checks.get(book.id)
        service.checks.action(book.id, "pause", current["generation"])
        return observation

    with patch.object(service.sources, "fetch", side_effect=paused):
        assert service.checks.step()
    assert service.checks.get(book.id)["state"] == "paused"
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


def test_http_upload_worker_runs_source_check_without_user_refresh(queue, files, tmp_path):
    service, _ = queue
    source = tmp_path / "remote.zip"
    metadata = Metadata.model_validate(service.sources.fetch(gallery_url(EH))["metadata"])
    sidecar, _ = native_file(source, metadata)
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    from mangacouch.http import HTTPSettings, create_app

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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if files.library.get_book(identifier).review_state == "confirmed":
                break
            time.sleep(0.01)
        assert files.library.get_book(identifier).review_state == "confirmed"


def test_weekly_source_refresh_is_bounded_and_leaves_review_or_paused_items_alone(files):
    source = Source(site="nhentai", gallery_id=200)
    identifiers = []
    now = int(time.time())
    for index in range(25):
        book, _ = register_book(files.library, content(f"scheduled-{index}"), Metadata(source=source))
        identifiers.append(book.id)
        with files.library.transaction() as db:
            db.execute(
                "INSERT INTO source_check(book_id,state,updated_at) VALUES(?,?,?)",
                (
                    book.id,
                    "needs_review" if index == 23 else "paused" if index == 24 else "done",
                    now - 8 * 86400,
                ),
            )
    checks = SourceChecks(SimpleNamespace(library=files.library))
    assert checks.schedule_refresh() == 20
    assert checks.schedule_refresh() == 3
    assert checks.schedule_refresh() == 0
    review, paused = checks.get(identifiers[23]), checks.get(identifiers[24])
    assert review and review["state"] == "needs_review"
    assert paused and paused["state"] == "paused"


def test_pacing_is_per_site_and_cdn_hosts_are_not_paced():
    stamps: dict[str, list[float]] = {}

    def handler(request):
        stamps.setdefault(request.url.host, []).append(time.monotonic())
        return httpx.Response(200, content=b"ok")

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler), pace=0.2)
    try:
        for _ in range(3):
            client.read("GET", "https://covers.example/a.jpg")
        client.read("GET", "https://nhentai.net/api/v2/config")
        client.read("GET", "https://e-hentai.org/")
        client.read("GET", "https://api.e-hentai.org/api.php")
        covers = stamps["covers.example"]
        assert covers[-1] - covers[0] < 0.15
        # Different sites do not wait for each other; one site's API and HTML hosts share a pace.
        assert stamps["e-hentai.org"][0] - stamps["nhentai.net"][0] < 0.15
        assert stamps["api.e-hentai.org"][0] - stamps["e-hentai.org"][0] >= 0.18
    finally:
        client.close()


def test_waiting_for_a_paced_slot_holds_no_lock_and_observes_cancellation():
    client = SourceHTTP(
        SourceSettings(), {}, transport=httpx.MockTransport(lambda r: httpx.Response(200)), pace=30
    )
    try:
        client.read("GET", "https://e-hentai.org/")
        cancelled = threading.Event()
        outcome = []

        def waiter():
            try:
                client.read("GET", "https://e-hentai.org/", cancelled=cancelled.is_set)
            except InterruptedError:
                outcome.append("cancelled")

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        # Other hosts proceed while one thread sleeps for its e-hentai slot.
        assert client.read("GET", "https://nhentai.net/api/v2/config") == b""
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

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler), pace=0)
    try:
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("GET", "https://exhentai.org/")
        with pytest.raises(SourceError) as caught:
            client.read("GET", "https://exhentai.org/g/1/a/")
        assert len(calls) == 1 and 55 <= caught.value.retry_after <= 60
        assert set(client.cooldowns()) == {"exhentai"}
        assert client.cooldowns()["exhentai"] >= time.time() + 55
    finally:
        client.close()


def test_redirect_keeps_resume_headers_but_not_credentials_or_referer():
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.host == "e-hentai.org":
            return httpx.Response(302, headers={"location": "https://node.hath.example/archive"})
        return httpx.Response(206, content=b"rest")

    client = SourceHTTP(
        SourceSettings(), {"eh_cookie": "secret"}, transport=httpx.MockTransport(handler), pace=0
    )
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
        ("exhentai", 200, b'<table class="itg"></table>', (True, True, None)),
        ("exhentai", 200, b"", (True, False, "source_login_required")),
        ("nhentai", 200, b"{}", (True, True, None)),
        ("nhentai", 401, b"", (True, False, "source_login_required")),
        ("nhentai", 502, b"", (False, False, "source_unavailable")),
    ],
)
def test_verify_checks_credentials_not_just_reachability(site, status, body, expected):
    client = SourceHTTP(
        SourceSettings(),
        {"nh_key": "key"},
        transport=httpx.MockTransport(lambda r: httpx.Response(status, content=body)),
        pace=0,
    )
    try:
        result = Sources(client).verify(site)
        assert (result["reachable"], result["authenticated"], result["detail"]) == expected
    finally:
        client.close()
    keyless = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert Sources(keyless).verify("nhentai")["detail"] == "nh_key_required"
    keyless.close()


@pytest.mark.parametrize(
    "error,status",
    [
        (SourceError("source_rate_limited", 30), 429),
        (SourceError("source_network", 30), 502),
        (SourceError("source_unavailable", 30), 503),
        (SourceError("archive_preparing", 15), 503),
        (SourceError("source_login_required"), 409),
        (SourceError("nh_key_required"), 409),
        (SourceError("insufficient_gp"), 409),
        (SourceError("unsafe_source_url"), 422),
        (SourceError("source_protocol"), 502),
    ],
)
def test_source_error_status_mapping(queue, files, monkeypatch, error, status):
    service, _ = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)

    def fail(_site):
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
        assert body["error"] == ("rate_limited" if error.code == "source_rate_limited" else error.code)
        assert body["message"]
        assert ("retry-after" in response.headers) == bool(error.retry_after)


def test_comment_and_cover_endpoints(queue, files, tmp_path):
    service, remote = queue
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    from mangacouch.http import HTTPSettings, create_app

    book = local_with_source(files, tmp_path, service)
    with TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        login(client)
        assert client.get(f"/api/books/{book.id}/source-comments").json() == {"notes": [], "comments": []}
        response = client.post(f"/api/books/{book.id}/source-comments", json={"url": EH}, headers=CSRF)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["comments"][0]["body"] == "Fixture comment" and result["comments_error"] is None
        assert (
            client.get(f"/api/books/{book.id}/source-comments").json()["notes"][0]["source"]["gallery_id"]
            == 100
        )
        job = service.create(NH)
        deadline = time.monotonic() + 5
        while service.get(job["id"])["state"] != "needs_choice" and time.monotonic() < deadline:
            if not service.step():
                time.sleep(0.02)
        before = len(remote.requests)
        for _ in range(2):
            cover = client.get(f"/api/downloads/{job['id']}/cover/0")
            assert cover.status_code == 200, cover.text
            assert cover.headers["content-type"] == "image/png"
        assert len([r for r in remote.requests[before:] if r.url.host == "covers.example"]) == 1
        assert client.get(f"/api/downloads/{job['id']}/cover/-2").status_code == 404


def test_malformed_translation_database_is_reported_as_unsupported(files):
    from mangacouch.errors import InvalidInput
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
    service.http.cooldown.clear()
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

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler), pace=10)

    def fetch(url):
        try:
            client.read("GET", url, cancelled=stopping.is_set)
        except SourceError as error:
            errors.append(error.code)
        except InterruptedError:
            pass  # release a failed test's waiting thread in finally

    first = threading.Thread(target=fetch, args=("https://e-hentai.org/first",))
    waiting = threading.Thread(target=fetch, args=("https://e-hentai.org/second",))
    first.start()
    try:
        assert first_entered.wait(5)
        with client.lock:
            initial_slot = client.next_slot["ehentai"]
        waiting.start()
        deadline, reserved = time.monotonic() + 5, False
        while time.monotonic() < deadline:
            with client.lock:
                reserved = client.next_slot.get("ehentai", 0) > initial_slot
            if reserved:
                break
            time.sleep(0.005)
        assert reserved
        release_first.set()
        first.join(3)
        waiting.join(3)
        assert not first.is_alive() and not waiting.is_alive()
        assert calls == ["https://e-hentai.org/first"]
        assert errors == ["source_rate_limited", "source_rate_limited"]
    finally:
        stopping.set()
        release_first.set()
        first.join(5)
        if waiting.ident is not None:
            waiting.join(5)
        client.close()
