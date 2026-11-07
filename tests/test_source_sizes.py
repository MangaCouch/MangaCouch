from __future__ import annotations

import zipfile

import httpx
import pytest
from helpers import make_zip, png
from source_fixtures import EH, SourceFixture

from mangacouch.models import Metadata, Source
from mangacouch.online import OnlineMetadata
from mangacouch.review import Review
from mangacouch.source_adapters import Sources
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceHTTP, gallery_url


@pytest.mark.parametrize("site", ["ehentai", "exhentai"])
@pytest.mark.parametrize("declared", ["images", "archive", "incorrect_images"])
def test_source_size_compares_uncompressed_images_not_container_or_auxiliary(files, tmp_path, site, declared):
    path = tmp_path / "compressed.zip"
    pages = [png(20 + i) for i in range(3)]
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for i, page in enumerate(pages):
            archive.writestr(f"{i}.png", page)
        archive.writestr("notes.txt", "auxiliary content" * 100)
        archive.writestr("__MACOSX/unused.png", png())
    book = files.import_file(path).book
    image_total = sum(map(len, pages))
    assert book.content.byte_size != image_total
    assert files.image_byte_size(book.id) == image_total
    remote = SourceFixture(path.read_bytes())
    remote.gallery_bytes = {
        "images": image_total,
        "archive": book.content.byte_size,
        "incorrect_images": image_total + 1,
    }[declared]
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote), pace=0)
    try:
        url = EH if site == "ehentai" else EH.replace("e-hentai.org", "exhentai.org")
        OnlineMetadata(files, Sources(http)).refresh(book.id, gallery_url(url), book.metadata_revision)
        preview = Review(files).preview(book.id)
        assert preview["actual_byte_size"] == book.content.byte_size
        assert preview["compared_byte_size"] == image_total
        assert ("declared_byte_size_mismatch" in preview["issues"]) == (declared != "images")
        assert files.library.get_book(book.id).review_state == (
            "confirmed" if declared == "images" else "needs_review"
        )
        # Missing media must not turn an unverifiable total into a successful comparison.
        files.root.path.rename(tmp_path / "offline")
        unavailable = Review(files).preview(book.id)
        assert unavailable["compared_byte_size"] is None
        assert "archive_unavailable" in unavailable["issues"]
    finally:
        http.close()


def test_source_check_unexpected_failure_exits_running_and_logs_traceback(
    queue, files, tmp_path, monkeypatch, caplog
):
    service, _ = queue
    book = files.import_file(make_zip(tmp_path / "source-check.zip")).book
    Review(files).edit(book.id, Metadata(source=Source(site="nhentai", gallery_id=200)), expected_revision=1)
    with files.library.transaction() as db:
        db.execute("UPDATE source_check SET state='queued' WHERE book_id=?", (book.id,))

    def fail(*args, **kwargs):
        raise RuntimeError("injected source failure")

    monkeypatch.setattr(service.online, "refresh", fail)
    assert service.checks.step()
    result = service.checks.get(book.id)
    assert result and result["state"] == "needs_review" and result["error_code"] == "source_check_failed"
    assert not service.checks.step()
    record = next(record for record in caplog.records if book.id in record.message)
    assert record.exc_info and "injected source failure" in caplog.text
