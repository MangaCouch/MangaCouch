from __future__ import annotations

import base64
from xml.etree import ElementTree as ET

import httpx
import pytest
from helpers import make_zip, manager_client, save_metadata

from mangacouch.auth import Auth
from mangacouch.models import Metadata, PersonalState
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceError, SourceHTTP


def basic(auth, kind="management"):
    _, key = auth.create_key(kind, "client acceptance")
    return {"Authorization": "Basic " + base64.b64encode(("reader:" + key).encode()).decode()}


def test_pse_progress_timestamp_is_real_and_rating_does_not_change_it(
    client, auth, files, tmp_path, monkeypatch
):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    headers = basic(auth, "readonly")

    def stream():
        root = ET.fromstring(client.get("/opds/books", headers=headers).content)
        return next(
            item for item in root.iter() if item.get("rel") == "http://vaemendis.net/opds-pse/stream"
        ).attrib

    ns = "{http://vaemendis.net/opds-pse/ns}"
    assert ns + "lastRead" not in stream()
    monkeypatch.setattr("mangacouch.database.time.time", lambda: 1700000000)
    book = files.library.save_personal(book.id, PersonalState(resume_page=1), expected_revision=1)
    value = stream()
    assert value[ns + "lastRead"] == "2" and value[ns + "lastReadDate"].startswith("2023-11-14T22:13:20")
    monkeypatch.setattr("mangacouch.database.time.time", lambda: 1800000000)
    files.library.save_personal(
        book.id, book.personal.model_copy(update={"rating": 4.5}), expected_revision=book.personal_revision
    )
    assert stream() == value
    assert client.get(f"/opds/books/{book.id}/pages/0", headers=headers).status_code == 200
    assert stream() == value


def test_komga_auth_read_download_progress_and_cross_client_conflict(client, auth, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    files.library.save_personal(book.id, PersonalState(rating=4.5, bookmarks=[0]), expected_revision=1)
    headers = basic(auth)
    other = basic(auth)
    readonly = basic(auth, "readonly")
    path = f"/komga/api/v1/books/{book.id}"
    challenge = client.patch(path + "/read-progress", json={"page": 1})
    assert challenge.status_code == 401
    assert challenge.headers["www-authenticate"].startswith("Basic ")
    assert client.get("/komga/api/v1/users/me").status_code == 401
    assert client.get("/komga/api/v1/users/me", headers=headers).json()["roles"] == [
        "USER",
        "PAGE_STREAMING",
        "FILE_DOWNLOAD",
    ]
    assert client.get("/komga/api/v1/libraries", headers=headers).json()[0]["id"] == "mangacouch"
    listing = client.get("/komga/api/v1/series", headers=headers).json()
    assert listing["totalElements"] == 1 and listing["content"][0]["id"] == book.id
    assert client.patch(path + "/read-progress", headers=headers, json={"page": 1}).status_code == 409
    for credential in (headers, other, readonly):
        result = client.get(path, headers=credential)
        assert result.status_code == 200 and result.json()["readProgress"] is None
    assert client.get(path + "/pages", headers=headers).json()[0]["number"] == 1
    assert client.get(path + "/pages/1", headers=headers).content == files.page_bytes(book.id, 0)
    archive = client.get(path + "/file", headers=headers)
    assert archive.status_code == 200 and archive.content == (tmp_path / "book.zip").read_bytes()
    assert client.patch(path + "/read-progress", headers=readonly, json={"page": 2}).status_code == 403
    assert (
        client.patch(
            path + "/read-progress", headers=headers, json={"page": 2, "completed": False}
        ).status_code
        == 204
    )
    saved = files.library.get_book(book.id)
    assert (
        saved.personal.resume_page == 1 and saved.personal.rating == 4.5 and saved.personal.bookmarks == [0]
    )
    assert client.patch(path + "/read-progress", headers=other, json={"page": 1}).status_code == 409
    assert client.get(path, headers=other).json()["readProgress"]["page"] == 2
    assert client.patch(path + "/read-progress", headers=other, json={"page": 1}).status_code == 204
    assert client.get("/api/books", headers=headers).status_code == 401


def test_client_catalog_variants_keep_search_pagination_and_archive_links(client, auth, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "catalog.zip")).book
    headers = basic(auth)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    assert client.get("/opds/basic/catalog", headers=headers).status_code == 404
    for prefix, streaming in (("/opds", True), ("/komga/opds/v1.2", True)):
        root = client.get(prefix + "/catalog", headers=headers)
        assert root.status_code == 200
        feed = ET.fromstring(client.get(prefix + "/books?limit=1", headers=headers).content)
        entry = feed.find("a:entry", ns)
        assert entry is not None
        links = {link.get("rel"): link.get("href") for link in entry.findall("a:link", ns)}
        assert ("http://vaemendis.net/opds-pse/stream" in links) == streaming
        assert (
            client.get(links["http://opds-spec.org/acquisition"], headers=headers).content
            == (tmp_path / "catalog.zip").read_bytes()
        )
        next_link = feed.find("a:link[@rel='next']", ns)
        assert next_link is not None
        next_url = next_link.get("href")
        assert next_url is not None
        assert prefix + "/books?" in next_url
        assert not ET.fromstring(client.get(next_url, headers=headers).content).findall("a:entry", ns)
        search = client.get(prefix + "/search.xml", headers=headers)
        assert prefix + "/books?query=" in search.text
        identifier = entry.find("a:id", ns)
        expected_id = book.id if prefix.startswith("/komga") else f"urn:uuid:{book.id}"
        assert identifier is not None and identifier.text == expected_id


