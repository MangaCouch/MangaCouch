"""Archive choices of a download: prepared, resample and invalid archives and unexpected archive pages.

Synthetic responses only: no request leaves the process.
"""

from __future__ import annotations

import io
import zipfile

import httpx
import pytest
from helpers import action, download_step, png, ready
from source_fixtures import ARCHIVE, EH, archiver_posts

from mangacouch.online import OnlineMetadata
from mangacouch.review import Review
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import (
    SourceError,
    SourceHTTP,
    gallery_url,
)

# An archive that cannot be imported is its own error and is never imported again by itself


def damaged_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("1.png", png(10))
        archive.writestr("2.png", png(20)[:40])
        archive.writestr("3.png", png(30))
    return buffer.getvalue()


def test_invalid_archive_keeps_the_file_and_resume_transfers_again_without_paying(queue, files):
    service, remote = queue
    good = remote.payload
    remote.payload = damaged_zip()
    job = ready(service)
    action(service, job, "approve")
    for _ in range(4):
        download_step(service)
    latest = service.get(job["id"], private=True)
    assert latest["state"] == "error" and latest["error_code"] == "download_invalid_archive"
    assert latest["details"]["import_error"] and latest["book_id"] is None
    assert service._path(job["id"]).read_bytes() == remote.payload
    assert files.library.search("") == []
    # Nothing retries by itself: the same bytes would fail the same way.
    assert not download_step(service)
    assert len(archiver_posts(remote)) == 1
    remote.payload = good
    resumed = action(service, latest, "resume")
    assert resumed["state"] == "queued" and resumed["details"]["phase"] == "transfer"
    assert resumed["received"] == 0 and resumed["total"] is None
    for _ in range(3):
        download_step(service)
    done = service.get(job["id"])
    assert done["state"] == "done" and done["book_id"]
    assert len(archiver_posts(remote)) == 1
    downloads = [r for r in remote.requests if r.url.host == "archive.example"]
    assert len(downloads) == 2 and "range" not in downloads[1].headers


# Unrecognized archiver and archive server pages


def test_blank_archiver_page_is_not_offered_with_an_unknown_price(queue):
    service, remote = queue
    original = remote.__call__

    def blank(request):
        if request.url.path == "/archiver.php":
            return httpx.Response(200, text="<html><body>Invalid archiver key</body></html>")
        return original(request)

    service.http.transport.handler = blank
    job = service.create(EH)
    download_step(service)
    latest = service.get(job["id"])
    assert latest["state"] == "error" and latest["error_code"] == "source_protocol"


@pytest.mark.parametrize(
    "headers,body",
    [
        ({"content-type": "image/gif"}, b"GIF89a"),
        (
            {
                "content-type": "application/octet-stream",
                "content-disposition": "inline; filename=sadpanda.jpg",
            },
            b"x",
        ),
        ({"content-type": "text/html"}, b"<html><img src='/img/kokomade.jpg'></html>"),
        ({"content-type": "text/html"}, b""),
    ],
)
def test_exhentai_without_a_session_reports_login_required(headers, body):
    client = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, headers=headers, content=body)),
    )
    try:
        with pytest.raises(SourceError, match="source_login_required"):
            client.read("GET", "https://exhentai.org/archiver.php?gid=1&token=ab")
        # The same answer from E-Hentai or a CDN is just a response.
        assert client.read("GET", "https://e-hentai.org/archiver.php?gid=1&token=ab") == body
    finally:
        client.close()


def transfer_page(service, remote, body: bytes):
    original = remote.__call__

    def handler(request):
        if request.url.host == "archive.example":
            return httpx.Response(200, content=body, headers={"content-type": "text/html; charset=utf-8"})
        return original(request)

    service.http.transport.handler = handler
    job = ready(service)
    action(service, job, "approve")
    download_step(service)
    download_step(service)
    return job, original


def test_archive_page_waits_only_for_a_recognized_preparing_marker(queue):
    service, remote = queue
    job, _ = transfer_page(service, remote, b"<p>Your archive is being prepared. Please wait.</p>")
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["error_code"] == "archive_preparing"
    assert latest["attempts"] == 0


def test_unrecognized_archive_page_moves_to_get_only_recovery(queue):
    service, remote = queue
    job, original = transfer_page(service, remote, b"<html><body>Link expired or invalid</body></html>")
    latest = service.get(job["id"])
    assert latest["state"] == "queued" and latest["error_code"] == "archive_link_invalid"
    assert latest["details"]["phase"] == "recover"
    service.http.transport.handler = original
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET retry_at=0")
    download_step(service)
    assert service.get(job["id"])["state"] == "queued"
    for _ in range(3):
        download_step(service)
    assert service.get(job["id"])["state"] == "done"
    assert len(archiver_posts(remote)) == 1


