from __future__ import annotations

import base64
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree as ET

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import ORIGIN, make_zip, save_metadata

from mangacouch.auth import Auth
from mangacouch.collections import Collections
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app
from mangacouch.models import Metadata, PersonalState, Tag
from mangacouch.preferences import LibraryPreferences, save_preferences
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
    files.library.save_personal(book.id, PersonalState(rating=4.5), expected_revision=1)
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
    assert saved.personal.resume_page == 1 and saved.personal.rating == 4.5
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


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (PersonalState(), None),  # unread
        (PersonalState(resume_page=1), (2, False)),  # in progress
        (PersonalState(completed=True), (3, True)),  # marked read, never opened
        (PersonalState(resume_page=1, completed=True), (3, True)),  # marked read midway
        (PersonalState(resume_page=2, completed=True), (3, True)),  # read to the end
        (PersonalState(resume_page=0, completed=True), (3, True)),  # re-reading a finished book
        (PersonalState(resume_page=None, completed=False), None),  # read mark undone
    ],
)
def test_opds_pse_and_komga_project_the_same_progress(client, auth, files, tmp_path, state, expected):
    """A finished book is finished on both protocols; the stored resume page is kept."""
    book = files.import_file(make_zip(tmp_path / "progress.zip")).book
    assert book.content.page_count == 3
    files.library.save_personal(book.id, state, expected_revision=1)
    headers = basic(auth, "readonly")
    root = ET.fromstring(client.get("/opds/books", headers=headers).content)
    stream = next(item for item in root.iter() if item.get("rel") == "http://vaemendis.net/opds-pse/stream")
    pse = stream.get("{http://vaemendis.net/opds-pse/ns}lastRead")
    komga = client.get(f"/komga/api/v1/books/{book.id}", headers=headers).json()["readProgress"]
    if expected is None:
        assert pse is None and komga is None
    else:
        assert pse is not None and (int(pse), komga["completed"]) == expected
        assert (komga["page"], komga["completed"]) == expected
    assert files.library.get_book(book.id).personal == state  # projection only, nothing rewritten
    series = client.get(f"/komga/api/v1/series/{book.id}", headers=headers).json()
    assert series["booksReadCount"] == int(bool(expected and expected[1]))


def test_completion_changes_read_activity_but_keeps_ratings(client, auth, files, tmp_path, monkeypatch):
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


def test_opensearch_namespace_and_komga_sort(api, auth, files, tmp_path):
    import base64
    from xml.etree import ElementTree as ET

    books = [files.import_file(make_zip(tmp_path / f"{n}.cbz", [f"{n}.png"])).book for n in range(2)]
    for book, title in zip(books, ["Zebra", "Alpha"], strict=True):
        save_metadata(files.library, book.id, Metadata(title=title), expected_revision=book.metadata_revision)
    _, key = auth.create_key("readonly", "reader")
    basic = {"Authorization": "Basic " + base64.b64encode(f"reader:{key}".encode()).decode()}
    with api as client:
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


def test_komga_titles_follow_the_title_preference(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "pref.zip")).book
    save_metadata(
        files.library, book.id, Metadata(title="Translated", original_title="原題"), expected_revision=1
    )
    save_preferences(
        files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
    )
    auth = Auth(files.library)
    auth.initialize("fixture-only-passcode")
    _, key = auth.create_key("readonly", "reader")
    client = TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    )
    with client:
        listing = client.get("/komga/api/v1/series").json()["content"]
        assert [item["metadata"]["title"] for item in listing] == ["原題"]


