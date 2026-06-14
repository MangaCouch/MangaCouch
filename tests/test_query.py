"""Query-syntax parsing (§5.1)."""

from __future__ import annotations

from mangacouch.search.query import MatchType, parse_query


def test_comma_and_namespace():
    q = parse_query("artist:foo, school")
    assert len(q.text_terms) == 2
    assert q.text_terms[0].namespace == "artist"
    assert q.text_terms[0].value == "foo"
    assert q.text_terms[1].namespace is None
    assert q.text_terms[1].value == "school"


def test_negation():
    q = parse_query("-language:japanese")
    assert q.text_terms[0].negate is True
    assert q.text_terms[0].namespace == "language"


def test_exact_quoted_and_dollar():
    q = parse_query('"big title", value$')
    assert q.text_terms[0].match is MatchType.EXACT
    assert q.text_terms[0].value == "big title"
    assert q.text_terms[1].match is MatchType.EXACT
    assert q.text_terms[1].value == "value"


def test_wildcards():
    for token in ("scho*l", "scho?l", "scho%l", "scho_l"):
        q = parse_query(token)
        assert q.text_terms[0].match is MatchType.WILDCARD


def test_numeric_predicates():
    q = parse_query("pages:>20, read:<=5, pages:=10")
    assert (q.numeric_terms[0].field, q.numeric_terms[0].op, q.numeric_terms[0].value) == (
        "pages",
        ">",
        20,
    )
    assert (q.numeric_terms[1].field, q.numeric_terms[1].op, q.numeric_terms[1].value) == (
        "read",
        "<=",
        5,
    )
    assert q.numeric_terms[2].op == "="


def test_filters():
    q = parse_query("newonly, untaggedonly, hidecompleted, foo")
    assert q.filters.newonly and q.filters.untaggedonly and q.filters.hidecompleted
    assert len(q.text_terms) == 1


def test_url_value_keeps_colon():
    q = parse_query("source:https://e-hentai.org/g/1/abc/")
    assert q.text_terms[0].namespace == "source"
    assert q.text_terms[0].value == "https://e-hentai.org/g/1/abc/"


def test_empty_query():
    assert parse_query("").is_empty
    assert parse_query(None).is_empty
