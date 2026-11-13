from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, PASSCODE, finish_download, login, ready
from helpers import finish_download as finish

from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.downloads import RETENTION_SECONDS, settle
from mangacouch.http import create_app


def details(queue, identifier: str) -> dict:
    return queue.get(identifier, private=True)["details"]


def test_a_settled_download_keeps_no_comments_or_gdata(queue):
    service, _remote = queue
    job = ready(service)
    with service.library.transaction() as db:
        db.execute(
            "UPDATE download_job SET details_json=json_set(details_json,'$.selected.gdata',json(?))",
            (json.dumps({"gid": 100}),),
        )
    done = finish_download(service, job)
    assert done["state"] == "done"
    selected = details(service, done["id"])["selected"]
    assert "gdata" not in selected and selected["comments"] == []
    assert selected["comments_state"] in {"not_requested", "complete", "failed"}
    assert selected["metadata"]["title"]


def test_settle_keeps_comments_out_of_a_resumed_import():
    observation = {
        "metadata": {"title": "T"},
        "comments": [{"comment_id": "1"}],
        "comments_state": "complete",
        "comments_error": None,
        "gdata": {"gid": 1},
    }
    settled = settle({"selected": dict(observation)})
    for item in (settled["selected"],):
        assert item == {
            "metadata": {"title": "T"},
            "comments": [],
            "comments_state": "not_requested",
            "comments_error": None,
        }


def test_settled_downloads_are_forgotten_after_the_retention_period(queue):
    service, _remote = queue
    done = finish_download(service, ready(service))
    waiting = service.create("https://e-hentai.org/g/999/abcdef0123/")
    with service.library.transaction() as db:
        db.execute(
            "UPDATE download_job SET updated_at=?,"
            "details_json=json_set(details_json,'$.selected.gdata',json('{}'))",
            (int(time.time()) - RETENTION_SECONDS - 1,),
        )
    assert service.purge() == 1
    with service.library.connection() as db:
        assert [row[0] for row in db.execute("SELECT id FROM download_job")] == [waiting["id"]]
    assert done["book_id"]


def test_import_failures_after_publication_do_not_fail_the_download(queue, monkeypatch):
    service, _remote = queue

    def unavailable(*_args, **_kwargs):
        raise OSError("source note storage failed")

    monkeypatch.setattr(service.online, "apply", unavailable)
    done = finish_download(service, ready(service))
    assert done["state"] == "done" and done["book_id"]
    assert details(service, done["id"])["warnings"] == ["source_metadata_not_saved"]
    assert service.library.get_book(done["book_id"])


def test_cancellation_is_seen_without_reading_the_job_for_every_chunk(queue, monkeypatch):
    service, _remote = queue
    job = service.create("https://e-hentai.org/g/999/abcdef0123/")
    reads = []
    connection = service.library.connection

    def counted(*args, **kwargs):
        reads.append(1)
        return connection(*args, **kwargs)

    monkeypatch.setattr(service.library, "connection", counted)
    assert not any(service._cancelled(job["id"]) for _ in range(100))
    assert len(reads) == 0
    service.interrupted.add(job["id"])
    assert service._cancelled(job["id"])
    service.interrupted.clear()
    assert not service._cancelled(job["id"])


def test_history_paginates_and_clear_keeps_books_and_all_pending_states(queue):
    service, _ = queue
    done = finish(service, ready(service))
    book = service.library.get_book(done["book_id"])
    personal = book.personal.model_dump()
    retained = []
    for i, state in enumerate(
        [
            "queued",
            "needs_approval",
            "paused",
            "error",
            "downloading",
        ]
    ):
        job = service.create(f"https://e-hentai.org/g/{1000 + i}/abc/")
        service._update(job["id"], state=state)
        retained.append(job["id"])
    for i in range(44):
        job = service.create(f"https://e-hentai.org/g/{2000 + i}/abc/")
        service._update(job["id"], state="cancelled")
    first = service.list(scope="history")
    second = service.list(first["after"], scope="history")
    third = service.list(second["after"], scope="history")
    ids = [item["id"] for page in [first, second, third] for item in page["items"]]
    assert len(ids) == len(set(ids)) == 45
    assert len(first["items"]) == len(second["items"]) == 20 and third["after"] is None
    assert service.list(scope="active")["total"] == len(retained)
    assert {item["id"] for item in service.list(scope="active")["items"]} == set(retained)
    assert service.library.get_book(book.id).personal.model_dump() == personal
    assert service.files.page_bytes(book.id, 0).startswith(b"\x89PNG")


def test_download_listing_validates_page_size(queue):
    service, _ = queue
    auth = Auth(service.library)
    auth.initialize(PASSCODE)
    with TestClient(
        create_app(service.files, HTTPSettings(public_origin=ORIGIN), downloads=service), base_url=ORIGIN
    ) as client:
        assert client.get("/api/downloads", headers=CSRF).status_code == 401
        login(client)
        assert client.get("/api/downloads?limit=0").status_code == 422
