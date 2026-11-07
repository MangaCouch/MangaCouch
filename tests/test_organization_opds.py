from __future__ import annotations

import base64
import uuid
from xml.etree import ElementTree as ET

import pytest
from helpers import CSRF, book_id, content, login, make_zip, register_book, save_metadata

from mangacouch.activity import Activity
from mangacouch.collections import Collections
from mangacouch.database import RevisionConflict
from mangacouch.errors import InvalidInput, NotFound
from mangacouch.models import Metadata, PersonalState, Tag
from mangacouch.organization import BulkAction, BulkRequest, BulkSearchRequest, BulkTarget, Organization
from mangacouch.snapshots import create_snapshot, restore_snapshot


def request_for(books, action):
    return BulkRequest(
        id=str(uuid.uuid4()),
        action=action,
        targets=[
            BulkTarget(
                book_id=book.id,
                metadata_revision=book.metadata_revision,
                personal_revision=book.personal_revision,
            )
            for book in books
        ],
    )


def test_tag_alias_search_is_unicode_normalized_and_revision_checked(files):
    org = Organization(files.library)
    books = [
        register_book(
            files.library,
            content(str(i), f"{i}.cbz"),
            Metadata(title=f"测试{i}", tags=[Tag(namespace="artist", value="ＡＢＣ")]),
        )[0]
        for i in range(3)
    ]
    org.alias(Tag(namespace="artist", value="ABC"), "测试译名", None)
    assert set(files.library.search("译名")) == {book.id for book in books}
    assert org.aliases() == [{"namespace": "artist", "value": "abc", "translation": "测试译名"}]
    with pytest.raises(RevisionConflict):
        org.alias(Tag(namespace="artist", value="abc"), "new", None)
    org.alias(Tag(namespace="artist", value="abc"), "", "测试译名")
    assert not files.library.search("译名") and org.aliases() == []


def test_bulk_conflicts_receipts_and_snapshot(files, tmp_path):
    org = Organization(files.library)
    books = [register_book(files.library, content(str(i), f"{i}.cbz"))[0] for i in range(4)]
    request = request_for(books, BulkAction(kind="rating", rating=4.5))
    task = org.create_bulk(request)
    assert task["state"] == "running" and task["counts"]["pending"] == 4
    files.library.save_personal(books[0].id, PersonalState(rating=2, bookmarks=[0]), expected_revision=1)
    assert org.advance(limit=2) is True
    assert org.bulk(task["id"])["counts"] == {
        "done": 1,
        "conflict": 1,
        "missing": 0,
        "pending": 2,
        "error": 0,
    }
    assert org.create_bulk(request)["state"] == "running"
    assert org.advance() is False
    finished = org.bulk(task["id"])["counts"]
    assert finished == {"done": 3, "conflict": 1, "missing": 0, "pending": 0, "error": 0}
    assert org.create_bulk(request)["state"] == "done"
    assert files.library.get_book(books[0].id).personal.bookmarks == [0]
    assert files.library.get_book(books[0].id).personal.rating == 2
    assert files.library.get_book(books[1].id).personal.rating == 4.5
    with pytest.raises(RevisionConflict):
        org.create_bulk(request.model_copy(update={"action": BulkAction(kind="completed")}))
    snapshot = create_snapshot(files, tmp_path / "bulk.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=files.root.path)
    assert Organization(restored.library).bulk(task["id"]) == org.bulk(task["id"])


def test_bulk_add_collection_appends_once_and_skips_dynamic_targets(files):
    org = Organization(files.library)
    collections = Collections(files.library)
    books = [register_book(files.library, content(str(i), f"{i}.cbz"))[0] for i in range(2)]
    static = collections.create("周末")
    static = collections.edit_item(static["id"], books[1].id, static["revision"])
    action = BulkAction(kind="add_collection", target_id=static["id"], target_revision=static["revision"])
    job = org.create_bulk(request_for(books, action))
    org.advance()
    assert org.bulk(job["id"])["counts"]["done"] == 2
    current = collections.get(static["id"])
    assert [item["book_id"] for item in current["items"]] == [books[1].id, books[0].id]
    assert current["revision"] == static["revision"] + 1
    dynamic = collections.create("中文", {"query": "", "options": {"language": "Chinese"}})
    action = BulkAction(kind="add_collection", target_id=dynamic["id"], target_revision=dynamic["revision"])
    with pytest.raises(InvalidInput) as rejected:
        org.create_bulk(request_for(books, action))
    assert rejected.value.code == "dynamic_collection"
    missing = BulkAction(kind="add_collection", target_id="missing", target_revision=1)
    with pytest.raises(NotFound):
        org.create_bulk(request_for(books, missing))
    stale = BulkAction(kind="add_collection", target_id=static["id"], target_revision=1)
    with pytest.raises(RevisionConflict):
        org.create_bulk(request_for(books, stale))
    with pytest.raises(ValueError):
        BulkAction(kind="add_collection", target_id=static["id"])


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


def test_organization_http_permissions_and_alias_input(client, files):
    login(client)
    body = {"tag": {"namespace": "artist", "value": "ABC"}, "translation": "译名"}
    assert client.put("/api/tag-aliases", json=body).status_code == 403
    assert client.put("/api/tag-aliases", json=body, headers=CSRF).status_code == 200
    assert client.get("/api/tag-aliases").json()["aliases"] == [
        {"namespace": "artist", "value": "abc", "translation": "译名"}
    ]
    too_long = {**body, "translation": "x" * 513}
    assert client.put("/api/tag-aliases", json=too_long, headers=CSRF).status_code == 422
    changed = {**body, "translation": "其他"}
    assert client.put("/api/tag-aliases", json=changed, headers=CSRF).status_code == 409


def test_search_bulk_freezes_over_500_targets_and_revision_conflicts(library):
    ids = []
    for i in range(505):
        book, _ = register_book(library, content(str(i)), Metadata(title="Selected"))
        ids.append(book.id)
    org = Organization(library)
    request = BulkSearchRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="rating", rating=4),
        query="Selected",
        expected_count=505,
        exclude=ids[:2],
    )
    job = org.create_bulk(request)
    assert job["counts"]["pending"] == 503
    assert len(job["items"]) == 200 and job["after_position"] == 199
    assert len(org.bulk(request.id, after=199)["items"]) == 200
    register_book(library, content("later"), Metadata(title="Selected"))
    assert org.create_bulk(request)["counts"]["pending"] == 503
    changed = library.get_book(ids[2])
    library.save_personal(changed.id, PersonalState(rating=5), expected_revision=changed.personal_revision)
    while org.advance(limit=20):
        pass
    job = org.bulk(request.id)
    assert job["state"] == "done" and job["counts"]["done"] == 502 and job["counts"]["conflict"] == 1
    assert library.get_book(ids[0]).personal.rating is None
    assert library.get_book(ids[2]).personal.rating == 5
    assert Activity(library).recent()["events"] == []
    with pytest.raises(RevisionConflict):
        org.create_bulk(request.model_copy(update={"id": str(uuid.uuid4())}))