def test_completion_changes_read_activity_but_not_bookmarks(client, auth, files, tmp_path, monkeypatch):
    book = files.import_file(make_zip(tmp_path / "complete.zip")).book
    monkeypatch.setattr("mangacouch.database.time.time", lambda: 1700000000)
    book = files.library.save_personal(book.id, PersonalState(resume_page=0), expected_revision=1)
    monkeypatch.setattr("mangacouch.database.time.time", lambda: 1800000000)
    book = files.library.save_personal(
        book.id,
        book.personal.model_copy(update={"completed": True}),
        expected_revision=book.personal_revision,
    )
    with files.library.connection() as db:
        assert (
            db.execute("SELECT updated_at FROM reading_activity WHERE book_id=?", (book.id,)).fetchone()[0]
            == 1800000000
        )


def setup_challenge(handler):
    return SourceHTTP(
        SourceSettings(user_agent="FixtureBrowser/1"),
        {"eh_cookie": "member=preserved"},
        transport=httpx.MockTransport(handler),
        pace=0,
    )


def test_challenge_stops_without_solver_and_manual_cookie_works(files):
    calls = []

    def source(req):
        calls.append(req)
        if req.url.host == "cdn.example" or "cf_clearance=manual" in req.headers.get("cookie", ""):
            return httpx.Response(200, content=b"ok")
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    http = setup_challenge(source)
    try:
        with pytest.raises(SourceError, match="verification_required"):
            http.read("GET", "https://e-hentai.org/")
        assert len(calls) == 1
        http.secrets["eh_cookie"] = "member=preserved; cf_clearance=manual"
        assert http.read("GET", "https://e-hentai.org/") == b"ok"
        assert calls[-1].headers["cookie"] == "member=preserved; cf_clearance=manual"
        assert http.read("GET", "https://cdn.example/file") == b"ok"
        assert "cookie" not in calls[-1].headers
    finally:
        http.close()


def test_post_challenge_is_never_replayed_and_solver_is_not_called(files):
    calls = []

    def source(req):
        calls.append(req.method)
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    http = setup_challenge(source)
    try:
        with pytest.raises(SourceError):
            http.read("POST", "https://e-hentai.org/archiver.php", data={"purchase": "yes"})
        assert calls == ["POST"]
    finally:
        http.close()


def test_opensearch_namespace_and_komga_sort(files, tmp_path):
    import base64
    from xml.etree import ElementTree as ET

    books = [files.import_file(make_zip(tmp_path / f"{n}.cbz", [f"{n}.png"])).book for n in range(2)]
    for book, title in zip(books, ["Zebra", "Alpha"], strict=True):
        save_metadata(files.library, book.id, Metadata(title=title), expected_revision=book.metadata_revision)
    client = manager_client(files)
    auth = Auth(files.library)
    _, key = auth.create_key("readonly", "reader")
    basic = {"Authorization": "Basic " + base64.b64encode(f"reader:{key}".encode()).decode()}
    with client:
        for mount in ("/opds", "/komga/opds/v1.2"):
            response = client.get(mount + "/search.xml", headers=basic)
            root = ET.fromstring(response.content)
            assert root.tag == "{http://a9.com/-/spec/opensearch/1.1/}OpenSearchDescription"
            assert root.find("{http://a9.com/-/spec/opensearch/1.1/}Url") is not None
            assert b"ns0:" not in response.content
        response = client.get("/komga/api/v1/books?sort=metadata.titleSort,asc", headers=basic)
        assert response.status_code == 200
        assert [entry["id"] for entry in response.json()["content"]] == [books[1].id, books[0].id]
        # Unknown sort keys and reading statuses are ignored (falling back to id order).
        for query in ("sort=nonsense,asc", "sort=metadata.numberSort,desc", "read_status=SOMETHING"):
            response = client.get("/komga/api/v1/books?" + query, headers=basic)
            assert response.status_code == 200
            assert [entry["id"] for entry in response.json()["content"]] == sorted(book.id for book in books)
        response = client.get("/komga/api/v1/books?sort=lastModified,desc", headers=basic)
        assert response.status_code == 200 and len(response.json()["content"]) == 2
