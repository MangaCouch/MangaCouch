from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from helpers import (
    CANONICAL_TAGS,
    CSRF,
    TAG_QUERIES,
    TYPED_TAGS,
    content,
    login,
    make_zip,
    register_book,
    register_books,
    save_metadata,
    titled,
)
from pydantic import ValidationError

from mangacouch import searching
from mangacouch.database import Library
from mangacouch.models import (
    Metadata,
    PersonalState,
    SourceRating,
    Tag,
)
from mangacouch.organization import Organization
from mangacouch.searching import SearchOptions, search_count, search_terms, tag_suggestions
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
        ({"reading": "unread"}, [0, 1, 3]),
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
                "reading": "unread",
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
    first_book = first.json()["items"][0]["id"]
    second = client.get("/api/books", params={**params, "after": first.json()["after"]}, headers=headers)
    assert second.status_code == 200
    assert {first_book, second.json()["items"][0]["id"]} == {ids[2], ids[3]}
    assert client.get("/api/books", params={"order": "random"}, headers=headers).status_code == 422
    assert client.get("/api/books", params={"source_rating_min": "nan"}, headers=headers).status_code == 422
    assert client.get("/api/books", params=params).status_code == 401


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


@pytest.mark.perf
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
    assert len(large.json()["items"]) == 24
    for book in large.json()["items"]:
        assert book["tag_translations"] == [{"namespace": "artist", "value": "same", "translation": "译名"}]
        assert book["source_check"]["reasons"] == ["source_absent"]
        assert "sidecar" not in book


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
        assert tag_suggestions(db, "作者:blue") == tag_suggestions(db, "artist:blue")
        assert tag_suggestions(db, "作者:蓝")[0]["value"] == "blue sky"
        assert tag_suggestions(db, "社团:blue") == []
        assert tag_suggestions(db, "%' OR 1=1 --") == []
        assert search_count(db, 'artist:"blue sky"', SearchOptions()) == 2
    assert library.search('artist:"blue sky"') == sorted(ids[:2])
    assert library.search("蓝天") == sorted(ids[:2])
    assert library.search('作者:"blue sky"') == sorted(ids[:2])


def test_namespace_aliases_match_the_web_display_names():
    import re

    from mangacouch.models import NAMESPACE_ALIASES

    source = (Path(__file__).parents[1] / "web/src/Pills.tsx").read_text(encoding="utf-8")
    block = source.split("export const namespaceNames", 1)[1].split("};", 1)[0]
    shown = {label: namespace for namespace, label in re.findall(r'(\w+): "([^"]+)"', block)}
    assert shown == NAMESPACE_ALIASES
    # The tag editor resolves the other accepted names to the same keys.
    from mangacouch.models import SEARCH_NAMESPACE_ALIASES

    block = source.split("const namespaceAliases", 1)[1].split("};", 1)[0]
    other = dict(re.findall(r'(\w+): "([^"]+)"', block))
    assert {**shown, **other} == SEARCH_NAMESPACE_ALIASES


