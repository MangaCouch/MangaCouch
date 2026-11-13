from __future__ import annotations

import sqlite3

import pytest
from helpers import content, make_zip, register_book, save_metadata, titled

from mangacouch import searching
from mangacouch.database import Library
from mangacouch.models import Metadata, PersonalState, Tag
from mangacouch.organization import Organization
from mangacouch.removals import Removals
from mangacouch.searching import SearchOptions, search_count, search_ids
from mangacouch.translations import Translations

RANDOM = SearchOptions(order="random", seed="a" * 32)


@pytest.mark.perf
def test_random_continuations_do_not_rescan_books_or_hash_each_book(library, monkeypatch):
    for index in range(80):
        register_book(library, content(str(index)))
    expected = library.search("", options=RANDOM, limit=200)
    calls = []
    original = searching.random_key

    def measured(seed, identifier):
        calls.append(identifier)
        return original(seed, identifier)

    monkeypatch.setattr(searching, "random_key", measured)
    with Library(library.path).connection() as db:
        db.set_authorizer(
            lambda action, table, *_: (
                sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_READ and table == "book"
                else sqlite3.SQLITE_OK
            )
        )
        assert search_ids(db, "", options=RANDOM, limit=9, after_id=expected[6]) == expected[7:16]
        assert search_ids(db, "", options=RANDOM, limit=9, after_id="", offset=20) == expected[20:29]
    assert len(calls) < 12


def test_random_cache_respects_snapshot_and_does_not_publish_rolled_back_edits(library):
    book, _ = register_book(library, content(), Metadata(title="Original"))
    other = Library(library.path)
    with library.connection() as reader:
        reader.execute("BEGIN")
        assert search_ids(reader, "Original", options=RANDOM, limit=20, after_id="") == [book.id]
        save_metadata(other, book.id, Metadata(title="Committed"), expected_revision=1)
        assert other.search("Original", options=RANDOM) == []
        assert search_ids(reader, "Original", options=RANDOM, limit=20, after_id="") == [book.id]
    with pytest.raises(RuntimeError, match="rollback"), library.transaction() as writer:
        library.write_metadata(writer, book.id, Metadata(title="Transient"), 2)
        assert search_ids(writer, "Transient", options=RANDOM, limit=20, after_id="") == [book.id]
        assert writer.in_transaction
        raise RuntimeError("rollback")
    save_metadata(other, book.id, Metadata(title="Different"), expected_revision=2)
    assert library.search("Transient", options=RANDOM) == []
    assert library.search("Different", options=RANDOM) == [book.id]


def test_random_filter_cache_changes_with_metadata_personal_review_and_imports(library):
    book, _ = register_book(library, content(), Metadata(language="Japanese", work_type="Manga"))
    options = RANDOM.model_copy(update={"language": "Japanese", "work_type": "Manga", "rating": "rated"})
    assert library.search("", options=options) == []
    library.save_personal(book.id, PersonalState(rating=5.0), expected_revision=1)
    assert library.search("", options=options) == [book.id]
    other = Library(library.path)
    save_metadata(other, book.id, Metadata(language="English", work_type="Manga"), expected_revision=1)
    assert library.search("", options=options) == []
    options = RANDOM.model_copy(update={"review": "confirmed"})
    assert library.search("", options=options) == []
    with other.transaction() as db:
        db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (book.id,))
    assert library.search("", options=options) == [book.id]
    assert library.search("", options=RANDOM) == [book.id]
    added, _ = register_book(other, content("new"))
    assert set(library.search("", options=RANDOM)) == {book.id, added.id}
    unread = RANDOM.model_copy(update={"reading": "unread"})
    assert set(library.search("", options=unread)) == {book.id, added.id}
    library.save_personal(book.id, PersonalState(rating=5.0, resume_page=1), expected_revision=2)
    assert library.search("", options=unread) == [added.id]


def test_alias_translation_and_tag_changes_invalidate_cached_text_results(library):
    tag = Tag(namespace="artist", value="test")
    book, _ = register_book(library, content(), Metadata(tags=[tag]))
    organization = Organization(library)
    assert library.search("译名", options=RANDOM) == []
    Translations(library).import_data(
        {"version": 7, "data": [{"namespace": "artist", "data": {"test": {"name": "译名"}}}]}
    )
    assert library.search("译名", options=RANDOM) == [book.id]
    organization.alias(tag, "手动覆盖", None)
    assert library.search("译名", options=RANDOM) == []
    assert library.search("手动覆盖", options=RANDOM) == [book.id]
    organization.alias(tag, "更新译名", "手动覆盖")
    assert library.search("手动覆盖", options=RANDOM) == []
    organization.alias(tag, "", "更新译名")
    assert library.search("译名", options=RANDOM) == [book.id]
    save_metadata(library, book.id, Metadata(), expected_revision=1)
    assert library.search("译名", options=RANDOM) == []


def test_progress_and_feature_bookkeeping_do_not_evict_random_results(library):
    book, _ = register_book(library, content())
    library.save_personal(book.id, PersonalState(resume_page=1), expected_revision=1)
    assert library.search("", options=RANDOM) == [book.id]
    with library.connection() as db:
        before = db.execute("SELECT token FROM search_revision").fetchone()[0]
    library.save_personal(book.id, PersonalState(resume_page=2), expected_revision=2)
    with library.transaction() as db:
        db.execute("UPDATE book SET indexed_pages=1,availability='missing' WHERE id=?", (book.id,))
        assert db.execute("SELECT token FROM search_revision").fetchone()[0] == before
    assert library.search("", options=RANDOM) == [book.id]