def test_opds_basic_all_resources_and_revocation(client, files, auth, tmp_path):
    book = files.import_file(make_zip(tmp_path / "源本子.cbz")).book
    save_metadata(
        files.library,
        book.id,
        Metadata(title="标题\x01 😀", tags=[Tag(value="标签\x02")]),
        expected_revision=1,
    )
    key_id, key = auth.create_key("readonly", "reader")
    basic = {"Authorization": "Basic " + base64.b64encode(f"reader:{key}".encode()).decode()}
    response = client.get("/opds")
    assert response.status_code == 401 and "Basic" in response.headers["www-authenticate"]
    ns = {"a": "http://www.w3.org/2005/Atom", "pse": "http://vaemendis.net/opds-pse/ns"}
    response = client.get("/opds/books?limit=1", headers=basic)
    root = ET.fromstring(response.content)
    entry = root.find("a:entry", ns)
    assert entry is not None
    assert entry.findtext("a:id", namespaces=ns) == f"urn:uuid:{book.id}"
    assert entry.findtext("a:title", namespaces=ns) == "标题 😀"
    category = entry.find("a:category", ns)
    assert category is not None and category.attrib["term"] == "other:标签"
    links = {link.attrib["rel"]: link.attrib for link in entry.findall("a:link", ns)}
    stream = links["http://vaemendis.net/opds-pse/stream"]
    assert stream["{http://vaemendis.net/opds-pse/ns}count"] == str(book.content.page_count)
    page = client.get(stream["href"].replace("{pageNumber}", "0"), headers=basic)
    assert page.content.startswith(b"\x89PNG") and page.headers["content-type"] == "image/png"
    assert client.get(links["http://opds-spec.org/image"]["href"], headers=basic).status_code == 200
    download = client.get(links["http://opds-spec.org/acquisition"]["href"], headers=basic)
    assert download.content == (tmp_path / "源本子.cbz").read_bytes()
    assert client.head(links["http://opds-spec.org/acquisition"]["href"], headers=basic).content == b""
    next_link = root.find("a:link[@rel='next']", ns)
    assert next_link is not None
    assert not ET.fromstring(client.get(next_link.attrib["href"], headers=basic).content).findall(
        "a:entry", ns
    )
    assert client.get("/opds/search.xml", headers=basic).status_code == 200
    assert client.get("/api/books", headers=basic).status_code == 401
    assert client.post("/api/bulk", headers={"Authorization": f"Bearer {key}"}, json={}).status_code == 403
    assert client.get("/opds/categories", headers=basic).status_code == 404
    assert files.library.get_book(book.id).personal == book.personal
    auth.revoke(key_id)
    assert client.get(stream["href"].replace("{pageNumber}", "0"), headers=basic).status_code == 401


NS = {"a": "http://www.w3.org/2005/Atom"}


NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"


ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"


PREFIXES = ["/opds", "/komga/opds/v1.2"]


@pytest.fixture
def reader(auth) -> dict[str, str]:
    return basic(auth, "readonly")


def feed(client, url: str, headers: dict[str, str]) -> ET.Element:
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    return ET.fromstring(response.content)


def links(element: ET.Element) -> dict[str, ET.Element]:
    return {link.attrib["rel"]: link for link in element.findall("a:link", NS)}


def next_link(element: ET.Element) -> str | None:
    found = links(element).get("next")
    return found.attrib["href"] if found is not None else None


def entry_ids(element: ET.Element) -> list[str]:
    return [entry.findtext("a:id", namespaces=NS) or "" for entry in element.findall("a:entry", NS)]


def book_ids(files, tmp_path, count: int) -> list[str]:
    return [files.import_file(make_zip(tmp_path / f"{n}.zip", [f"{n}.png"])).book.id for n in range(count)]


def book_entry(prefix: str, book_id: str) -> str:
    return book_id if prefix.startswith("/komga") else f"urn:uuid:{book_id}"


