from __future__ import annotations

import json
import socket

import httpx
import pytest
from source_fixtures import EH

from mangacouch.models import Source
from mangacouch.source_http import SourceError


@pytest.mark.parametrize("status", [200, 403, 503])
@pytest.mark.parametrize("media", ["text/html", "application/json"])
def test_ban_detection_ignores_status_media_and_reason_wording(queue, status, media):
    service, remote = queue
    remote_body = b"Your IP address has been temporarily banned. The ban expires in 2 minutes."
    seen = []

    def answer(request):
        seen.append(request)
        return httpx.Response(status, content=remote_body, headers={"Content-Type": media})

    service.http.transport.handler = answer
    with pytest.raises(SourceError) as error:
        service.http.read("GET", EH)
    assert error.value.code == "source_rate_limited"
    assert service.http.cooldowns()["ehentai"] > 0
    with pytest.raises(SourceError):
        service.http.read("GET", EH)
    assert len(seen) == 1


def test_prefetch_keeps_good_rows_without_cross_site_retry(queue, monkeypatch):
    service, _ = queue
    one = Source(site="ehentai", gallery_id=100, gallery_token="abc")
    two = Source(site="ehentai", gallery_id=101, gallery_token="def")
    calls = []

    def gdata(batch, cancelled):
        calls.append(batch)
        if batch[0].site == "exhentai":
            raise SourceError("source_login_required")
        return [{"gid": 100, "error": "gone"}, {"gid": 101, "title": "kept"}]

    monkeypatch.setattr(service.sources, "_gdata", gdata)
    service.sources.prefetch([one, two])
    assert len(calls) == 1 and service.sources.prefetched(one) and service.sources.prefetched(two)
    service.sources.forget_prefetched([one, two])
    assert not service.sources._prefetched


def test_second_dns_lookup_failure_is_transient_network_error(queue, monkeypatch):
    service, _ = queue
    service.http.transport = None
    monkeypatch.setattr("mangacouch.source_http.public_url", lambda *args, **kwargs: EH)

    def fail(*args, **kwargs):
        raise socket.gaierror("DNS temporary failure")

    monkeypatch.setattr("mangacouch.source_http.socket.getaddrinfo", fail)
    with pytest.raises(SourceError) as error:
        service.http.read("GET", EH)
    assert error.value.code == "source_network" and error.value.unsent


def test_short_invalid_api_responses_halt_after_three_and_valid_batch_resets(queue):
    service, remote = queue
    service.http.clock = lambda: 1000000.0
    source = Source(site="ehentai", gallery_id=100, gallery_token="abc")
    service.http.transport.handler = lambda request: httpx.Response(200, text="unexpected short page")
    for _ in range(2):
        with pytest.raises(SourceError, match="source_protocol"):
            service.sources.fetch(source, comments=False)
    service.http.transport.handler = remote
    service.sources.fetch(source, comments=False)
    service.http.transport.handler = lambda request: httpx.Response(200, text="unexpected short page")
    for _ in range(2):
        with pytest.raises(SourceError, match="source_protocol"):
            service.sources.fetch(source, comments=False)
    with pytest.raises(SourceError, match="source_rate_limited") as error:
        service.sources.fetch(source, comments=False)
    assert error.value.retry_after == 600


def test_response_deadline_bounds_slow_drip_and_releases_slot(queue, monkeypatch):
    service, _ = queue
    clock = [0.0]
    closed = []
    monkeypatch.setattr("mangacouch.source_http.time.monotonic", lambda: clock[0])

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                clock[0] += 0.4
                yield b"x"

        def close(self):
            closed.append(True)

    service.http.transport.handler = lambda request: httpx.Response(200, stream=Drip())
    with pytest.raises(SourceError, match="source_network"):
        service.http.read("GET", "https://archive.example/file.zip", total_timeout=1)
    assert closed and clock[0] < 2
    assert service.http.slot_users["archive.example"] == 0


def test_active_cdn_semaphore_is_not_evicted(queue):
    service, _ = queue
    http = service.http
    releases = [http._admit(f"https://cdn{i}.example/file", None) for i in range(65)]
    first = http.slots["cdn0.example"]
    again = http._admit("https://cdn0.example/other", None)
    try:
        assert http.slots["cdn0.example"] is first
    finally:
        again()
        for release in releases:
            release()
    assert len(http.slots) <= 64


