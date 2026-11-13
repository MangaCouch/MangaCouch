"""Source work a rate limit put off (source_task) runs later by itself, with nobody asking again."""

from __future__ import annotations

import time

import httpx
from helpers import make_zip
from source_fixtures import EH, TOO_FAST, end_halts

from mangacouch.source_http import gallery_url

PAGE_URL = "https://e-hentai.org/g/1/abcdef0123/"


def gallery_page(remote, body: bytes):
    """Gallery pages (comments) answer `body`; the API and everything else stay normal."""
    original = remote.__call__

    def handler(request):
        if request.url.path.startswith("/g/"):
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        return original(request)

    return handler


def tasks(files) -> list[tuple]:
    with files.library.connection() as db:
        return [
            tuple(row)
            for row in db.execute(
                "SELECT book_id,source_url,kind,manual,retry_at FROM source_task ORDER BY kind"
            )
        ]


def due(files) -> None:
    """Let the queued tasks' time come."""
    with files.library.transaction() as db:
        db.execute("UPDATE source_task SET retry_at=0")


def comments(service, book_id: str) -> list[str]:
    return [comment["body"] for comment in service.online.notes(book_id)["comments"]]


def new_book(files, tmp_path, name="book"):
    return files.import_file(make_zip(tmp_path / f"{name}.zip")).book


def test_comments_a_rate_limit_kept_are_fetched_later_by_themselves(queue, files, tmp_path):
    service, remote = queue
    http = service.http
    book = new_book(files, tmp_path)
    http.transport.handler = gallery_page(remote, TOO_FAST)
    result = service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    # The metadata is saved; the comments are owed, due when the warning's halt ends.
    assert result["comments_error"] == "source_rate_limited"
    assert service.library.get_book(book.id).metadata.source == gallery_url(EH)
    [(book_id, url, kind, manual, retry_at)] = tasks(files)
    assert (book_id, url, kind, manual) == (book.id, gallery_url(EH).url, "comments", 0)
    assert retry_at >= time.time() + 590
    assert not service.checks.step()  # not due yet

    # Due, but still rate limited: put off again, not dropped.
    end_halts(http)
    due(files)
    assert service.checks.step()
    assert [task[2] for task in tasks(files)] == ["comments"]
    assert tasks(files)[0][4] >= time.time() + 590

    end_halts(http)
    http.transport.handler = remote
    remote.comment_body = "Fetched later"
    due(files)
    assert service.checks.step()
    assert tasks(files) == []
    assert comments(service, book.id) == ["Fetched later"]
    assert not service.checks.step()


def test_a_fetch_that_reads_the_comments_settles_what_was_owed(queue, files, tmp_path):
    service, remote = queue
    book = new_book(files, tmp_path)
    service.http.transport.handler = gallery_page(remote, TOO_FAST)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    assert len(tasks(files)) == 1
    end_halts(service.http)
    service.http.transport.handler = remote
    revision = service.library.get_book(book.id).metadata_revision
    assert service.online.refresh(book.id, gallery_url(EH), revision)["comments_error"] is None
    assert tasks(files) == []


def test_a_refresh_asked_for_during_a_rate_limit_is_queued_and_finishes_by_itself(
    api, queue, files, tmp_path
):
    service, _ = queue
    book = new_book(files, tmp_path)
    service.http._halt(PAGE_URL, "http_429", 600)
    response = api.post(
        f"/api/books/{book.id}/source", params={"expected_revision": book.metadata_revision}, json={"url": EH}
    )
    assert response.status_code == 202
    body = response.json()
    assert body["retry_at"] >= time.time() + 590 and body["book"]["id"] == book.id
    assert [task[2:4] for task in tasks(files)] == [("metadata", 1)]
    assert service.library.get_book(book.id).metadata.source is None

    end_halts(service.http)
    due(files)
    assert service.checks.step()
    assert tasks(files) == []
    refreshed = service.library.get_book(book.id)
    assert refreshed.metadata.source == gallery_url(EH)
    assert comments(service, book.id) == ["Fixture comment"]
    check = service.checks.get(book.id)
    assert check and check["state"] in {"done", "needs_review"} and check["error_code"] is None


