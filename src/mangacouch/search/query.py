"""Parse the MangaCouch / LANraragi query syntax (§5.1) into a structured ``Query``.

Syntax (preserved exactly):
- comma-separated tokens, AND-combined;
- ``namespace:value`` (namespace-anchored) vs a bare term (matches across all namespaces);
- negation ``-term``; exact match ``"…"`` or a trailing ``$``;
- wildcards ``*`` / ``%`` (multiple chars), ``?`` / ``_`` (single char);
- numeric predicates ``pages:>N``, ``pages:<=N``, ``read:>=N``;
- filter tokens ``newonly``, ``untaggedonly``, ``hidecompleted``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

# Namespaces that don't count an archive as "tagged" (used by the untagged filter). Configuration,
# not hard-coded behaviour (§5.1).
BASIC_NAMESPACES = frozenset(
    {"artist", "parody", "series", "language", "event", "group", "date_added", "timestamp", "source"}
)

_NUMERIC_FIELDS = {"pages", "read"}
_FILTER_TOKENS = {"newonly", "untaggedonly", "hidecompleted"}
_WILDCARD_CHARS = set("*?%_")


class MatchType(Enum):
    SUBSTRING = "substring"
    EXACT = "exact"
    WILDCARD = "wildcard"


@dataclass(slots=True)
class TextTerm:
    value: str
    namespace: str | None = None  # None = bare (any namespace + title)
    negate: bool = False
    match: MatchType = MatchType.SUBSTRING


@dataclass(slots=True)
class NumericTerm:
    field: str  # "pages" | "read"
    op: str  # one of < <= > >= =
    value: int
    negate: bool = False


@dataclass(slots=True)
class Filters:
    newonly: bool = False
    untaggedonly: bool = False
    hidecompleted: bool = False


@dataclass(slots=True)
class Query:
    text_terms: list[TextTerm] = field(default_factory=list)
    numeric_terms: list[NumericTerm] = field(default_factory=list)
    filters: Filters = field(default_factory=Filters)
    raw: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.text_terms and not self.numeric_terms


_NUM_RE = re.compile(r"^(<=|>=|<|>|=)?\s*(\d+)$")


def _split_tokens(q: str) -> list[str]:
    """Split on commas, respecting double-quoted spans (which may contain commas)."""
    tokens: list[str] = []
    buf: list[str] = []
    in_quote = False
    for ch in q:
        if ch == '"':
            in_quote = not in_quote
            buf.append(ch)
        elif ch == "," and not in_quote:
            tokens.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        tokens.append("".join(buf))
    return [t.strip() for t in tokens if t.strip()]


def _parse_numeric(namespace: str, value: str, negate: bool) -> NumericTerm | None:
    m = _NUM_RE.match(value.strip())
    if not m:
        return None
    op = m.group(1) or "="
    return NumericTerm(field=namespace, op=op, value=int(m.group(2)), negate=negate)


def parse_query(q: str | None) -> Query:
    query = Query(raw=q or "")
    if not q:
        return query

    for token in _split_tokens(q):
        negate = token.startswith("-")
        if negate:
            token = token[1:].strip()
        if not token:
            continue

        low = token.lower()
        if low in _FILTER_TOKENS and not negate:
            setattr(query.filters, low, True)
            continue

        # Split namespace:value (only the first colon; values may contain colons e.g. URLs).
        namespace: str | None = None
        value = token
        if ":" in token:
            ns, _, rest = token.partition(":")
            ns = ns.strip().lower()
            if ns and not ns.startswith('"'):
                namespace = ns
                value = rest.strip()

        # Numeric predicate?
        if namespace in _NUMERIC_FIELDS:
            num = _parse_numeric(namespace, value, negate)
            if num is not None:
                query.numeric_terms.append(num)
                continue

        # Exact match: quoted "…" or a trailing $.
        match = MatchType.SUBSTRING
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
            match = MatchType.EXACT
        elif value.endswith("$"):
            value = value[:-1]
            match = MatchType.EXACT
        elif _WILDCARD_CHARS & set(value):
            match = MatchType.WILDCARD

        value = value.strip()
        if not value:
            continue
        query.text_terms.append(
            TextTerm(value=value, namespace=namespace, negate=negate, match=match)
        )

    return query