def test_reset_with_proxy_cannot_silently_send_direct_requests(queue):
    from mangacouch.source_config import SourceSettings

    service, remote = queue
    service.save_settings(SourceSettings(), {"proxy": "http://localhost:7890"}, 0)
    reset = service.reset_credentials()
    assert reset["settings"]["online_paused"]
    with pytest.raises(SourceError, match="source_restore_paused"):
        service.http.read("GET", EH)
    assert not remote.requests


def test_rating_only_refresh_keeps_confirmed_candidate_decision(queue, files, tmp_path):
    from test_sources import local_with_source

    from mangacouch.models import SourceRating
    from mangacouch.review import Review

    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    service.checks.step()
    before = files.library.get_book(book.id)
    assert before.review_state == "confirmed"
    preview = Review(files).preview(book.id)
    Review(files).candidate(
        book.id,
        "online:ehentai",
        preview["token"],
        {field: "current" for field in preview["candidates"]["online:ehentai"]["fields"]},
    )
    before = files.library.get_book(book.id)
    remote = service.sources.fetch(before.metadata.source, comments=False)
    remote["metadata"]["source_rating"] = SourceRating(average=3.0).model_dump()
    service.online.apply(book.id, remote, before.metadata_revision)
    after = files.library.get_book(book.id)
    assert after.review_state == "confirmed"
    assert Review(files).preview(book.id)["candidates"]["online:ehentai"]["decision"] == "selected"
    assert "source_rating" not in Review(files).preview(book.id)["candidates"]["online:ehentai"]["fields"]


def test_skipping_another_gallery_candidate_preserves_own_source_evidence(queue, files, tmp_path):
    from test_sources import local_with_source

    from mangacouch.review import Review

    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    service.checks.step()
    before = files.library.get_book(book.id)
    other = Source(site="ehentai", gallery_id=999, gallery_token="abc")
    service.online.refresh(book.id, other, before.metadata_revision, link_source=True, comments=False)
    review = Review(files)
    current = review.preview(book.id)
    review.candidate(book.id, "online:ehentai", current["token"], None)
    assert "source_unverified" not in review.preview(book.id)["issues"]
    assert "declared_byte_size_mismatch" not in review.preview(book.id)["issues"]


def test_zero_page_gallery_and_uppercase_token_have_specific_results(queue):
    service, remote = queue
    source = Source(site="ehentai", gallery_id=100, gallery_token="ABCDEF")
    assert source.gallery_token == "abcdef"
    assert service.sources.fetch(source, comments=False)["metadata"]["source"]["gallery_token"] == "abcdef"

    def empty(request):
        response = remote(request)
        body = response.json()
        body["gmetadata"][0]["filecount"] = "0"
        return httpx.Response(200, json=body)

    service.http.transport.handler = empty
    with pytest.raises(SourceError, match="gallery_unavailable"):
        service.sources.fetch(source, comments=False)


def test_source_check_page_overflow_is_rejected(api):
    response = api.get("/api/source-checks", params={"page": 2**63})
    assert response.status_code == 422


def test_bulk_source_refresh_reports_retired_books_as_ignored(files):
    import uuid

    from helpers import content, register_book

    from mangacouch.models import Metadata
    from mangacouch.organization import BulkAction, BulkSelectionRequest, Organization

    book, _ = register_book(
        files.library, content("retired"), Metadata(source=Source(site="nhentai", gallery_id=100))
    )
    org = Organization(files.library)
    job = org.create_bulk(
        BulkSelectionRequest(id=str(uuid.uuid4()), action=BulkAction(kind="source"), book_ids=[book.id])
    )
    org.advance(budget=10)
    counts = org.bulk(job["id"])["counts"]
    assert counts["done"] == 0 and counts["ignored"] == 1


