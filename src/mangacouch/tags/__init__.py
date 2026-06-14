"""Namespaced tags + EhTagTranslation localisation (§5.2, Appendix A)."""

from __future__ import annotations

from .translation import (
    TAGDB_URL,
    TagTranslator,
    fetch_tagdb,
    ingest_tagdb,
    parse_tag,
)

__all__ = ["TAGDB_URL", "TagTranslator", "fetch_tagdb", "ingest_tagdb", "parse_tag"]
