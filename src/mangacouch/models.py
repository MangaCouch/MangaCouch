from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

from .errors import InvalidInput

MAX_METADATA_BYTES = 256 * 1024
STORED = {"stored": True}
"""Validation context of documents read back from the database, which were checked when written."""
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Text = Annotated[str, Field(max_length=4096)]
FieldName = Literal[
    "title",
    "original_title",
    "language",
    "translated",
    "work_type",
    "tags",
    "source",
    "uploader",
    "uploaded_at",
    "source_rating",
    "declared_page_count",
    "declared_byte_size",
    "parent",
    "newer_versions",
]


# Tag namespaces that a bare `namespace:value` search term filters on (E-Hentai/nHentai sets).
TAG_NAMESPACES = frozenset(
    {
        "artist",
        "character",
        "cosplayer",
        "female",
        "group",
        "language",
        "location",
        "male",
        "mixed",
        "other",
        "parody",
        "reclass",
        "tag",
        "temp",
    }
)


# Display names of the namespaces (the web app shows the same ones), accepted in searches
# as `作者:name` alongside `artist:name`.
NAMESPACE_ALIASES = {
    "作者": "artist",
    "社团": "group",
    "原作": "parody",
    "角色": "character",
    "女性": "female",
    "男性": "male",
    "混合": "mixed",
    "其他": "other",
    "语言": "language",
    "角色扮演者": "cosplayer",
    "类别": "reclass",
    "地点": "location",
    "临时": "temp",
    "标签": "tag",
    "分类": "category",
}


# EhTagTranslation's own names for the namespaces, also accepted in searches.
SEARCH_NAMESPACE_ALIASES = {
    **NAMESPACE_ALIASES,
    "艺术家": "artist",
    "团队": "group",
    "重新分类": "reclass",
    "coser": "cosplayer",
}


def canonical_namespace(namespace: str) -> str:
    """The stored namespace for a (normalized) English or translated namespace name."""
    return SEARCH_NAMESPACE_ALIASES.get(namespace, namespace)


# Markdown images (EhTagTranslation names carry icons such as `![阴阳玉图标](…)东方Project`)
# and stray HTML tags.
_LABEL_MARKUP = re.compile(r"!\[[^\]]*\]\([^)]*\)|<[^>]*>")


def display_label(text: str) -> str:
    """A translated tag name as shown: icons and markup removed, emoji kept."""
    return " ".join(_LABEL_MARKUP.sub("", text).split())


def label_key(text: str) -> str:
    """Matching form of a translated name: normalized, without emoji and symbols, so
    `air` finds `AIR🕊`. Falls back to the normalized text when nothing else is left."""
    text = normalized(display_label(text))
    kept = "".join(
        char
        for char in text
        if unicodedata.category(char) not in ("So", "Sk", "Cf", "Co") and char not in "\ufe0e\ufe0f"
    )
    return " ".join(kept.split()) or text


