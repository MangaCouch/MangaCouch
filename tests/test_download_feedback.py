from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, EH, ORIGIN, PASSCODE, action, login, ready
from helpers import finish_download as finish

from mangacouch.auth import Auth
from mangacouch.database import RevisionConflict
from mangacouch.http import HTTPSettings, create_app
from mangacouch.models import SourceNotes
from mangacouch.source_adapters import parse_eh_comments
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import gallery_url


def test_saved_settings_apply_to_requests_without_closing_an_active_stream(queue):
    service, remote = queue
    old_client = service.http.client
    with service.http.stream("GET", EH) as response:
        result = service.save_settings(
            SourceSettings(user_agent="new-agent", comments=False), {"eh_cookie": "new-cookie"}, 0
        )
        assert result["revision"] == 1
        # Header-only changes reuse the client; only a proxy change needs a new one.
        assert service.http.client is old_client
        replacement = httpx.Client(transport=service.http.transport)
        service.http.apply(service.http.settings, service.http.secrets, replacement)
        assert not old_client.is_closed
        assert response.read()
        service.http.read("GET", EH)
        assert remote.requests[-1].headers["Cookie"] == "new-cookie"
        assert remote.requests[-1].headers["User-Agent"] == "new-agent"
        assert not service.settings.comments
    assert old_client.is_closed
    with pytest.raises(RevisionConflict):
        service.save_settings(SourceSettings(), {}, 0)
    assert service.http.secrets["eh_cookie"] == "new-cookie"
    with pytest.raises(ValueError):
        service.save_settings(SourceSettings(), {"unexpected": "value"}, 1)
    assert service.config_revision == service.vault.load()[2] == 1
    service.save_settings(SourceSettings(), {"eh_cookie": ""}, 1)
    service.http.read("GET", EH)
    assert "Cookie" not in remote.requests[-1].headers


def test_unchanged_settings_save_is_a_no_op_and_proxy_is_validated_first(queue):
    service, _ = queue
    service.save_settings(SourceSettings(comments=False), {"eh_cookie": "cookie"}, 0)
    client = service.http.client
    # Same values from a stale tab: no revision bump, no conflict, no client rebuild.
    result = service.save_settings(SourceSettings(comments=False), {"eh_cookie": "cookie"}, 0)
    assert result["revision"] == 1 and service.http.client is client
    with pytest.raises(ValueError, match="proxy"):
        service.save_settings(SourceSettings(), {"proxy": "ftp://proxy.example"}, 1)
    assert service.http.client is client and service.vault.load()[2] == 1


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
            "needs_attention",
            "needs_choice",
            "downloading",
        ]
    ):
        job = service.create(f"https://e-hentai.org/g/{1000 + i}/abc/")
        service._update(job["id"], state=state)
        retained.append(job["id"])
    for i in range(44):
        job = service.create(f"https://e-hentai.org/g/{2000 + i}/abc/")
        service._update(job["id"], state="cancelled" if i % 2 else "skipped")
    first = service.list(scope="history")
    second = service.list(first["before"], scope="history")
    third = service.list(second["before"], scope="history")
    ids = [item["id"] for page in [first, second, third] for item in page["items"]]
    assert len(ids) == len(set(ids)) == 45
    assert len(first["items"]) == len(second["items"]) == 20 and third["before"] is None
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


def test_speed_drops_to_zero_when_stalled_or_finished(queue, monkeypatch):
    service, _ = queue
    job = service.create(EH)
    service._update(job["id"], state="downloading")
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    service._progress(job["id"], 1024)
    clock[0] += 0.5
    service._progress(job["id"], 3072)
    assert service.get(job["id"])["bytes_per_second"] == 4096
    clock[0] += 5
    assert service.get(job["id"])["bytes_per_second"] == 0
    service._update(job["id"], state="paused")
    assert service.get(job["id"])["bytes_per_second"] == 0


def test_resample_approval_reaches_source_as_resample(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve", mode="res")
    service.step()
    submitted = [r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]
    assert len(submitted) == 1 and b"dltype=res" in submitted[0].content


def test_uploader_comment_role_survives_comment_storage(files, tmp_path):
    from helpers import make_zip

    from mangacouch.online import OnlineMetadata, store_notes
    from mangacouch.source_adapters import Sources

    html = """<div id="cdiv"><div class="c1"><div class="c3">
      Posted on 1 January 2026, 12:00 by: <a>author</a></div>
      <div class="c4">Uploader Comment</div><div class="c6" id="comment_0">Hello</div></div>
      <div class="c1"><div class="c3"><a>reader</a></div>
      <div class="c6" id="comment_1">Uploader Comment</div></div></div>"""
    comments = parse_eh_comments(html, gallery_url(EH))
    assert [c.is_uploader for c in comments] == [True, False]
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    notes = SourceNotes(source=gallery_url(EH), tags=[], fetched_at=1)
    with files.library.transaction() as db:
        store_notes(
            db,
            book.id,
            {
                "notes": notes.model_dump(mode="json"),
                "comments": [c.model_dump(mode="json") for c in comments],
            },
        )
    stored = OnlineMetadata(files, Sources(None)).notes(book.id)["comments"]  # type: ignore[arg-type]
    assert [c["is_uploader"] for c in stored] == [True, False]
