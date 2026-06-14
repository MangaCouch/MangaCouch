"""Search: the ``namespace:value`` query syntax (§5.1) over an FTS5 trigram index."""

from __future__ import annotations

from .index import SearchIndex
from .query import Query, parse_query
from .service import SearchResult, search_archives

__all__ = ["Query", "SearchIndex", "SearchResult", "parse_query", "search_archives"]