def normalized(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Model(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        validate_assignment=True,
        revalidate_instances="never",
        hide_input_in_errors=True,
    )


def tag_value(value: str) -> str:
    """A tag value as stored: runs of whitespace (including line breaks) become one space."""
    return " ".join(value.split())


class Tag(Model):
    namespace: Annotated[str, Field(max_length=128)] = "other"
    value: Annotated[str, Field(min_length=1, max_length=512)]

    @field_validator("namespace", mode="before")
    @classmethod
    def other_namespace(cls, value):
        """Store a translated namespace name (`作者`) as the key searches use (`artist`)."""
        if not isinstance(value, str):
            return value
        value = value.strip()
        canonical = canonical_namespace(normalized(value))
        return canonical if canonical != normalized(value) else value or "other"

    @field_validator("value", mode="before")
    @classmethod
    def collapse_whitespace(cls, value):
        return tag_value(value) if isinstance(value, str) else value


EH_SITES: tuple[str, ...] = ("ehentai", "exhentai")
"""E-Hentai and ExHentai: one backend and one gallery namespace, so they share pacing, IP bans,
rate-limit halts and gallery identity."""
EH_SITES_SQL = "(" + ",".join(f"'{site}'" for site in EH_SITES) + ")"
"""EH_SITES as an SQL list for `site IN ...` conditions."""


def site_group(site: str) -> tuple[str, ...]:
    """Sites that share pacing, halts and gallery identity with `site`."""
    return EH_SITES if site in EH_SITES else (site,)


class Source(Model):
    site: Literal["ehentai", "exhentai", "nhentai"]
    gallery_id: Annotated[int, Field(gt=0, le=2**63 - 1)]
    gallery_token: Annotated[str, Field(pattern=r"^[a-zA-Z0-9]{1,64}$")] | None = None

    @field_validator("gallery_token")
    @classmethod
    def normalize_token(cls, value: str | None) -> str | None:
        return value.lower() if value else value

    @model_validator(mode="after")
    def check_token(self) -> Self:
        if (self.site == "nhentai") != (self.gallery_token is None):
            raise ValueError("EH requires a gallery token; nHentai has no gallery token")
        return self

    @property
    def key(self) -> str:
        return f"{site_group(self.site)[0]}:{self.gallery_id}"

    @property
    def url(self) -> str:
        if self.site == "nhentai":
            return f"https://nhentai.net/g/{self.gallery_id}/"
        host = "e-hentai.org" if self.site == "ehentai" else "exhentai.org"
        return f"https://{host}/g/{self.gallery_id}/{self.gallery_token}/"


class SourceRating(Model):
    average: Annotated[float, Field(ge=0, le=5, allow_inf_nan=False)]
    count: Annotated[int, Field(ge=0)] | None = None


class SourceComment(Model):
    source: Source
    comment_id: Annotated[str, Field(min_length=1, max_length=128)]
    author: Annotated[str, Field(max_length=256)] = ""
    body: Annotated[str, Field(max_length=8192)]
    posted_at: int | None = None
    score: int | None = None
    is_uploader: bool = False


class SourceNotes(Model):
    source: Source
    tags: Annotated[list[Tag], Field(max_length=2048)] = Field(default_factory=list)
    fetched_at: int | None
    """None until the source was fetched: such notes only record a failed deferred comment fetch."""
    comments_sort: Literal["source", "newest"] = "source"
    comments_pages: Annotated[int, Field(ge=0, le=5)] = 0
    comments_complete: bool = False


class Metadata(Model):
    title: Text | None = None
    original_title: Text | None = None
    language: Annotated[str, Field(max_length=128)] | None = None
    translated: bool | None = None
    work_type: Annotated[str, Field(max_length=128)] | None = None
    tags: Annotated[list[Tag], Field(max_length=2048)] = Field(default_factory=list)
    source: Source | None = None
    uploader: Annotated[str, Field(max_length=512)] | None = None
    uploaded_at: Annotated[str, Field(max_length=40)] | None = None
    source_rating: SourceRating | None = None
    declared_page_count: Annotated[int, Field(gt=0)] | None = None
    declared_byte_size: Annotated[int, Field(ge=0)] | None = None
    parent: Source | None = None
    newer_versions: Annotated[list[Source], Field(max_length=256)] = Field(default_factory=list)
    field_sources: dict[FieldName, Literal["manual", "eze:info", "eze:full", "native", "online"]] = Field(
        default_factory=dict
    )
    protected_fields: list[FieldName] = Field(default_factory=list)

    @field_validator("uploaded_at")
    @classmethod
    def timestamp_has_timezone(cls, value: str | None) -> str | None:
        if value is not None and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("Source timestamp requires an explicit timezone")
        return value

    @field_validator("tags", mode="before")
    @classmethod
    def drop_blank_tags(cls, tags):
        """A value of only whitespace is no tag; such entries are dropped rather than refused."""
        if not isinstance(tags, list):
            return tags

        def blank(tag) -> bool:
            value = tag.get("value") if isinstance(tag, dict) else getattr(tag, "value", None)
            return isinstance(value, str) and not value.split()

        return [tag for tag in tags if not blank(tag)]

    @field_validator("tags")
    @classmethod
    def deduplicate_tags(cls, tags: list[Tag]) -> list[Tag]:
        by_key = {(normalized(t.namespace), normalized(t.value)): t for t in reversed(tags)}
        return [by_key[key] for key in sorted(by_key)]

    @model_validator(mode="after")
    def size_is_bounded(self, info: ValidationInfo) -> Self:
        if info.context is STORED:
            return self
        if len(canonical_json(self.model_dump(mode="json")).encode()) > MAX_METADATA_BYTES:
            raise ValueError("Metadata exceeds the supported byte budget")
        return self

    @classmethod
    def stored(cls, data: str) -> Metadata:
        """Metadata as the database holds it: it was within the byte budget when it was written."""
        return cls.model_validate_json(data, context=STORED)


class Content(Model):
    sha256: SHA256
    format: Literal["cbz", "pdf"]
    relative_path: Annotated[str, Field(min_length=1, max_length=4096)]
    original_filename: Annotated[str, Field(min_length=1, max_length=4096)]
    byte_size: Annotated[int, Field(gt=0)]
    page_count: Annotated[int, Field(gt=0)]

    @field_validator("relative_path")
    @classmethod
    def relative_file_only(cls, value: str) -> str:
        posix, windows = PurePosixPath(value), PureWindowsPath(value)
        if (
            posix.is_absolute()
            or windows.drive
            or windows.root
            or "\\" in value
            or any(p in {"", ".", ".."} for p in value.split("/"))
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError("Expected a relative library file path")
        return value

    @property
    def managed_path(self) -> str:
        suffix = "pdf" if self.format == "pdf" else "cbz"
        return f"{self.sha256[:2]}/{self.sha256}.{suffix}"


class PersonalState(Model):
    rating: Annotated[float, Field(ge=0, le=5, multiple_of=0.5, allow_inf_nan=False)] | None = None
    resume_page: Annotated[int, Field(ge=0)] | None = None
    completed: bool = False


def protocol_progress(state: PersonalState, page_count: int) -> tuple[int | None, bool]:
    """Reading progress as OPDS-PSE and Komga clients see it: (1-based page read up to, finished).

    A finished book reports its last page on both protocols, however it was finished (read to
    the end, or marked read midway) and wherever the web reader would reopen it: the completed
    flag is the read state, and the stored resume page (kept for the web reader and for undoing
    "mark read") is not shown as unfinished progress. An unfinished book reports its resume page;
    an unread one reports nothing.
    """
    if state.completed:
        return page_count, True
    if state.resume_page is None:
        return None, False
    return min(state.resume_page + 1, page_count), False


class NativeSidecar(Model):
    schema_name: Literal["mangacouch.native/1"] = Field(alias="schema")
    content_sha256: SHA256
    revision: Annotated[int, Field(ge=1)]
    metadata: Metadata

    def canonical_bytes(self) -> bytes:
        return canonical_json(self.model_dump(mode="json", by_alias=True)).encode("utf-8")

    def semantic_hash(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def read_native(data: bytes, expected_sha256: str) -> NativeSidecar:
    if len(data) > MAX_METADATA_BYTES + 4096:
        raise InvalidInput("Sidecar exceeds the supported byte budget")
    sidecar = NativeSidecar.model_validate_json(data)
    if sidecar.content_sha256 != expected_sha256:
        raise InvalidInput("Sidecar belongs to different file content")
    return sidecar