@pytest.mark.usefixtures("without_maintenance")
def test_web_routes_share_auth_filters_and_snapshot_contract(client, files, auth, tmp_path):
    identifier = book_id(files, tmp_path)
    library = files.library
    save_metadata(
        library, identifier, Metadata(tags=[Tag(namespace="artist", value="blue sky")]), expected_revision=1
    )
    Organization(library).alias(Tag(namespace="artist", value="blue sky"), "蓝天", None)
    for path in ["/api/tag-suggestions", "/api/reading-history", "/api/statistics"]:
        assert client.get(path).status_code == 401
    login(client)
    assert client.get("/api/tag-suggestions", params={"query": "蓝天"}).json()["tags"][0]["count"] == 1
    for query, total in [('artist:"blue sky"', 1), ("absent", 0)]:
        assert client.get("/api/books", params={"query": query}).json()["total"] == total
    library.save_personal(identifier, PersonalState(resume_page=0), expected_revision=1)
    assert client.get("/api/reading-history").json()["events"][0]["book"]["id"] == identifier
    assert client.get("/api/statistics?days=7").json()["period"]["books"] == 1
    request = {
        "id": str(uuid.uuid4()),
        "action": {"kind": "rating", "rating": 4},
        "query": "蓝天",
        "expected_count": 1,
    }
    response = client.post("/api/bulk-search", json=request, headers=CSRF)
    assert response.status_code == 200 and response.json()["counts"]["pending"] == 1
    assert library.get_book(identifier).personal.rating is None
    assert client.get(f"/api/bulk/{request['id']}?after=0").json()["items"] == []
    request["id"] = str(uuid.uuid4())
    request["expected_count"] = 2
    assert client.post("/api/bulk-search", json=request, headers=CSRF).status_code == 409
    _, readonly = auth.create_key("readonly", "test readonly")
    client.cookies.clear()
    assert (
        client.post(
            "/api/bulk-search", json=request, headers={"Authorization": f"Bearer {readonly}"}
        ).status_code
        == 403
    )