def test_a_queued_refresh_that_fails_otherwise_leaves_its_book_needing_review(api, queue, files, tmp_path):
    service, remote = queue
    book = new_book(files, tmp_path)
    service.http._halt(PAGE_URL, "http_429", 600)
    response = api.post(
        f"/api/books/{book.id}/source", params={"expected_revision": book.metadata_revision}, json={"url": EH}
    )
    assert response.status_code == 202
    end_halts(service.http)
    service.http.transport.handler = lambda request: httpx.Response(404)  # the gallery is gone
    due(files)
    assert service.checks.step()
    assert len(tasks(files)) == 1 and tasks(files)[0][-1] > 2**31
    check = service.checks.get(book.id)
    assert check and (check["state"], check["error_code"]) == ("needs_review", "source_unavailable")
    assert check["reasons"] == ["source_unavailable"]


def test_comments_asked_for_during_a_rate_limit_are_queued_and_fetched_by_themselves(
    api, queue, files, tmp_path
):
    service, remote = queue
    book = new_book(files, tmp_path)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    service.http._halt(PAGE_URL, "http_429", 600)
    response = api.post(f"/api/books/{book.id}/source-comments", json={"url": EH})
    assert response.status_code == 202
    body = response.json()
    assert body["retry_at"] >= time.time() + 590
    assert [comment["body"] for comment in body["comments"]] == ["Fixture comment"]
    assert [task[2:4] for task in tasks(files)] == [("comments", 1)]

    end_halts(service.http)
    remote.comment_body = "Fetched later"
    due(files)
    assert service.checks.step()
    assert tasks(files) == [] and comments(service, book.id) == ["Fetched later"]


def test_queued_work_waits_for_its_time_and_goes_away_with_its_book(api, queue, files, tmp_path):
    service, _ = queue
    book = new_book(files, tmp_path)
    service.http._halt(PAGE_URL, "http_429", 600)
    api.post(
        f"/api/books/{book.id}/source", params={"expected_revision": book.metadata_revision}, json={"url": EH}
    )
    assert len(tasks(files)) == 1
    end_halts(service.http)
    assert not service.checks.step()  # waits for its retry_at even once the halt is gone
    with files.library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (book.id,))
    assert tasks(files) == []


def test_a_queued_refresh_retries_passing_failures_beyond_the_old_expiry(api, queue, files, tmp_path):
    service, _ = queue
    book = new_book(files, tmp_path)
    service.http._halt(PAGE_URL, "http_429", 600)
    api.post(
        f"/api/books/{book.id}/source", params={"expected_revision": book.metadata_revision}, json={"url": EH}
    )
    end_halts(service.http)

    def offline(_request):
        raise httpx.ConnectError("down")

    service.http.transport.handler = offline
    due(files)
    assert service.checks.step()
    # No connection: tried again in a minute or more, the book untouched.
    [(_, _, kind, _, retry_at)] = tasks(files)
    assert kind == "metadata" and retry_at >= time.time() + 55
    assert service.checks.get(book.id) is None
    with files.library.transaction() as db:
        # Past the six hours after which a task once expired.
        db.execute("UPDATE source_task SET retry_at=0,created_at=created_at-?", (6 * 3600,))
    assert service.checks.step()
    assert len(tasks(files)) == 1
    assert service.checks.get(book.id) is None


def test_a_failed_queued_comment_fetch_shows_with_the_comments_until_one_works(api, queue, files, tmp_path):
    service, remote = queue
    book = new_book(files, tmp_path)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    service.http._halt(PAGE_URL, "http_429", 600)
    assert api.post(f"/api/books/{book.id}/source-comments", json={"url": EH}).status_code == 202
    end_halts(service.http)
    service.http.transport.handler = lambda request: httpx.Response(404)  # the gallery is gone
    due(files)
    assert service.checks.step()
    assert tasks(files) == []
    [note] = api.get(f"/api/books/{book.id}/source-comments").json()["notes"]
    assert note["comments_failure"]["code"] == "source_unavailable"
    assert note["comments_failure"]["at"] >= time.time() - 5
    # A refresh that skips comments keeps it; comments fetched again clear it.
    service.http.transport.handler = remote
    revision = service.library.get_book(book.id).metadata_revision
    service.online.refresh(book.id, gallery_url(EH), revision, comments=False)
    [note] = api.get(f"/api/books/{book.id}/source-comments").json()["notes"]
    assert note["comments_failure"]["code"] == "source_unavailable"
    assert api.post(f"/api/books/{book.id}/source-comments", json={"url": EH}).status_code == 200
    [note] = api.get(f"/api/books/{book.id}/source-comments").json()["notes"]
    assert "comments_failure" not in note