# A prepared archive of unknown quality is its own choice


@pytest.mark.parametrize("default_quality", ["org", "res"])
def test_prepared_archive_is_offered_as_unknown_quality_not_as_the_default(queue, default_quality):
    service, remote = queue
    service.settings = SourceSettings(default_quality=default_quality)
    remote.reusable = True
    job = ready(service)
    details = job["details"]
    assert details["reusable"] and details["mode"] == "reuse"
    assert details["quote"]["original_gp"] is None and details["quote"]["resample_gp"] is None
    for mode in ("org", "res"):
        with pytest.raises(Exception, match="price is unavailable"):
            action(service, job, "approve", mode=mode)
    action(service, job, "approve", mode="reuse")
    for _ in range(3):
        download_step(service)
    done = service.get(job["id"])
    assert done["state"] == "done" and done["details"]["archive_variant"] == "unknown"
    assert len(archiver_posts(remote)) == 0


def test_prepared_archive_of_unknown_quality_waits_for_approval_even_with_auto_free(queue):
    service, remote = queue
    service.settings = SourceSettings(default_quality="res", auto_free=True)
    remote.reusable = True
    job = service.create(EH)
    for _ in range(4):
        download_step(service)
    waiting = service.get(job["id"])
    assert waiting["state"] == "needs_approval" and waiting["details"]["mode"] == "reuse"
    assert len(archiver_posts(remote)) == 0


def test_reuse_requires_a_prepared_archive(queue):
    service, _ = queue
    job = ready(service)
    with pytest.raises(Exception, match="Invalid archive type"):
        action(service, job, "approve", mode="reuse")


@pytest.mark.parametrize("mode", ["org", "res"])
def test_recovered_link_uses_a_fresh_transfer_and_records_unknown_quality(queue, mode):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve", mode=mode)
    download_step(service)
    service._path(job["id"]).write_bytes(b"stale bytes")
    details = service.get(job["id"], private=True)["details"] | {"etag": "stale-etag"}
    service._update(
        job["id"],
        details=details,
        received=11,
        total=11,
        remote_secret=service.vault.encrypt({"url": ARCHIVE, "expires_at": 1}),
    )
    download_step(service)
    with service.library.transaction() as db:
        db.execute("UPDATE download_job SET retry_at=0")
    download_step(service)
    waiting = service.get(job["id"])
    assert waiting["state"] == "queued" and waiting["details"]["mode"] == "reuse"
    assert waiting["total"] is None and waiting["received"] == 0
    assert "etag" not in waiting["details"]
    assert not [r for r in remote.requests if r.url.host == "archive.example"]
    for _ in range(3):
        download_step(service)
    done = service.get(job["id"])
    assert done["state"] == "done" and done["details"]["archive_variant"] == "unknown"
    assert len(archiver_posts(remote)) == 1
    requests = [r for r in remote.requests if r.url.host == "archive.example"]
    assert len(requests) == 1 and "range" not in requests[0].headers


# A resample archive is never compared with the original's declared size


@pytest.mark.parametrize("mode,flagged", [("org", True), ("res", False)])
def test_resample_download_skips_declared_byte_size_comparison(queue, files, mode, flagged):
    service, remote = queue
    remote.gallery_bytes += 1000  # The resample is smaller than the gallery's original files.
    job = ready(service)
    done = finish_mode(service, job, mode)
    assert done["state"] == "done", done
    book = files.library.get_book(done["book_id"])
    assert book.metadata.declared_byte_size == remote.gallery_bytes
    preview = Review(files).preview(book.id)
    assert ("declared_byte_size_mismatch" in preview["issues"]) is flagged
    if not flagged:
        assert preview["byte_size_basis"] == "resample"
        # A later refresh of the same bytes keeps knowing they are a resample.
        OnlineMetadata(files, service.sources).refresh(book.id, gallery_url(EH), book.metadata_revision)
        assert "declared_byte_size_mismatch" not in Review(files).preview(book.id)["issues"]
        assert files.library.get_book(book.id).metadata.declared_byte_size == remote.gallery_bytes


def finish_mode(service, job, mode):
    action(service, job, "approve", mode=mode)
    for _ in range(5):
        if not download_step(service):
            break
    return service.get(job["id"])


def test_resample_approval_reaches_source_as_resample(queue):
    service, remote = queue
    job = ready(service)
    action(service, job, "approve", mode="res")
    download_step(service)
    submitted = [r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]
    assert len(submitted) == 1 and b"dltype=res" in submitted[0].content
