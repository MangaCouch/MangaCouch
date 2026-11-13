from __future__ import annotations

import httpx
import pytest
from helpers import action, download_step, ready
from helpers import finish_download as finish
from source_fixtures import EH, EX, archiver_posts

from mangacouch.source_config import SourceSettings


@pytest.mark.parametrize(
    "url",
    [
        EX,
        "https://g.e-hentai.org/g/100/abcdef/",
        "https://www.e-hentai.org/g/100/abcdef",
        "https://www.exhentai.org/g/100/ABCDEF/",
    ],
)
def test_eh_and_exhentai_links_of_one_gallery_share_a_job(queue, url):
    service, _ = queue
    first = service.create(EH)
    assert first["outcome"] == "created"
    again = service.create(url)
    assert again["id"] == first["id"] and again["outcome"] == "existing"
    with service.library.connection() as db:
        assert db.execute("SELECT count(*) FROM download_job").fetchone()[0] == 1
    items = service.create_many([EH, url, "https://exhentai.org/g/101/abcdef/"])
    assert items[0]["job"]["id"] == items[1]["job"]["id"] == first["id"]
    assert items[2]["job"]["id"] != first["id"] and items[2]["job"]["outcome"] == "created"


def test_readding_a_done_download_returns_it_and_only_redownload_queues_it_again(queue, files):
    service, remote = queue
    done = finish(service, ready(service))
    assert done["state"] == "done"
    sent = len(remote.requests)
    for url in (EH, EX):
        again = service.create(url)
        assert again["id"] == done["id"] and again["outcome"] == "existing"
        assert again["state"] == "done" and again["book_id"] == done["book_id"]
    assert not download_step(service) and len(remote.requests) == sent
    # Explicit redownload: inspected again, and the book already in the library holds it at approval
    # even though free downloads would start by themselves.
    service.settings = SourceSettings(auto_free=True)
    remote.cost = 0
    remote.prepared.clear()
    queued = action(service, done, "redownload")
    assert queued["state"] == "queued" and queued["book_id"] is None
    assert "duplicate" not in queued["details"]
    download_step(service)
    latest = service.get(done["id"])
    assert latest["state"] == "needs_approval", latest
    assert latest["details"]["library"] == [
        {"id": done["book_id"], "title": "Synthetic source book", "relation": "same"}
    ]
    assert len(archiver_posts(remote)) == 1
    with pytest.raises(Exception, match="not available"):
        action(service, latest, "redownload")


def test_library_hit_waits_for_approval_even_with_auto_free(queue):
    service, remote = queue
    done = finish(service, ready(service))
    service.delete(done["id"])
    service.settings = SourceSettings(auto_free=True)
    remote.cost = 0
    remote.prepared.clear()
    # The ExHentai link of the gallery the library got from E-Hentai.
    job = service.create(EX)
    assert job["outcome"] == "created"
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval"
    assert [item["id"] for item in latest["details"]["library"]] == [done["book_id"]]
    # Without a library hit the same free gallery starts by itself.
    other = service.create("https://e-hentai.org/g/101/abcdef/")
    download_step(service)
    started = service.get(other["id"])
    assert started["state"] == "queued" and started["details"]["phase"] == "submit"
    assert started["details"]["library"] == []
    assert len(archiver_posts(remote)) == 1


def test_newer_edition_lists_the_earlier_book(queue):
    service, remote = queue
    done = finish(service, ready(service))
    original = remote.__call__

    def newer(request):
        response = original(request)
        if request.url.path == "/api.php":
            data = response.json()
            for entry in data["gmetadata"]:
                if entry["gid"] == 105:
                    entry.update(parent_gid="100", parent_key="abcdef")
            return httpx.Response(200, json=data)
        return response

    service.http.transport.handler = newer
    job = service.create("https://e-hentai.org/g/105/abcdef/")
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "needs_approval"
    assert latest["details"]["library"] == [
        {"id": done["book_id"], "title": "Synthetic source book", "relation": "edition"}
    ]


@pytest.mark.parametrize("known", [True, False])
def test_cancelled_retry_keeps_approval_when_library_matches_change(queue, files, known):
    service, remote = queue
    done = finish(service, ready(service))
    service.delete(done["id"])
    remote.prepared.clear()
    job = ready(service, EX)
    assert [item["id"] for item in job["details"]["library"]] == [done["book_id"]]
    approved = action(service, job, "approve")["details"]["approved_gp"]
    if not known:
        # Approved before the book reached the library.
        latest = service.get(job["id"])
        service._update(job["id"], details={**latest["details"], "library": []})
    action(service, job, "cancel")
    remote.cost = 0
    action(service, job, "redownload")
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["details"]["phase"] == "transfer"
    assert latest["details"]["approved_gp"] == approved