def ex_books(service, files, tmp_path, count: int) -> list[str]:
    """`count` imported ExHentai books, each with a queued source check."""
    from helpers import make_zip
    from test_sources import native_file

    from mangacouch.models import Metadata
    from mangacouch.source_checks import enqueue_check
    from mangacouch.source_http import gallery_url

    metadata = Metadata.model_validate(service.sources.fetch(gallery_url(EH))["metadata"])
    ids = []
    for index in range(count):
        path = tmp_path / f"ex{index}.zip"
        make_zip(path, [f"{page}.png" for page in range(index + 1)])  # distinct files
        source = Source(site="exhentai", gallery_id=100 + index, gallery_token="abcdef")
        native_file(path, metadata.model_copy(update={"source": source}))
        book = files.import_file(path).book
        with files.library.transaction() as db:
            enqueue_check(db, book.id)
        ids.append(book.id)
    return ids


@pytest.mark.parametrize("cookie", [False, True])
def test_prefetch_for_exhentai_books_is_consumed_in_one_request(queue, files, tmp_path, cookie):
    """A batch of several ExHentai books costs one gdata request: on api.e-hentai.org without an
    ExHentai cookie, on exhentai.org with one; every book then takes its entry."""
    from mangacouch.source_config import SourceSettings

    service, remote = queue
    if cookie:
        service.save_settings(SourceSettings(), {"ex_cookie": "ipb_member_id=1; igneous=y"}, 0)
    books = ex_books(service, files, tmp_path, 3)
    remote.requests.clear()
    assert service.checks.step()
    assert [(request.url.host, request.url.path) for request in remote.requests] == [
        ("exhentai.org" if cookie else "api.e-hentai.org", "/api.php")
    ]
    assert sorted(gallery for gallery, _ in json.loads(remote.requests[0].content)["gidlist"]) == [
        100,
        101,
        102,
    ]
    assert not service.sources._prefetched
    for book_id in books:
        check = service.checks.get(book_id)
        assert check and check["state"] in {"done", "needs_review"} and check["error_code"] is None


def test_translation_import_updates_search_version_once_for_100000_rows(files):
    from mangacouch.translations import Translations

    with files.library.transaction() as db:
        db.execute("CREATE TABLE search_changes(count INTEGER)")
        db.execute("INSERT INTO search_changes VALUES(0)")
        db.execute(
            "CREATE TRIGGER count_search_changes AFTER UPDATE ON search_revision "
            "BEGIN UPDATE search_changes SET count=count+1; END"
        )
    Translations(files.library).import_data(
        {
            "version": 7,
            "head": {"sha": "large"},
            "data": [
                {"namespace": "artist", "data": {f"tag{i}": {"name": f"译名{i}"} for i in range(100000)}}
            ],
        }
    )
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM tag_translation").fetchone()[0] == 100000
        assert db.execute("SELECT count FROM search_changes").fetchone()[0] == 1


def test_download_chunks_expose_one_byte_immediately_without_waiting_to_fill_buffer():
    from mangacouch.downloads import response_chunks

    class OneByte(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x"
            raise AssertionError("consumer must regain control after one byte")

    response = httpx.Response(200, stream=OneByte())
    chunks = response_chunks(response)
    try:
        assert next(chunks) == b"x"
    finally:
        chunks.close()
        response.close()


def test_three_unrecognized_answers_pause_ten_minutes_without_a_ban_penalty(queue):
    """The fallback for answers nobody recognizes pauses requests; unlike a ban it neither fills
    nor shrinks the usage bucket, logs no rate event and is not stored."""
    from source_fixtures import end_halts

    from mangacouch.source_http import INVALID_SECONDS

    service, remote = queue
    http = service.http
    source = Source(site="ehentai", gallery_id=100, gallery_token="abc")
    level = http.budget()["ehentai"]["level"]
    http.transport.handler = lambda request: httpx.Response(400, text="unexpected short page")
    for _ in range(2):
        with pytest.raises(SourceError, match="source_unavailable"):
            service.sources.fetch(source, comments=False)
    with pytest.raises(SourceError, match="source_rate_limited") as error:
        service.sources.fetch(source, comments=False)
    assert INVALID_SECONDS - 5 <= error.value.retry_after <= INVALID_SECONDS == 600
    budget = http.budget()["ehentai"]
    assert budget["penalty"] == 1 and budget["penalty_until"] is None
    assert budget["level"] < level + 10  # not filled to capacity
    assert http.cooldowns()["ehentai"] > 0
    http.save_if_dirty()
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM source_rate_event").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM source_halt").fetchone()[0] == 0
    # Paused: further requests fail fast without reaching the site.
    http.transport.handler = remote
    remote.requests.clear()
    with pytest.raises(SourceError, match="source_rate_limited"):
        service.sources.fetch(source, comments=False)
    assert not remote.requests
    end_halts(http)
    service.sources.fetch(source, comments=False)


