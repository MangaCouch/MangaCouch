from __future__ import annotations

from contextlib import contextmanager

import pytest
from helpers import content, login, make_zip, pending_sidecars, register_book, save_metadata
from pydantic import ValidationError

from mangacouch import searching
from mangacouch.database import Library
from mangacouch.models import Metadata, PersonalState, SourceRating, Tag
from mangacouch.organization import Organization
from mangacouch.searching import SearchOptions, search_count, tag_suggestions
from mangacouch.translations import Translations


def populate(library):
    result = []
    values = [
        (None, None, False, False),
        (1.0, 4.8, False, False),
        (4.5, 1.0, True, True),
        (5.0, 4.5, False, True),
        (None, 0.0, True, False),
    ]
    for i, (rating, source, completed, confirmed) in enumerate(values):
        book, _ = register_book(
            library,
            content(f"book-{i}", f"心海-{i}.cbz"),
            Metadata(
                title=f"ＡＢＣ 第{i}本",
                source_rating=SourceRating(average=source) if source is not None else None,
            ),
        )
        library.save_personal(book.id, PersonalState(rating=rating, completed=completed), expected_revision=1)
        if confirmed:
            with library.transaction() as db:
                db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (book.id,))
        result.append(book.id)
    return result


@pytest.mark.parametrize(
    "options, indexes",
    [
        ({}, [0, 1, 2, 3, 4]),
        ({"reading": "unfinished"}, [0, 1, 3]),
        ({"reading": "completed"}, [2, 4]),
        ({"review": "needs_review"}, [0, 1, 4]),
        ({"review": "confirmed"}, [2, 3]),
        ({"rating": "unrated"}, [0, 4]),
        ({"rating": "rated"}, [1, 2, 3]),
        ({"rating": "rated", "min_rating": 4.5}, [2, 3]),
        ({"source_rating_min": 4.5}, [1, 3]),
        ({"source_rating_min": 0.0}, [1, 2, 3, 4]),
        (
            {
                "reading": "unfinished",
                "review": "confirmed",
                "rating": "rated",
                "min_rating": 4.5,
                "source_rating_min": 4.5,
            },
            [3],
        ),
        ({"rating": "unrated", "source_rating_min": 4.5}, []),
    ],
)
def test_filters_combine_with_unicode_terms_and_keep_ratings_separate(library, options, indexes):
    ids = populate(library)
    assert library.search("心 abc", options=SearchOptions(**options)) == sorted(ids[i] for i in indexes)


def test_random_pagination_is_repeatable_across_reopen_and_does_not_duplicate(library):

    for i in range(41):
        register_book(library, content(str(i)))
    first = SearchOptions(order="random", seed="a" * 32)
    all_ids = library.search("", options=first, limit=200)
    assert all_ids == Library(library.path).search("", options=first, limit=200)
    assert all_ids != library.search("", options=SearchOptions(order="random", seed="b" * 32), limit=200)
    assert set(all_ids) == set(library.search("", limit=200))
    pages = []
    while page := library.search("", options=first, limit=7, after_id=pages[-1] if pages else ""):
        pages.extend(page)
    assert pages == all_ids and len(set(pages)) == 41


def test_random_cursor_survives_deleted_book_and_hash_ties(library, monkeypatch):
    ids = sorted(populate(library))
    monkeypatch.setattr(searching, "random_key", lambda _seed, _id: "same-key")
    options = SearchOptions(order="random", seed="a" * 32)
    assert library.search("", options=options, limit=2) == ids[:2]
    with library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (ids[1],))
    assert library.search("", options=options, after_id=ids[1]) == ids[2:]


def test_metadata_and_personal_edits_are_visible_without_search_index_rebuild(library):
    ids = populate(library)
    options = SearchOptions(rating="rated", min_rating=5.0, review="needs_review", source_rating_min=4.5)
    assert library.search("", options=options) == []
    book = library.get_book(ids[1])
    library.save_personal(book.id, PersonalState(rating=5.0), expected_revision=book.personal_revision)
    assert library.search("", options=options) == [book.id]
    save_metadata(
        library,
        book.id,
        book.metadata.model_copy(update={"source_rating": SourceRating(average=1.0)}),
        expected_revision=book.metadata_revision,
    )
    assert library.search("", options=options) == []


