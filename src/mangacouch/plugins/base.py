"""Plugin contracts (§5.4) — four typed ABCs, a typed ``PluginInfo``, and typed contexts/results.

Trust model: a single owner trusts all plugins; plugins run **in-process** (no sandbox). Metadata is
validated at import; contexts and results are typed objects, not loose dicts. Named parameters only.
A plugin's ``cooldown`` is advisory here and **enforced server-side** by the rate limiter (§5.3).
"""

from __future__ import annotations

import enum
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field


class PluginError(Exception):
    """A clean, user-facing plugin failure (e.g. 'Insufficient funds', 'out of GP')."""


class PluginType(enum.StrEnum):
    LOGIN = "login"
    DOWNLOAD = "download"
    METADATA = "metadata"
    SCRIPT = "script"


class PluginParam(BaseModel):
    name: str
    type: str = "string"  # string | int | bool | password
    description: str = ""
    default: Any = None
    secret: bool = False  # stored encrypted at rest (§5.6)


class PluginInfo(BaseModel):
    """Validated plugin metadata (the ``plugin_info()`` payload)."""

    namespace: str = Field(pattern=r"^[a-z0-9_]+$")
    name: str
    type: PluginType
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    parameters: list[PluginParam] = Field(default_factory=list)
    cooldown: float = 0.0  # advisory seconds between calls; enforced by the rate limiter
    login_from: str | None = None  # namespace of a Login plugin whose session to inject
    url_regex: str | None = None  # download plugins: which URLs they claim


class BasePlugin(ABC):
    @abstractmethod
    def plugin_info(self) -> PluginInfo: ...


# --- Login -----------------------------------------------------------------------------------


@dataclass(slots=True)
class LoginContext:
    config: dict[str, Any]  # decrypted plugin_config for this plugin
    user_agent: str
    proxy: str | None = None
    proxy_mounts: dict[str, Any] | None = None


class LoginPlugin(BasePlugin):
    """Returns a configured HTTP session; the authenticated session is cached, not rebuilt per run."""

    @abstractmethod
    def do_login(self, ctx: LoginContext) -> httpx.Client: ...


# --- Download --------------------------------------------------------------------------------


@dataclass(slots=True)
class DownloadContext:
    url: str
    config: dict[str, Any]
    session: httpx.Client  # injected from the referenced Login plugin (login_from)
    dest_dir: Path  # where to stream the fetched archive (a staging dir under cache/)
    dltype: str = "org"  # "org" | "res"
    on_progress: Callable[[float], None] | None = None


@dataclass(slots=True)
class DownloadResult:
    """A download yields a saved archive path, OR a URL to fetch, OR an error."""

    archive_path: Path | None = None
    url: str | None = None
    suggested_filename: str | None = None
    gallery_meta: dict[str, Any] | None = None
    error: str | None = None
    gp_cost: int | None = None
    gp_balance: int | None = None


class DownloadPlugin(BasePlugin):
    @abstractmethod
    def matches(self, url: str) -> bool: ...

    @abstractmethod
    def download(self, ctx: DownloadContext) -> DownloadResult: ...

    def provide_url(self, ctx: DownloadContext) -> DownloadResult:
        """Optional: resolve the final archive URL without fetching (default = delegate)."""
        return self.download(ctx)


# --- Metadata --------------------------------------------------------------------------------


@dataclass(slots=True)
class MetadataContext:
    archive_id: str
    title: str
    source_url: str | None
    config: dict[str, Any]
    session: httpx.Client | None = None
    file_path: Path | None = None


@dataclass(slots=True)
class MetadataResult:
    tags: list[str] = field(default_factory=list)  # "namespace:value"
    title: str | None = None
    summary: str | None = None
    error: str | None = None


class MetadataPlugin(BasePlugin):
    @abstractmethod
    def get_tags(self, ctx: MetadataContext) -> MetadataResult: ...


# --- Script ----------------------------------------------------------------------------------


@dataclass(slots=True)
class ScriptContext:
    config: dict[str, Any]
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ScriptResult:
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class ScriptPlugin(BasePlugin):
    @abstractmethod
    def run_script(self, ctx: ScriptContext) -> ScriptResult: ...


# --- ML extension points (API surface only, §5.5) -------------------------------------------


@dataclass(slots=True)
class PageProcessContext:
    """Passed to a page-processing hook in the image-serving path."""

    archive_id: str
    page_index: int
    page_path: str
    image_bytes: bytes
    mime: str
    target_language: str | None = None


@dataclass(slots=True)
class PageProcessResult:
    """A hook may (a) replace the page image, or (b) attach overlay data for the browser to render."""

    image_bytes: bytes | None = None
    mime: str | None = None
    overlay: list[dict[str, Any]] | None = None  # [{bbox, text, translated}, ...]


class PageProcessHook(ABC):
    """Auto-translation hook signature (no v1 implementation — interface only)."""

    @abstractmethod
    def process_page(self, ctx: PageProcessContext) -> PageProcessResult: ...