def test_short_server_errors_never_pause_the_site(queue):
    """A failing site (short 5xx pages) is a passing failure, not an unrecognized answer."""
    from mangacouch.source_http import gallery_url

    service, _ = queue
    service.http.transport.handler = lambda request: httpx.Response(
        502, text="<html><body><h1>502 Bad Gateway</h1></body></html>"
    )
    for _ in range(5):
        with pytest.raises(SourceError, match="source_unavailable"):
            service.sources.fetch(gallery_url(EH), comments=False)
    assert not service.http.cooldowns()
    assert service.http.budget()["ehentai"]["penalty"] == 1


def test_a_ban_page_still_halts_and_penalizes_after_unrecognized_answers(queue):
    service, _ = queue
    http = service.http
    http.transport.handler = lambda request: httpx.Response(400, text="unexpected short page")
    for _ in range(2):
        with pytest.raises(SourceError, match="source_unavailable"):
            http.read("GET", EH)
    http.transport.handler = lambda request: httpx.Response(
        503, text="This IP address has been temporarily banned. The ban expires in 2 hours"
    )
    with pytest.raises(SourceError, match="source_rate_limited") as error:
        http.read("GET", EH)
    assert error.value.retry_after > 7000
    assert http.budget()["ehentai"]["penalty"] < 1
    with service.library.connection() as db:
        assert [row[0] for row in db.execute("SELECT reason FROM source_halt")] == ["ban"]


@pytest.mark.parametrize("location", ["https://[broken/g/1/", "https://e-hentai.org:99999/g/1/"])
def test_malformed_redirect_location_is_a_network_error(queue, location):
    service, _ = queue
    service.http.transport.handler = lambda request: httpx.Response(302, headers={"Location": location})
    with pytest.raises(SourceError) as error:
        service.http.read("GET", EH)
    assert error.value.code == "source_network" and not error.value.unsent


def test_exhentai_comments_without_cookie_are_read_on_e_hentai(queue):
    service, remote = queue

    def handler(request):
        if request.url.host == "exhentai.org":
            remote.requests.append(request)
            return httpx.Response(200, content=b"")  # ExHentai's blank page for visitors
        return remote(request)

    service.http.transport.handler = handler
    remote.requests.clear()
    source = Source(site="exhentai", gallery_id=100, gallery_token="abcdef")
    observation = service.sources.preferred(source, comments=True)
    assert observation["comments_state"] == "complete" and observation["comments_error"] is None
    assert observation["metadata"]["source"]["site"] == "exhentai"
    assert [comment["body"] for comment in observation["comments"]] == ["Fixture comment"]
    assert not [request for request in remote.requests if request.url.host == "exhentai.org"]
    assert [request.url.path for request in remote.requests if request.url.host == "e-hentai.org"] == [
        "/g/100/abcdef/"
    ]


@pytest.mark.parametrize("page", ["gone", "rate_limited", "failing"])
def test_exhentai_comment_fallback_failure_reports_the_exhentai_error(queue, page):
    """An ExHentai-only gallery is not on e-hentai.org: the comments keep ExHentai's own error
    (log in), not the fallback's; a rate limit of the shared site group stays a rate limit, and a
    failing e-hentai.org stays a source failure rather than asking for a cookie."""
    from source_fixtures import TOO_FAST

    service, remote = queue

    def handler(request):
        if request.url.host == "e-hentai.org" and request.url.path.startswith("/g/"):
            if page == "gone":
                return httpx.Response(404, text="Gallery not available")
            if page == "failing":
                return httpx.Response(503, text="Service Unavailable")
            return httpx.Response(200, content=TOO_FAST)
        return remote(request)

    service.http.transport.handler = handler
    source = Source(site="exhentai", gallery_id=100, gallery_token="abcdef")
    observation = service.sources.preferred(source, comments=True)
    assert observation["comments_state"] == "failed"
    expected = {
        "gone": "source_login_required",
        "rate_limited": "source_rate_limited",
        "failing": "source_unavailable",
    }[page]
    assert observation["comments_error"] == expected