def test_a_failed_queued_comment_fetch_shows_without_stored_notes(api, queue, files, tmp_path):
    service, remote = queue
    book = new_book(files, tmp_path)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    assert service.library.get_book(book.id).metadata.source == gallery_url(EH)
    with files.library.transaction() as db:
        db.execute("DELETE FROM source_note WHERE book_id=?", (book.id,))
    service.http._halt(PAGE_URL, "http_429", 600)
    assert api.post(f"/api/books/{book.id}/source-comments", json={"url": EH}).status_code == 202
    end_halts(service.http)
    service.http.transport.handler = lambda request: httpx.Response(404)
    due(files)
    assert service.checks.step()
    assert tasks(files) == []
    [note] = api.get(f"/api/books/{book.id}/source-comments").json()["notes"]
    assert note["source"] == gallery_url(EH).model_dump(mode="json") and note["fetched_at"] is None
    assert note["comments_failure"]["code"] == "source_unavailable"
    service.http.transport.handler = remote
    assert api.post(f"/api/books/{book.id}/source-comments", json={"url": EH}).status_code == 200
    [note] = api.get(f"/api/books/{book.id}/source-comments").json()["notes"]
    assert "comments_failure" not in note and note["fetched_at"]


class ExpiredDeadline:
    """`time` for source_api whose request deadline has run out by its first check (a bucket
    wait of 20 to 60 seconds, or a slow answer)."""

    def __init__(self):
        self.calls = 0

    def monotonic(self) -> float:
        self.calls += 1
        return 0.0 if self.calls == 1 else 1000.0

    @staticmethod
    def time() -> float:
        return time.time()


def test_a_refresh_whose_request_deadline_runs_out_is_queued_like_a_rate_limit(
    api, queue, files, tmp_path, monkeypatch
):
    import mangacouch.source_api as source_api

    service, _ = queue
    book = new_book(files, tmp_path)
    monkeypatch.setattr(source_api, "time", ExpiredDeadline())
    response = api.post(
        f"/api/books/{book.id}/source", params={"expected_revision": book.metadata_revision}, json={"url": EH}
    )
    assert response.status_code == 202
    assert response.json()["retry_at"] >= time.time() + 50
    assert [task[2:4] for task in tasks(files)] == [("metadata", 1)]
    monkeypatch.undo()
    due(files)
    assert service.checks.step()
    assert tasks(files) == []
    assert service.library.get_book(book.id).metadata.source == gallery_url(EH)


def test_comments_whose_request_deadline_runs_out_are_queued_like_a_rate_limit(
    api, queue, files, tmp_path, monkeypatch
):
    import mangacouch.source_api as source_api

    service, remote = queue
    book = new_book(files, tmp_path)
    service.online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    monkeypatch.setattr(source_api, "time", ExpiredDeadline())
    response = api.post(f"/api/books/{book.id}/source-comments", json={"url": EH})
    assert response.status_code == 202
    assert response.json()["retry_at"] >= time.time() + 50
    assert [task[2:4] for task in tasks(files)] == [("comments", 1)]
    monkeypatch.undo()
    remote.comment_body = "Fetched later"
    due(files)
    assert service.checks.step()
    assert tasks(files) == [] and comments(service, book.id) == ["Fetched later"]


def test_a_credential_check_whose_deadline_runs_out_still_reports_source_timeout(api, monkeypatch):
    """Only manual refreshes go to the background; checking credentials just says it timed out."""
    import mangacouch.source_api as source_api

    monkeypatch.setattr(source_api, "time", ExpiredDeadline())
    response = api.post("/api/sources/verify/ehentai", json={})
    assert response.status_code == 200
    assert response.json() == {"reachable": False, "authenticated": False, "detail": "source_timeout"}
