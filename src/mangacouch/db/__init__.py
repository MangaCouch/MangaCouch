"""The authoritative relational store (``database/library.sqlite``).

The search index (``cache/search.sqlite``) and the thumbnail blob store (``cache/thumbs.sqlite``)
are separate, rebuildable SQLite files managed by their own modules — not part of this package.
"""

from __future__ import annotations

from .base import Base, get_engine, get_sessionmaker, init_engine, session_scope

__all__ = ["Base", "get_engine", "get_sessionmaker", "init_engine", "session_scope"]