def test_tags_written_with_translated_namespaces_and_stray_whitespace_are_stored_canonically(
    client, files, tmp_path
):
    """`作者:蓝天` is stored as `artist:蓝天`; whitespace collapses; duplicates merge."""
    login(client)
    identifier = files.import_file(make_zip(tmp_path / "tags.zip")).book.id
    response = client.put(
        f"/api/books/{identifier}/metadata",
        params={"expected_revision": 1},
        json={"metadata": {"tags": TYPED_TAGS}},
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    stored = [(tag["namespace"], tag["value"]) for tag in response.json()["metadata"]["tags"]]
    assert stored == CANONICAL_TAGS
    library = files.library
    for query in TAG_QUERIES:
        assert library.search(query) == [identifier], query
    with library.connection() as db:
        assert tag_suggestions(db, "作者:蓝")[0] | {"count": 1} == {
            "namespace": "artist",
            "value": "蓝天",
            "count": 1,
            "translation": None,
        }
        assert not db.execute("SELECT 1 FROM book_tag WHERE namespace IN ('作者','社团','coser')").fetchone()


@pytest.mark.parametrize(
    "value", ['say "hi"', "back\\slash", 'both \\"x\\"', "ends with \\", '"', "标签\x02"]
)
def test_tag_values_with_quotes_and_backslashes_round_trip_through_the_search_encoder(library, value):
    """The web app quotes a clicked tag with JSON.stringify; the server reads it back."""
    book, _ = register_book(library, content("quoted"), Metadata(tags=[Tag(namespace="parody", value=value)]))
    register_book(library, content("other"), Metadata(tags=[Tag(namespace="parody", value="say")]))
    encoded = json.dumps(value, ensure_ascii=False)  # what JSON.stringify produces
    assert search_terms(f"parody:{encoded}") == [("parody", value)]
    assert library.search(f"parody:{encoded}") == [book.id]
    assert library.search(f"原作:{encoded} other words") == []
    assert search_terms(f"{encoded} words") == [value.strip(), "words"]


@pytest.mark.parametrize("typed", ["foo\\bar", "a\\nb", "c:\\temp\\x", "\\u12"])
def test_typed_backslashes_in_quotes_stay_literal(typed):
    """Only the escapes JSON.stringify writes are decoded; `\\b`, `\\n` and the like are text."""
    assert search_terms(f'"{typed}"') == [typed]
    assert search_terms(f'parody:"{typed}"') == [("parody", typed)]


def test_category_alias_searches_the_work_type_or_a_category_tag(library):
    """`分类:漫画` (the shown name of a `category` tag) is a usable query."""
    manga, _ = register_book(library, content("manga"), Metadata(work_type="Manga"))
    tagged, _ = register_book(
        library, content("tagged"), Metadata(tags=[Tag(namespace="分类", value="漫画")])
    )
    doujin, _ = register_book(library, content("doujin"), Metadata(work_type="Doujinshi"))
    assert library.get_book(tagged.id).metadata.tags == [Tag(namespace="category", value="漫画")]
    assert search_terms("分类:漫画") == [("category", "漫画")]
    assert library.search("分类:漫画") == sorted([manga.id, tagged.id])
    assert library.search("category:Manga") == [manga.id]
    assert library.search("分类:同人志") == library.search('category:"doujinshi"') == [doujin.id]
    assert library.search("分类:missing") == []


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
    assert first["total"] == 29 and len(first["items"]) == 24 and len(last["items"]) == 5
    assert set(book["id"] for book in first["items"]).isdisjoint(book["id"] for book in last["items"])
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
        "items"
    ]
    assert value == [
        {"namespace": "parody", "value": "blue archive", "translation": "自定义档案", "count": 1}
    ]
    value = client.get("/api/tag-suggestions", params={"query": "parody:天使", "database": True}).json()[
        "items"
    ]
    assert value[0]["value"] == "blue angel" and value[0]["count"] == 0


def test_translated_names_drop_icons_keep_emoji_and_match_without_them(files):
    air = register_book(files.library, content("air"), Metadata(tags=[Tag(namespace="parody", value="air")]))[
        0
    ]
    touhou = register_book(
        files.library, content("touhou"), Metadata(tags=[Tag(namespace="parody", value="touhou project")])
    )[0]
    Translations(files.library).import_data(
        {
            "version": 7,
            "head": {"sha": "icons"},
            "data": [
                {
                    "namespace": "parody",
                    "data": {
                        "air": {"name": "AIR🕊"},
                        "touhou project": {
                            "name": "![阴阳玉图标](https://example.invalid/i.webp)东方Project"
                        },
                        "icon only": {"name": "![图](https://example.invalid/x.jpg)"},
                    },
                }
            ],
        }
    )
    with files.library.connection() as db:
        labels = dict(db.execute("SELECT value,translation FROM tag_translation").fetchall())
        assert labels == {"air": "AIR🕊", "touhou project": "东方Project"}
        assert tag_suggestions(db, "东方")[0]["translation"] == "东方Project"
        assert tag_suggestions(db, "原作:air🕊")[0]["value"] == "air"
    assert files.library.search("东方project") == [touhou.id]
    assert files.library.search("AIR🕊") == [air.id]
    # EhTagTranslation's own namespace names work too.
    assert files.library.search('艺术家:"x"') == []
    assert search_terms("团队:abc") == [("group", "abc")]


