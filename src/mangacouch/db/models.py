"""The relational data model (§3.2).

The ``manga/`` folder is the source of truth; this database is a rebuildable index over it. Every
on-disk reference is a path **relative** to the manga root (R2). Identity hash = ``archive.id``
(full-file xxh3-128); dedup hash = ``archive.fingerprint`` (content fingerprint, §4).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base


def _now() -> datetime:
    return datetime.now(UTC)


class Archive(Base):
    __tablename__ = "archive"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # xxh3-128 hex (exact identity)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)  # content dedup hash
    perceptual_hash: Mapped[str | None] = mapped_column(String(64), index=True)  # cover pHash (P1)

    rel_path: Mapped[str] = mapped_column(Text, unique=True)  # relative to manga root (R2)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)  # used with (rel_path, size) to cache the hash (R4)
    format: Mapped[str] = mapped_column(String(8))  # "zip" | "pdf" | "cbz"
    page_count: Mapped[int] = mapped_column(Integer, default=0)

    title: Mapped[str] = mapped_column(Text, default="", index=True)
    title_jpn: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    rating: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str | None] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(64))  # e-hentai gallery category

    # Provenance (set when acquired via Archive Download).
    source_url: Mapped[str | None] = mapped_column(Text, index=True)
    source_gid: Mapped[int | None] = mapped_column(Integer)
    source_token: Mapped[str | None] = mapped_column(String(32))
    uploader: Mapped[str | None] = mapped_column(String(128))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # love/read/favorite counts surfaced on the detail page (e-hentai-derived where available).
    love_count: Mapped[int] = mapped_column(Integer, default=0)
    view_count: Mapped[int] = mapped_column(Integer, default=0)

    cover_status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|ready|error
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    tags: Mapped[list[ArchiveTag]] = relationship(
        back_populates="archive", cascade="all, delete-orphan"
    )
    comments: Mapped[list[Comment]] = relationship(
        back_populates="archive", cascade="all, delete-orphan"
    )
    progress: Mapped[Progress | None] = relationship(
        back_populates="archive", cascade="all, delete-orphan", uselist=False
    )


class Tag(Base):
    __tablename__ = "tag"
    __table_args__ = (UniqueConstraint("namespace", "value", name="uq_tag_ns_value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), default="", index=True)
    value: Mapped[str] = mapped_column(Text, index=True)


class ArchiveTag(Base):
    __tablename__ = "archive_tag"

    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), primary_key=True
    )
    tag_id: Mapped[int] = mapped_column(
        ForeignKey("tag.id", ondelete="CASCADE"), primary_key=True, index=True
    )

    archive: Mapped[Archive] = relationship(back_populates="tags")
    tag: Mapped[Tag] = relationship()


class TagTranslation(Base):
    __tablename__ = "tag_translation"
    __table_args__ = (UniqueConstraint("namespace", "raw", name="uq_trans_ns_raw"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), index=True)
    raw: Mapped[str] = mapped_column(Text, index=True)
    translated: Mapped[str] = mapped_column(Text)
    intro: Mapped[str] = mapped_column(Text, default="")


class Category(Base):
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    type: Mapped[str] = mapped_column(String(8), default="static")  # "static" | "dynamic"
    predicate: Mapped[str] = mapped_column(Text, default="")  # saved-search string for dynamic
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)


class CategoryArchive(Base):
    __tablename__ = "category_archive"

    category_id: Mapped[int] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), primary_key=True
    )
    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), primary_key=True
    )


class FavoriteList(Base):
    __tablename__ = "favorite_list"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0)


class Favorite(Base):
    __tablename__ = "favorite"

    list_id: Mapped[int] = mapped_column(
        ForeignKey("favorite_list.id", ondelete="CASCADE"), primary_key=True
    )
    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), primary_key=True
    )
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Progress(Base):
    __tablename__ = "progress"

    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), primary_key=True
    )
    page: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    archive: Mapped[Archive] = relationship(back_populates="progress")


class History(Base):
    __tablename__ = "history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), index=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)


class Comment(Base):
    __tablename__ = "comment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    archive_id: Mapped[str] = mapped_column(
        ForeignKey("archive.id", ondelete="CASCADE"), index=True
    )
    username: Mapped[str] = mapped_column(String(128), default="")
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content: Mapped[str] = mapped_column(Text, default="")

    archive: Mapped[Archive] = relationship(back_populates="comments")


class DownloadJob(Base):
    __tablename__ = "download_job"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    gid: Mapped[int | None] = mapped_column(Integer)
    token: Mapped[str | None] = mapped_column(String(32))
    domain: Mapped[str | None] = mapped_column(String(32))  # "e-hentai" | "exhentai"
    dltype: Mapped[str] = mapped_column(String(4), default="org")  # "org" | "res"
    catid: Mapped[int | None] = mapped_column(Integer)
    # queued | running | preparing | done | failed
    state: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # Transient-failure retry count (a real column — parsing it out of `error` broke retries).
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    gp_cost: Mapped[int | None] = mapped_column(Integer)
    gp_balance: Mapped[int | None] = mapped_column(Integer)
    archive_id: Mapped[str | None] = mapped_column(String(32))  # set when ingest completes
    error: Mapped[str | None] = mapped_column(Text)
    next_run: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class PluginConfig(Base):
    __tablename__ = "plugin_config"
    __table_args__ = (UniqueConstraint("namespace", "key", name="uq_plugin_ns_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[str] = mapped_column(Text, default="")  # encrypted (token) when is_secret
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)


class AppConfig(Base):
    __tablename__ = "app_config"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")  # JSON-encoded typed value


class AuthCredential(Base):
    __tablename__ = "auth_credential"

    role: Mapped[str] = mapped_column(String(8), primary_key=True)  # "owner" | "reader"
    passcode_hash: Mapped[str | None] = mapped_column(Text)
    api_key_hash: Mapped[str | None] = mapped_column(Text, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AuthSession(Base):
    """A passcode-login session token (hash-only). Lets the PWA authenticate without exposing the
    long-lived owner/reader API keys, which are stored only as hashes (§5.6)."""

    __tablename__ = "auth_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(Text, index=True, unique=True)
    role: Mapped[str] = mapped_column(String(8))  # "owner" | "reader"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
