from __future__ import annotations

import httpx
import pytest
from helpers import make_zip
from source_fixtures import EH, TOO_FAST, SourceFixture, end_halts

from mangacouch.database import RevisionConflict
from mangacouch.models import SourceComment, SourceNotes
from mangacouch.online import OnlineMetadata
from mangacouch.review import Review
from mangacouch.source_adapters import Sources, bounded_comments, parse_eh_comments
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import (
    SourceError,
    SourceHTTP,
    gallery_url,
)


@pytest.fixture
def linked(files, tmp_path):
    remote = SourceFixture(make_zip(tmp_path / "source.zip").read_bytes())
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote))
    online = OnlineMetadata(files, Sources(http))
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    yield online, http, remote, book.id
    http.close()


def refresh(online, book_id, url=EH):
    revision = online.library.get_book(book_id).metadata_revision
    result = online.refresh(book_id, gallery_url(url), revision)
    return result["comments_error"], [c["body"] for c in online.notes(book_id)["comments"]]


def gallery_page(remote, body: bytes):
    original = remote.__call__

    def handler(request):
        if request.url.path.startswith("/g/"):
            return httpx.Response(200, content=body, headers={"content-type": "text/html"})
        return original(request)

    return handler


def test_comment_storage_distinguishes_not_requested_failed_and_complete(linked):
    online, http, remote, book_id = linked
    assert refresh(online, book_id) == (None, ["Fixture comment"])
    # Comments disabled: nothing was fetched, so nothing is removed.
    http.apply(SourceSettings(comments=False), http.secrets)
    remote.comment_body = "Changed while disabled"
    pages = len([r for r in remote.requests if r.url.path.startswith("/g/")])
    assert refresh(online, book_id) == (None, ["Fixture comment"])
    assert len([r for r in remote.requests if r.url.path.startswith("/g/")]) == pages
    http.apply(SourceSettings(), http.secrets)
    # The "too fast" warning page: a failed fetch that also starts the cooldown.
    http.transport.handler = gallery_page(remote, TOO_FAST)
    assert refresh(online, book_id) == ("source_rate_limited", ["Fixture comment"])
    end_halts(http)
    # A blank or unrelated page has no comment section: failed, not empty.
    http.transport.handler = gallery_page(remote, b"<html><body></body></html>")
    assert refresh(online, book_id) == ("source_protocol", ["Fixture comment"])
    # Re-enabled with a normal page: the fresh comments replace the stored ones.
    http.transport.handler = remote
    assert refresh(online, book_id) == (None, ["Changed while disabled"])
    # A gallery whose comment section really is empty clears them.
    http.transport.handler = gallery_page(remote, b'<div id="cdiv" class="gm"></div>')
    assert refresh(online, book_id) == (None, [])
    notes = online.notes(book_id)["notes"]
    assert notes[0]["comments_complete"] is True


def test_explicit_comment_refresh_never_clears_on_a_warning_page(linked):
    online, http, remote, book_id = linked
    http.transport.handler = gallery_page(remote, TOO_FAST)
    with pytest.raises(SourceError, match="source_rate_limited"):
        online.refresh_comments(book_id, gallery_url(EH))
    assert [c["body"] for c in online.notes(book_id)["comments"]] == ["Fixture comment"]


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
                "comments_state": "complete",
            },
        )
    stored = OnlineMetadata(files, Sources(None)).notes(book.id)["comments"]  # type: ignore[arg-type]
    assert [c["is_uploader"] for c in stored] == [True, False]


def test_comment_bytes_are_bounded():
    comments = [
        SourceComment(source=gallery_url(EH), comment_id=str(i), author="x", body="中" * 8192)
        for i in range(250)
    ]
    kept, complete = bounded_comments(comments)
    assert len(kept) <= 5 and not complete


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