@pytest.mark.parametrize(
    "options",
    [
        {"reading": "unknown"},
        {"review": "accepted"},
        {"rating": "favorite"},
        {"min_rating": 4.0},
        {"rating": "unrated", "min_rating": 4.0},
        {"rating": "rated", "min_rating": 4.2},
        {"rating": "rated", "min_rating": -0.5},
        {"source_rating_min": 5.1},
        {"source_rating_min": float("nan")},
        {"order": "random"},
        {"order": "random", "seed": "x" * 32},
        {"seed": "a" * 33},
        {"order": "injected SQL"},
    ],
)
def test_invalid_filter_and_random_inputs_are_rejected(options):
    with pytest.raises(ValidationError):
        SearchOptions(**options)


@pytest.mark.parametrize(
    "query, limit, after",
    [
        ("x" * 1025, 50, ""),
        ("", 201, ""),
        ("", 0, ""),
        ("", 50, "x" * 129),
        (" ".join(map(str, range(33))), 50, ""),
    ],
)
def test_bounded_query_and_cursor(library, query, limit, after):
    with pytest.raises(ValueError):
        library.search(query, limit=limit, after_id=after)


def test_sql_metacharacters_are_literal_text(library):
    populate(library)
    assert library.search("%' OR 1=1 --", options=SearchOptions(order="random", seed="a" * 32)) == []
    assert len(library.search("")) == 5


def test_authenticated_http_filters_and_random_pagination_allow_readonly_keys(client, files, auth):
    ids = populate(files.library)
    _, key = auth.create_key("readonly", "filter-fixture")
    headers = {"Authorization": f"Bearer {key}"}
    params = {"rating": "rated", "min_rating": "4.5", "order": "random", "seed": "a" * 32, "limit": 1}
    first = client.get("/api/books", params=params, headers=headers)
    assert first.status_code == 200
    first_book = first.json()["books"][0]["id"]
    second = client.get(
        "/api/books", params={**params, "after_id": first.json()["after_id"]}, headers=headers
    )
    assert second.status_code == 200
    assert {first_book, second.json()["books"][0]["id"]} == {ids[2], ids[3]}
    assert client.get("/api/books", params={"order": "random"}, headers=headers).status_code == 422
    assert client.get("/api/books", params={"source_rating_min": "nan"}, headers=headers).status_code == 422
    assert client.get("/api/books", params=params).status_code == 401


def test_filters_survive_snapshot_restore_and_do_not_touch_sidecars(files, client, tmp_path):
    from mangacouch.snapshots import create_snapshot, restore_snapshot

    ids = populate(files.library)
    before = pending_sidecars(files.library)
    login(client)
    result = client.get("/api/books", params={"review": "needs_review", "source_rating_min": 0})
    assert result.status_code == 200
    assert {book["id"] for book in result.json()["books"]} == {ids[1], ids[4]}
    snapshot = tmp_path / "snapshot.zip"
    create_snapshot(files, snapshot)
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    library = restored.library
    assert set(library.search("", options=SearchOptions(review="needs_review", source_rating_min=0.0))) == {
        ids[1],
        ids[4],
    }
    assert pending_sidecars(files.library) == before


def test_continue_reading_is_filtered_before_pagination_and_includes_first_page(library):
    for index in range(30):
        register_book(library, content(f"continue-{index}"))
    identifiers = library.search("", limit=200)
    for identifier, state in [
        (identifiers[25], PersonalState(resume_page=0)),
        (identifiers[27], PersonalState(resume_page=4)),
        (identifiers[29], PersonalState(resume_page=4, completed=True)),
    ]:
        library.save_personal(identifier, state, expected_revision=1)
    assert library.search("", options=SearchOptions(reading="in_progress"), limit=12) == [
        identifiers[25],
        identifiers[27],
    ]


def test_zero_and_half_star_ratings_remain_distinct_from_unrated(library):
    ids = populate(library)
    for identifier, rating in zip(ids[:2], [0, 0.5], strict=True):
        book = library.get_book(identifier)
        library.save_personal(
            identifier, PersonalState(rating=rating), expected_revision=book.personal_revision
        )
        assert library.get_book(identifier).personal.rating == rating
    rated = library.search("", options=SearchOptions(rating="rated", min_rating=0))
    assert set(ids[:2]).issubset(rated)
    assert ids[4] not in rated


def test_normalized_tag_index_updates_and_deletes(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "a.cbz")).book
    save_metadata(
        files.library,
        book.id,
        Metadata(tags=[Tag(namespace="ＦＥＭＡＬＥ", value="Test"), Tag(namespace="female", value="test")]),
        expected_revision=book.metadata_revision,
    )
    with files.library.connection() as db:
        assert tag_suggestions(db, "female:test") == [
            {"namespace": "female", "value": "test", "count": 1, "translation": None}
        ]
        assert files.library.search("female:test") == [book.id]
    with files.library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (book.id,))
        assert db.execute("SELECT count(*) FROM book_tag").fetchone()[0] == 0