@pytest.mark.perf
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


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Re:Zero", ["re:zero"]),
        ("進撃の巨人：完結編", ["進撃の巨人:完結編"]),
        ("Title: Sub", ["title:", "sub"]),
        ('artist:"blue sky"', [("artist", "blue sky")]),
        ('series:"x y" plain', [("series", "x y"), "plain"]),
        ("artist:alice", [("artist", "alice")]),
        ("作者:alice", [("artist", "alice")]),
        ('作者："blue sky"', [("artist", "blue sky")]),
        ("原作：xyz", [("parody", "xyz")]),
        ('"quoted phrase"', ["quoted phrase"]),
    ],
)
def test_colon_terms_are_tags_only_for_known_namespaces_or_quoted_values(query, expected):
    assert search_terms(query) == expected


def test_colon_titles_are_found_by_text_search(library: Library):
    zero = titled(library, "Re:Zero")
    finale = titled(library, "進撃の巨人：完結編")
    sub = titled(library, "Title: Sub")
    tagged = titled(library, "Tagged", Tag(namespace="artist", value="blue sky"))
    assert library.search("Re:Zero") == [zero.id]
    assert library.search("進撃の巨人：完結編") == [finale.id]
    assert library.search("Title: Sub") == [sub.id]
    assert library.search('artist:"blue sky"') == [tagged.id]
    assert library.search("artist:blue") == []


def test_tag_suggestions_count_only_matching_tags(library: Library):
    for index in range(3):
        titled(
            library, f"b{index}", Tag(namespace="artist", value="alpha"), Tag(namespace="group", value="beta")
        )
    with library.connection() as db:
        rows = tag_suggestions(db, "alp")
        assert rows == [{"namespace": "artist", "value": "alpha", "count": 3, "translation": None}]
        assert [row["value"] for row in tag_suggestions(db, "group:")] == ["beta"]


def test_book_list_counts_only_the_first_page_and_detail_adds_page_sizes(api, files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "sized.zip")).book
    files.import_file(make_zip(tmp_path / "other.zip", ["x.png"]))
    first = api.get("/api/books", params={"limit": 1}).json()
    assert first["total"] == 2
    rest = api.get("/api/books", params={"limit": 1, "after": first["after"]}).json()
    assert rest["total"] is None and len(rest["items"]) == 1
    assert "page_sizes" not in first["items"][0] and "comments" not in first["items"][0]["metadata"]
    detail = api.get(f"/api/books/{book.id}").json()
    assert detail["page_sizes"] == [[12, 8]] * book.content.page_count


def test_cjk_and_unicode_search_includes_original_names_without_requiring_metadata(library: Library):
    first, _ = register_book(library, content("one", "[C89] 心海.zip"))
    second, _ = register_book(
        library, content("two"), Metadata(title="ＡＢＣ 明日方舟 カタカナ 한글 cafe\u0301")
    )
    expectations = {
        "心": [first.id],
        "心海": [first.id],
        "c89 心海": [first.id],
        "abc": [second.id],
        "方舟": [second.id],
        "カ": [second.id],
        "한": [second.id],
        "café": [second.id],
        "不存在": [],
        "%": [],
    }
    for query, expected in expectations.items():
        assert library.search(query) == expected


def test_large_match_set_uses_stable_pagination(library: Library):
    expected = {
        book.id
        for book in register_books(library, (content(str(i)) for i in range(1001)), Metadata(title="共同"))
    }
    found: list[str] = []
    while page := library.search("共同", limit=200, after_id=found[-1] if found else ""):
        found.extend(page)
    assert len(found) == len(expected) and set(found) == expected
    assert found == sorted(found)