@pytest.mark.parametrize("budget", [0, 5000])
def test_random_results_survive_bounded_cache_eviction(library, monkeypatch, budget):
    cache = searching._RandomResults(budget=budget, capacity=1)
    monkeypatch.setattr(searching, "_random_results", cache)
    for index in range(12):
        register_book(library, content(str(index)))
    expected = library.search("", options=RANDOM)
    for seed in ("b", "c", "d"):
        assert set(library.search("", options=RANDOM.model_copy(update={"seed": seed * 32}))) == set(expected)
    assert cache.bytes <= budget and len(cache.entries) <= 1
    assert library.search("", options=RANDOM, after_id=expected[3]) == expected[4:]


@pytest.mark.perf
def test_normalized_filters_use_stored_keys_and_indexes(library):
    book, _ = register_book(library, content(), Metadata(language="Ｊａｐａｎｅｓｅ", work_type="Ｍａｎｇａ"))
    options = SearchOptions(language="JAPANESE", work_type="MANGA")
    with library.connection() as db:

        def forbidden(value):
            pytest.fail("Search must not normalize each database row")

        db.create_function("mc_normalized", 1, forbidden)
        assert search_count(db, "", options) == 1
        assert search_ids(db, "", options=options, limit=20, after_id="") == [book.id]
        conditions, values = searching.rule_conditions("", options)
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT b.id FROM book b JOIN personal p ON p.book_id=b.id WHERE "
            + " AND ".join(conditions),
            values,
        ).fetchall()
        assert any("book_language" in row[3] or "book_work_type" in row[3] for row in plan)
    save_metadata(library, book.id, Metadata(language="EN", work_type="Novel"), expected_revision=1)
    assert library.search("", options=options) == []
    assert library.search("", options=SearchOptions(language="English", work_type="ＮＯＶＥＬ")) == [book.id]


@pytest.mark.perf
def test_recent_paging_matches_original_order_and_uses_seek_index(library):
    books = [register_book(library, content(str(i)))[0] for i in range(45)]
    with library.transaction() as db:
        db.executemany(
            "INSERT INTO reading_activity VALUES(?,?)",
            [(book.id, (0, 10, -10, 20)[index % 4]) for index, book in enumerate(books[:30])],
        )
    options = SearchOptions(order="recent")
    with library.connection() as db:
        expected = [
            row[0]
            for row in db.execute(
                "SELECT b.id FROM book b LEFT JOIN reading_activity a ON a.book_id=b.id "
                "ORDER BY coalesce(a.updated_at,0) DESC,b.id"
            )
        ]
        statements = []
        db.set_trace_callback(statements.append)
        pages = []
        while page := search_ids(db, "", options=options, limit=7, after_id=pages[-1] if pages else ""):
            pages.extend(page)
        assert pages == expected
        assert search_ids(db, "", options=options, limit=7, after_id="", offset=14) == expected[14:21]
        query = next(sql for sql in statements if "SELECT b.id FROM" in sql and "recent_order,b.id)>" in sql)
        plan = [row[3] for row in db.execute("EXPLAIN QUERY PLAN " + query)]
        assert any("SEARCH b USING COVERING INDEX book_recent" in step for step in plan)
        assert not any("TEMP B-TREE" in step for step in plan)
    with library.transaction() as db:
        db.execute("UPDATE reading_activity SET updated_at=1000 WHERE book_id=?", (books[0].id,))
    assert library.search("", options=options)[0] == books[0].id
    with library.transaction() as db:
        db.execute("DELETE FROM reading_activity WHERE book_id=?", (books[0].id,))
    assert library.search("", options=options)[0] != books[0].id


def test_removal_restore_refreshes_random_results_and_recent_keys(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    files.library.save_personal(book.id, PersonalState(resume_page=1), expected_revision=1)
    recent = SearchOptions(order="recent")
    assert files.library.search("", options=RANDOM) == [book.id]
    service = Removals(files)
    preview = service.preview(book.id)
    receipt = service.remove(preview["id"], book.id, preview["token"], confirmed=True)
    assert files.library.search("", options=RANDOM) == []
    service.restore(receipt["id"])
    assert files.library.search("", options=RANDOM) == [book.id]
    assert files.library.search("", options=recent) == [book.id]
    with files.library.connection() as db:
        assert (
            db.execute(
                "SELECT b.recent_order=-a.updated_at FROM book b JOIN reading_activity a ON a.book_id=b.id"
            ).fetchone()[0]
            == 1
        )


def test_recent_order_uses_reading_activity(library: Library):
    first, second, third = (titled(library, name) for name in ("a", "b", "c"))
    library.save_personal(second.id, PersonalState(resume_page=1), expected_revision=1)
    with library.connection() as db:
        db.execute("UPDATE reading_activity SET updated_at=100 WHERE book_id=?", (second.id,))
    library.save_personal(third.id, PersonalState(resume_page=1), expected_revision=1)
    from mangacouch.searching import SearchOptions

    options = SearchOptions(order="recent")
    ordered = library.search("", options=options)
    assert ordered[:2] == [third.id, second.id] and ordered[2] == first.id
    assert library.search("", options=options, limit=1, after_id=third.id) == [second.id]
