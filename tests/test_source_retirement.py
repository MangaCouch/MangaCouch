from __future__ import annotations

import httpx
import pytest
from helpers import make_zip
from source_fixtures import EH, NH

from mangacouch.errors import InvalidInput
from mangacouch.models import Metadata, Source
from mangacouch.review import Review
from mangacouch.source_checks import enqueue_check
from mangacouch.source_http import SourceError, gallery_url


@pytest.mark.parametrize("host", ["nhentai.net", "www.nhentai.net", "t.nhentai.net", "i.nhentai.net."])
def test_removed_source_and_redirects_never_reach_the_network(queue, host):
    service, remote = queue
    target = f"https://{host}/g/200/"
    with pytest.raises(SourceError, match="unsupported_source") as caught:
        service.http.read("GET", target)
    assert caught.value.unsent and not remote.requests
    seen = []

    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    service.http.transport.handler = redirect
    with pytest.raises(SourceError, match="unsupported_source"):
        service.http.read("GET", EH)
    assert seen == [EH]


@pytest.mark.parametrize("operation", ["fetch", "preferred", "quote", "submit", "verify"])
def test_removed_adapter_operations_fail_without_requests(queue, operation):
    service, remote = queue
    source = Source(site="nhentai", gallery_id=200)
    with pytest.raises(SourceError, match="unsupported_source"):
        if operation == "verify":
            service.sources.verify(source.site)
        elif operation == "submit":
            service.sources.submit(source, "org")
        else:
            getattr(service.sources, operation)(source)
    assert not remote.requests


def test_removed_download_and_credentials_are_rejected(queue, api):
    service, remote = queue
    with pytest.raises(ValueError):
        gallery_url(NH)
    with pytest.raises(InvalidInput, match="gallery URL"):
        service.create(NH)
    assert api.post("/api/downloads", json={"url": NH}).status_code == 422
    assert api.post("/api/sources/verify/nhentai").status_code == 422
    assert api.get("/api/sources/settings").json()["configured"].keys() == {"eh_cookie", "ex_cookie", "proxy"}
    with pytest.raises(InvalidInput, match="Unknown source credential"):
        service.save_settings(service.settings, {"nh_key": "removed"}, 0)
    assert not remote.requests


def test_stored_nh_metadata_stays_local_and_cannot_be_refreshed(queue, files, tmp_path):
    service, remote = queue
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    source = Source(site="nhentai", gallery_id=200)
    book = Review(files).edit(book.id, Metadata(title="Preserved", source=source), expected_revision=1)
    with files.library.transaction() as db:
        enqueue_check(db, book.id)
    check = service.checks.get(book.id)
    assert check and check["state"] == "ignored" and check["error_code"] is None
    assert service.checks.schedule_refresh() == service.checks.retry_failed() == 0
    with pytest.raises(InvalidInput, match="no longer supported"):
        service.checks.action(book.id, "retry", check["generation"])
    assert not service.checks.step()
    assert files.library.get_book(book.id).metadata == book.metadata
    assert not remote.requests