def test_selection_reading_state_honours_filters_and_exclusions(api, files):
    ids = populate(files.library)
    login(api)

    def unfinished(exclude=(), options=None, query="ABC"):
        response = api.post(
            "/api/books/selection-state",
            json={"query": query, "options": options or {}, "exclude": list(exclude)},
            headers=CSRF,
        )
        assert response.status_code == 200
        return response.json()["unfinished"]

    assert unfinished()
    assert unfinished([ids[0], ids[1]])
    assert not unfinished([ids[0], ids[1], ids[3]])
    assert not unfinished(options={"reading": "completed"})
    assert not unfinished([ids[3]], {"rating": "rated", "min_rating": 4.5})
    assert unfinished(["missing"], {"reading": "unread"})
    assert not unfinished(query="no-such-title")
    invalid = api.post("/api/books/selection-state", json={"exclude": ["x"] * 501}, headers=CSRF)
    assert invalid.status_code == 422


def test_free_text_alias_search_uses_tag_index_on_a_large_library(library):
    """Aliases are filtered first, then book_tag is probed by (namespace,value); never scanned."""
    count = 20_000
    namespaces = ("artist", "group", "female", "male", "parody", "other")
    with library.transaction() as db:
        db.executemany(
            "INSERT INTO book(id,sha256,content_json,metadata_json,search_text) VALUES(?,?,'{}','{}',?)",
            ((f"b{i:05}", f"{i:064x}", f"title {i}") for i in range(count)),
        )
        db.executemany(
            "INSERT INTO personal(book_id,state_json) VALUES(?,?)",
            ((f"b{i:05}", PersonalState().model_dump_json()) for i in range(count)),
        )
        db.executemany(
            "INSERT INTO book_tag VALUES(?,?,?)",
            (
                (f"b{i:05}", namespace, f"v{(i * 7 + k) % 3000}")
                for i in range(count)
                for k, namespace in enumerate(namespaces)
            ),
        )
        db.executemany(
            "INSERT INTO tag_translation VALUES('female',?,?,?)",
            ((f"v{j}", f"译{j}", f"v{j}\n译{j}") for j in range(3000)),
        )
        expected = {
            row[0]
            for row in db.execute(
                "SELECT book_id FROM book_tag "
                "WHERE namespace='female' AND (value='v123' OR value LIKE 'v123_')"
            )
        }
    with library.connection() as db:
        conditions, values = searching.rule_conditions("译123", SearchOptions())
        plan = [
            row[3]
            for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT count(*) FROM book b JOIN personal p ON p.book_id=b.id WHERE "
                + " AND ".join(conditions),
                values,
            )
        ]
        assert "SEARCH book_tag USING COVERING INDEX book_tag_value (namespace=? AND value=?)" in plan, plan
        assert not any(step.startswith("SCAN book_tag") for step in plan), plan
        started = time.perf_counter()
        assert search_count(db, "译123", SearchOptions()) == len(expected) > 0
        found, after = [], ""
        while page := searching.search_ids(db, "译123", limit=200, after_id=after, options=None):
            found.extend(page)
            after = page[-1]
        assert set(found) == expected
        # Generous for slow CI; scanning book_tag for every search took seconds at this size.
        assert time.perf_counter() - started < 2.0
        top = tag_suggestions(db, "译123")[0]
        assert (top["namespace"], top["value"], top["translation"]) == ("female", "v123", "译123")


@pytest.mark.parametrize("namespace", ["custom group", "custom:group", 'custom"group', r"custom\group"])
def test_quoted_custom_namespace_roundtrips(library, namespace):
    book, _ = register_book(
        library,
        content("custom-tag", "custom.cbz"),
        Metadata(title="Custom tag", tags=[Tag(namespace=namespace, value='a "quoted" value')]),
    )
    query = f"{json.dumps(namespace)}:{json.dumps(book.metadata.tags[0].value)}"
    assert search_terms(query) == [(book.metadata.tags[0].namespace, book.metadata.tags[0].value)]
    with library.connection() as db:
        assert search_count(db, query, SearchOptions()) == 1
