from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing, contextmanager

import pytest
from helpers import CSRF, content, login, make_zip, register_book, save_metadata

from mangacouch import archives
from mangacouch.collections import Collections
from mangacouch.editions import Editions
from mangacouch.models import Metadata, Source
from mangacouch.organization import BulkAction, BulkRequest, BulkTarget, Organization
from mangacouch.page_features import PENDING, PageFeatures


def test_plain_sqlite_client_can_write_without_application_functions(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    with closing(sqlite3.connect(files.library.path)) as db:
        db.execute("UPDATE book SET metadata_json=metadata_json WHERE id=?", (book.id,))
        db.commit()
    assert files.library.get_book(book.id).metadata == book.metadata


def test_terminal_imports_expire_without_dropping_recovery_jobs(files):
    with files.library.transaction() as db:
        db.executemany(
            "INSERT INTO import_job(id,state,kind,created_at) VALUES(?,?,'copy',?)",
            [("done", "done", 0), ("failed", "failed", 0), ("copying", "copying", 0), ("ready", "ready", 0)],
        )
        db.execute("INSERT INTO import_job(id,state,kind) VALUES('new','done','copy')")
    assert files.purge_imports() == 2
    with files.library.connection() as db:
        assert {row[0] for row in db.execute("SELECT id FROM import_job")} == {"copying", "ready", "new"}


def test_page_versions_are_immutable_only_for_the_matching_archive(api, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    login(api)
    route = f"/api/books/{book.id}/pages/0"
    versioned = api.get(route + "?v=" + book.content.sha256[:16])
    assert versioned.status_code == 200
    assert "immutable" in versioned.headers["cache-control"]
    assert "immutable" not in api.get(route + "?v=outdated").headers["cache-control"]
    assert api.get(route).content == versioned.content


def test_numeric_continuation_does_not_count_again(api, files, monkeypatch):
    from mangacouch import api_library

    register_book(files.library, content())
    login(api)
    assert api.get("/api/books?limit=1&page=1").json()["total"] == 1

    def forbidden(*args):
        pytest.fail("Continuation must not count the library")

    monkeypatch.setattr(api_library, "search_count", forbidden)
    page = api.get("/api/books?limit=1&page=2&include_total=false")
    assert page.status_code == 200 and page.json()["total"] is None


def test_dynamic_collection_counts_share_one_book_scan(files, monkeypatch):
    register_book(files.library, content("one"), Metadata(title="One"))
    register_book(files.library, content("two"), Metadata(title="Two"))
    collections = Collections(files.library)
    for name, query in (("A", ""), ("B", "One"), ("C", "absent")):
        collections.create(name, {"query": query})
    statements = []
    connect = files.library.connection

    @contextmanager
    def measured():
        with connect() as db:
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith("SELECT") else None)
            yield db

    monkeypatch.setattr(files.library, "connection", measured)
    assert [item["count"] for item in collections.list()["collections"]] == [2, 1, 0]
    assert len(statements) == 2


def test_scan_edition_batch_matches_single_book_queries(files):
    old_source = Source(site="ehentai", gallery_id=10, gallery_token="abcdef1234")
    new_source = Source(site="exhentai", gallery_id=20, gallery_token="abcdef5678")
    older, _ = register_book(
        files.library, content("old"), Metadata(source=old_source, newer_versions=[new_source])
    )
    newer, _ = register_book(files.library, content("new"), Metadata(source=new_source, parent=old_source))
    editions = Editions(files.library)
    result = editions.previous_many([older.id, newer.id, "deleted"])
    assert result[newer.id] == [item["id"] for item in editions.candidates(newer.id)["items"]] == [older.id]
    assert result[older.id] == result["deleted"] == []


def test_bulk_source_queue_checks_revision_and_reports_missing_sources(files):
    service = Organization(files.library)
    source = Source(site="nhentai", gallery_id=100)
    good, _ = register_book(files.library, content("good"), Metadata(source=source))
    absent, _ = register_book(files.library, content("absent"))
    stale, _ = register_book(files.library, content("stale"), Metadata(source=source))
    request = BulkRequest(
        id=str(uuid.uuid4()),
        action=BulkAction(kind="source"),
        targets=[
            BulkTarget(book_id=book.id, metadata_revision=1, personal_revision=1)
            for book in (good, absent, stale)
        ],
    )
    save_metadata(files.library, stale.id, Metadata(title="Changed", source=source), expected_revision=1)
    service.create_bulk(request)
    service.advance()
    assert service.bulk(request.id)["counts"] == {
        "done": 1,
        "error": 1,
        "conflict": 1,
        "missing": 0,
        "pending": 0,
    }
    with files.library.connection() as db:
        assert (
            db.execute("SELECT state FROM source_check WHERE book_id=?", (good.id,)).fetchone()[0] == "queued"
        )


def test_removal_selection_returns_only_matching_ids_and_rejects_changed_counts(api, files):
    a, _ = register_book(files.library, content("a"), Metadata(title="Choose"))
    b, _ = register_book(files.library, content("b"), Metadata(title="Choose"))
    register_book(files.library, content("c"), Metadata(title="Other"))
    login(api)
    body = {"query": "Choose", "expected_count": 2, "exclude": [b.id]}
    response = api.post("/api/removals/search/selection", json=body, headers=CSRF)
    assert response.status_code == 200 and response.json() == {"book_ids": [a.id]}
    assert (
        api.post(
            "/api/removals/search/selection", json={**body, "expected_count": 3}, headers=CSRF
        ).status_code
        == 409
    )
    assert len(files.library.search("")) == 3  # Selecting never removes anything.


def test_feature_progress_is_indexed_and_rebuilt_after_delete(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    worker = PageFeatures(files)
    worker.index(book.id, limit=book.content.page_count)
    with files.library.transaction() as db:
        assert db.execute(PENDING, (None, None)).fetchone() is None
        plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + PENDING, (None, None)))
        assert "book_pending_features" in plan
        db.execute("DELETE FROM page_feature WHERE book_id=? AND page=0", (book.id,))
        assert db.execute(PENDING, (None, None)).fetchone()[0] == book.id
    assert worker.index(book.id, limit=10)["processed"] == 1


def test_zip_directory_cache_invalidates_and_never_retains_open_archives(tmp_path, monkeypatch):
    path = make_zip(tmp_path / "cache.cbz", ["1.png", "2.png"])
    limits = archives.Limits()
    pages = archives.inspect_archive(path, "zip", limits)
    calls = []
    parse = archives.zipfile.ZipFile._RealGetContents  # pyright: ignore[reportAttributeAccessIssue]

    def counted(archive):
        calls.append(1)
        return parse(archive)

    monkeypatch.setattr(archives.zipfile.ZipFile, "_RealGetContents", counted)
    with archives.zipfile.ZipFile(path) as archive:
        expected = [archive.read(page.name) for page in pages]
    calls.clear()
    assert archives.read_page(path, "zip", pages[0], limits) == expected[0]
    assert archives.read_page(path, "zip", pages[1], limits) == expected[1]
    assert len(calls) == 1
    with pytest.raises(archives.InvalidArchive):
        archives.read_page(path, "zip", pages[0], archives.Limits(max_pages=1))
    # Replacement and deletion work even after a cache hit, including on Windows.
    path.unlink()
    make_zip(path, ["replacement.png"])
    with pytest.raises(archives.InvalidArchive, match="no longer present"):
        archives.read_page(path, "zip", pages[0], limits)
    path.unlink()