def test_catalog_queries_do_not_grow_with_page_size_and_keep_enrichment(api, files, monkeypatch):
    for index in range(24):
        register_book(
            files.library, content(str(index)), Metadata(tags=[Tag(namespace="artist", value="same")])
        )
    Organization(files.library).alias(Tag(namespace="artist", value="same"), "译名", None)
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO source_check(book_id,state,error_code,reasons_json,updated_at) "
            "SELECT id,'needs_review','source_absent','[\"source_absent\"]',100 FROM book"
        )
    connect = files.library.connection
    statements = []
    connections = []

    @contextmanager
    def measured():
        connections.append(1)
        with connect() as db:
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith("SELECT") else None)
            yield db

    monkeypatch.setattr(files.library, "connection", measured)
    small = api.get("/api/books?limit=1")
    small_counts = len(connections), len(statements)
    connections.clear()
    statements.clear()
    large = api.get("/api/books?limit=24")
    assert small.status_code == large.status_code == 200
    assert (len(connections), len(statements)) == small_counts
    assert len(large.json()["books"]) == 24
    for book in large.json()["books"]:
        assert book["tag_translations"] == [{"namespace": "artist", "value": "same", "translation": "译名"}]
        assert book["source_check"]["reasons"] == ["source_absent"]
        assert book["sidecar"] == {"pending_revision": 1, "synced_revision": 0, "last_error": None}


def test_suggestions_count_books_and_alias_and_exact_multiword_search(library):
    ids = []
    for i in range(3):
        tags = [Tag(namespace="artist", value="Blue Sky" if i < 2 else "Blue Sky Extra")]
        book, _ = register_book(library, content(str(i)), Metadata(tags=tags))
        ids.append(book.id)
    Organization(library).alias(Tag(namespace="artist", value="blue sky"), "蓝天", None)
    with library.connection() as db:
        rows = tag_suggestions(db, "蓝")
        assert rows == [{"namespace": "artist", "value": "blue sky", "count": 2, "translation": "蓝天"}]
        assert tag_suggestions(db, "artist:blue")[0]["count"] == 2
        assert tag_suggestions(db, "%' OR 1=1 --") == []
        assert search_count(db, 'artist:"blue sky"', SearchOptions()) == 2
    assert library.search('artist:"blue sky"') == sorted(ids[:2])
    assert library.search("蓝天") == sorted(ids[:2])


def test_numeric_pages_and_unread_include_page_zero_correctly(client, files, auth):
    ids = []
    for index in range(29):
        book, _ = register_book(
            files.library, content(f"ui-{index}"), Metadata(language="zh", work_type="Manga")
        )
        ids.append(book.id)
    files.library.save_personal(ids[0], PersonalState(resume_page=0), expected_revision=1)
    login(client)
    first = client.get("/api/books?limit=24&page=1").json()
    last = client.get("/api/books?limit=24&page=2").json()
    assert first["total"] == 29 and len(first["books"]) == 24 and len(last["books"]) == 5
    assert set(book["id"] for book in first["books"]).isdisjoint(book["id"] for book in last["books"])
    assert client.get("/api/books?reading=unread").json()["total"] == 28
    assert client.get("/api/books?reading=in_progress&language=Chinese&work_type=Manga").json()["total"] == 1
    assert client.get("/api/books?page=0").status_code == 422


def test_translations_merge_custom_overrides_and_namespace_completion(client, files, auth):
    tag = Tag(namespace="parody", value="blue archive")
    register_book(files.library, content("translation"), Metadata(tags=[tag]))
    Translations(files.library).import_data(
        {
            "version": 7,
            "head": {"sha": "test"},
            "data": [
                {
                    "namespace": "parody",
                    "data": {"blue archive": {"name": "碧蓝档案"}, "blue angel": {"name": "蓝色天使"}},
                }
            ],
        }
    )
    Organization(files.library).alias(tag, "自定义档案", None)
    login(client)
    value = client.get("/api/tag-suggestions", params={"query": "parody:档案", "database": True}).json()[
        "tags"
    ]
    assert value == [
        {"namespace": "parody", "value": "blue archive", "translation": "自定义档案", "count": 1}
    ]
    value = client.get("/api/tag-suggestions", params={"query": "parody:天使", "database": True}).json()[
        "tags"
    ]
    assert value[0]["value"] == "blue angel" and value[0]["count"] == 0