def test_exhentai_comments_with_cookie_stay_on_exhentai(queue):
    from mangacouch.source_config import SourceSettings

    service, remote = queue
    service.save_settings(SourceSettings(), {"ex_cookie": "ipb_member_id=1; igneous=y"}, 0)
    remote.requests.clear()
    source = Source(site="exhentai", gallery_id=100, gallery_token="abcdef")
    observation = service.sources.preferred(source, comments=True)
    assert observation["comments_state"] == "complete"
    assert {request.url.host for request in remote.requests} == {"exhentai.org"}


def test_an_eh_check_failure_is_ignored_once_the_source_changes_to_nh(queue, files, tmp_path):
    from test_sources import local_with_source

    from mangacouch.review import Review

    service, remote = queue
    book = local_with_source(files, tmp_path, service)
    remote.removed = {100}
    assert service.checks.step()
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"]) == ("needs_review", "gallery_unavailable")
    assert service.checks.attention(1, 20) == ([book.id], 1)
    current = files.library.get_book(book.id)
    Review(files).edit(
        book.id,
        current.metadata.model_copy(update={"source": Source(site="nhentai", gallery_id=5)}),
        expected_revision=current.metadata_revision,
    )
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"], check["reasons"]) == ("ignored", None, [])
    assert service.checks.attention(1, 20) == ([], 0)


def test_retrying_a_failed_pasted_link_of_an_nh_book_leaves_its_check_ignored(queue, files, tmp_path):
    """The retry runs the pasted E-Hentai link again; the check of the book's own (nH) source
    settles as ignored instead of a red unsupported_source failure."""
    from helpers import make_zip

    from mangacouch.models import Metadata
    from mangacouch.online import FAILED_TASK
    from mangacouch.review import Review

    service, _ = queue
    book = files.import_file(make_zip(tmp_path / "a.zip")).book
    Review(files).edit(
        book.id, Metadata(title="Kept", source=Source(site="nhentai", gallery_id=200)), expected_revision=1
    )
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO source_check(book_id,state,error_code,reasons_json,updated_at) "
            "VALUES(?,'needs_review','gallery_unavailable','[\"gallery_unavailable\"]',0) "
            "ON CONFLICT(book_id) DO UPDATE SET state='needs_review',error_code='gallery_unavailable'",
            (book.id,),
        )
        db.execute("INSERT INTO source_task VALUES(?,?,'metadata',1,?,0)", (book.id, EH, FAILED_TASK))
    check = service.checks.get(book.id)
    assert check
    service.checks.action(book.id, "retry", check["generation"])
    service.http.transport.handler = lambda request: httpx.Response(503, text="x" * 4000)
    for _ in range(3):
        service.checks.step()
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"], check["reasons"]) == ("ignored", None, [])
    assert service.checks.attention(1, 20) == ([], 0)
    with files.library.connection() as db:
        # The pasted link is still owed, waiting out the passing failure.
        assert db.execute("SELECT count(*) FROM source_task WHERE book_id=?", (book.id,)).fetchone()[0] == 1


def test_a_halt_takes_the_write_lock_once_not_every_second(queue, files, tmp_path, monkeypatch):
    from test_sources import local_with_source

    service, _ = queue
    book = local_with_source(files, tmp_path, service)
    service.http._halt(EH, "http_429", 600)
    transactions = []
    original = service.checks.library.transaction

    def counted(*args, **kwargs):
        transactions.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(service.checks.library, "transaction", counted)
    assert not service.checks.step()
    assert len(transactions) == 1
    check = service.checks.get(book.id)
    assert check and check["error_code"] == "source_rate_limited" and check["retry_at"] > 0
    for _ in range(5):
        assert not service.checks.step()
    assert len(transactions) == 1