@pytest.mark.parametrize("prefix", PREFIXES)
def test_collections_feed_lists_collections_by_name_with_a_next_page(client, files, reader, prefix):
    collections = Collections(files.library)
    made = {name: collections.create(name)["id"] for name in ("Charlie", "alpha", "Bravo")}
    root = feed(client, prefix, reader)
    navigation = [
        entry for entry in root.findall("a:entry", NS) if "Collections" in entry.findtext("a:title", "", NS)
    ]
    assert links(navigation[0])["subsection"].attrib == {
        "href": f"{prefix}/collections",
        "rel": "subsection",
        "type": NAV,
    }

    first = feed(client, f"{prefix}/collections?limit=2", reader)
    assert links(first)["self"].attrib["type"] == NAV
    entries = first.findall("a:entry", NS)
    assert [entry.findtext("a:title", namespaces=NS) for entry in entries] == ["alpha", "Bravo"]
    for entry, name in zip(entries, ["alpha", "Bravo"], strict=True):
        subsection = links(entry)["subsection"].attrib
        assert subsection["href"] == f"{prefix}/collections/{made[name]}"
        assert subsection["type"] == ACQ
    following = next_link(first)
    assert following is not None and following.startswith(f"{prefix}/collections?")
    assert links(first)["next"].attrib["type"] == NAV
    assert parse_qs(urlsplit(following).query)["limit"] == ["2"]

    last = feed(client, following, reader)
    assert [entry.findtext("a:title", namespaces=NS) for entry in last.findall("a:entry", NS)] == ["Charlie"]
    assert next_link(last) is None
    assert entry_ids(feed(client, f"{prefix}/collections", reader)) == [
        f"{ORIGIN}{prefix}/collections/{made[name]}" for name in ("alpha", "Bravo", "Charlie")
    ]


@pytest.mark.parametrize("prefix", PREFIXES)
def test_collection_feed_pages_members_in_collection_order(client, files, tmp_path, reader, prefix):
    members = book_ids(files, tmp_path, 3)
    collections = Collections(files.library)
    shelf = collections.create("Shelf")
    for book_id in reversed(members):
        shelf = collections.edit_item(shelf["id"], book_id, shelf["revision"])
    order = list(reversed(members))

    first = feed(client, f"{prefix}/collections/{shelf['id']}?limit=2", reader)
    assert first.findtext("a:title", namespaces=NS) == "Shelf"
    assert links(first)["self"].attrib["type"] == ACQ
    assert entry_ids(first) == [book_entry(prefix, book_id) for book_id in order[:2]]
    following = next_link(first)
    assert following is not None and links(first)["next"].attrib["type"] == ACQ
    assert following.startswith(f"{prefix}/collections/{shelf['id']}?")
    assert parse_qs(urlsplit(following).query) == {
        "limit": ["2"],
        "after_position": ["1"],
        "expected_revision": [str(shelf["revision"])],
    }

    last = feed(client, following, reader)
    assert entry_ids(last) == [book_entry(prefix, order[2])] and next_link(last) is None

    # A page link of an edited collection is refused rather than skipping or repeating books.
    collections.rename(shelf["id"], "Renamed", shelf["revision"])
    response = client.get(following, headers=reader)
    assert response.status_code == 409 and response.json()["error"] == "revision_conflict"


def test_dynamic_collection_feed_refuses_a_page_of_changed_results(client, files, tmp_path, reader):
    members = sorted(book_ids(files, tmp_path, 3))
    dynamic = Collections(files.library).create("Everything", {"query": "", "options": {}})

    first = feed(client, f"/opds/collections/{dynamic['id']}?limit=2", reader)
    assert entry_ids(first) == [f"urn:uuid:{book_id}" for book_id in members[:2]]
    following = next_link(first)
    assert following is not None
    query = parse_qs(urlsplit(following).query)
    assert query["after_position"] == ["1"] and query["expected_result"][0]
    assert entry_ids(feed(client, following, reader)) == [f"urn:uuid:{members[2]}"]

    files.import_file(make_zip(tmp_path / "late.zip", ["late.png"]))
    response = client.get(following, headers=reader)
    assert response.status_code == 409 and response.json()["error"] == "revision_conflict"


def test_collection_feeds_reject_invalid_pages_and_unknown_collections(client, files, reader):
    shelf = Collections(files.library).create("Shelf")
    for url, status in [
        (f"/opds/collections/{shelf['id']}?after_position=-2", 422),
        (f"/opds/collections/{shelf['id']}?limit=0", 422),
        ("/opds/collections?limit=201", 422),
        ("/opds/collections?after=not-a-cursor", 422),
        ("/opds/collections/unknown", 404),
    ]:
        assert client.get(url, headers=reader).status_code == status, url
    empty = feed(client, f"/opds/collections/{shelf['id']}", reader)
    assert entry_ids(empty) == [] and next_link(empty) is None
    assert client.get("/opds/collections").status_code == 401
