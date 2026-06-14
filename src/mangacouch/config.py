"""Configuration: the four path roots + runtime settings (§3.1, §6.2).

``config.toml`` lives next to the executable (or in the database root). Every path is stored
*relative* (R2) and resolved against the executable directory at startup, so the whole install
relocates across machines and removable drives. Runtime settings that the UI can change live in the
``app_config`` table; this module holds the bootstrap settings needed to *find* and *open* the DB.
"""

from __future__ import annotations

import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MangaCouch/0.1"

CONFIG_FILENAME = "config.toml"
SECRETS_KEYFILE = "secrets.key"
LIBRARY_DB = "library.sqlite"
SEARCH_DB = "search.sqlite"
THUMBS_DB = "thumbs.sqlite"
PAGE_CACHE_DIR = "pagecache"
TAGDB_FILE = "ehtags.json"


def executable_dir() -> Path:
    """The directory the app runs from.

    For a PyInstaller ``--onedir`` build this is the folder beside ``database/ cache/ manga/``;
    for a normal install we fall back to the current working directory so ``mangacouch serve`` run
    from a project folder behaves intuitively.
    """
    if getattr(sys, "frozen", False):  # PyInstaller
        return Path(sys.executable).resolve().parent
    return Path.cwd()


@dataclass(slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: list[str] = field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )


@dataclass(slots=True)
class AcquisitionConfig:
    proxy: str = ""
    proxy_scope: str = "all"  # "all" | "exhentai-only"
    default_dltype: str = "org"  # "org" | "res"
    gp_short_behavior: str = "block"  # "block" | "resample"
    rate_limit_interval_seconds: float = 5.0
    rate_limit_concurrency: int = 1
    tag_refresh_hours: int = 24
    user_agent: str = DEFAULT_USER_AGENT


@dataclass(slots=True)
class ReaderConfig:
    default_mode: str = "scroll"
    default_direction: str = "rtl"
    default_fit: str = "width"
    default_preload: int = 2
    theme: str = "dark"
    language: str = "en"


@dataclass(slots=True)
class ThumbnailConfig:
    prewarm: str = "lazy"  # "lazy" | "full"
    cover_size: int = 512
    page_size: int = 320
    quality: int = 80
    max_cache_mb: int = 0


@dataclass(slots=True)
class AuthConfig:
    auto_lock_minutes: int = 15


@dataclass(slots=True)
class Config:
    """Resolved configuration. All ``*_root`` are absolute, derived from relative on-disk values."""

    base_dir: Path
    database_root: Path
    cache_root: Path
    manga_root: Path
    server: ServerConfig = field(default_factory=ServerConfig)
    acquisition: AcquisitionConfig = field(default_factory=AcquisitionConfig)
    reader: ReaderConfig = field(default_factory=ReaderConfig)
    thumbnails: ThumbnailConfig = field(default_factory=ThumbnailConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    # The original relative path strings, preserved so we can re-serialise without absolutising.
    _rel_paths: dict[str, str] = field(default_factory=dict)

    # --- derived file locations -------------------------------------------------------------
    @property
    def library_db_path(self) -> Path:
        return self.database_root / LIBRARY_DB

    @property
    def secrets_keyfile_path(self) -> Path:
        return self.database_root / SECRETS_KEYFILE

    @property
    def search_db_path(self) -> Path:
        return self.cache_root / SEARCH_DB

    @property
    def thumbs_db_path(self) -> Path:
        return self.cache_root / THUMBS_DB

    @property
    def page_cache_dir(self) -> Path:
        return self.cache_root / PAGE_CACHE_DIR

    @property
    def tagdb_path(self) -> Path:
        return self.cache_root / TAGDB_FILE

    def ensure_roots(self) -> None:
        """Create the three data roots and the page-cache dir if missing (R1: no external setup)."""
        for p in (self.database_root, self.cache_root, self.manga_root, self.page_cache_dir):
            p.mkdir(parents=True, exist_ok=True)


def _resolve(base: Path, value: str) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def config_path(base_dir: Path | None = None) -> Path:
    return (base_dir or executable_dir()) / CONFIG_FILENAME


def load_config(base_dir: Path | None = None) -> Config:
    """Load ``config.toml`` (or return defaults if absent). Roots are resolved to absolute paths."""
    base = (base_dir or executable_dir()).resolve()
    path = base / CONFIG_FILENAME
    raw: dict[str, Any] = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    paths = raw.get("paths", {})
    rel = {
        "database": paths.get("database", "database"),
        "cache": paths.get("cache", "cache"),
        "manga": paths.get("manga", "manga"),
    }

    def section(name: str) -> dict[str, Any]:
        return raw.get(name, {}) or {}

    s = section("server")
    a = section("acquisition")
    r = section("reader")
    t = section("thumbnails")
    au = section("auth")

    return Config(
        base_dir=base,
        database_root=_resolve(base, rel["database"]),
        cache_root=_resolve(base, rel["cache"]),
        manga_root=_resolve(base, rel["manga"]),
        server=ServerConfig(
            host=s.get("host", "127.0.0.1"),
            port=int(s.get("port", 8000)),
            cors_origins=list(
                s.get("cors_origins", ["http://localhost:5173", "http://127.0.0.1:5173"])
            ),
        ),
        acquisition=AcquisitionConfig(
            proxy=a.get("proxy", ""),
            proxy_scope=a.get("proxy_scope", "all"),
            default_dltype=a.get("default_dltype", "org"),
            gp_short_behavior=a.get("gp_short_behavior", "block"),
            rate_limit_interval_seconds=float(a.get("rate_limit_interval_seconds", 5.0)),
            rate_limit_concurrency=int(a.get("rate_limit_concurrency", 1)),
            tag_refresh_hours=int(a.get("tag_refresh_hours", 24)),
            user_agent=a.get("user_agent", DEFAULT_USER_AGENT),
        ),
        reader=ReaderConfig(
            default_mode=r.get("default_mode", "scroll"),
            default_direction=r.get("default_direction", "rtl"),
            default_fit=r.get("default_fit", "width"),
            default_preload=int(r.get("default_preload", 2)),
            theme=r.get("theme", "dark"),
            language=r.get("language", "en"),
        ),
        thumbnails=ThumbnailConfig(
            prewarm=t.get("prewarm", "lazy"),
            cover_size=int(t.get("cover_size", 512)),
            page_size=int(t.get("page_size", 320)),
            quality=int(t.get("quality", 80)),
            max_cache_mb=int(t.get("max_cache_mb", 0)),
        ),
        auth=AuthConfig(auto_lock_minutes=int(au.get("auto_lock_minutes", 15))),
        _rel_paths=rel,
    )


def to_toml_dict(config: Config) -> dict[str, Any]:
    """Serialise a :class:`Config` back to the ``config.toml`` structure (relative paths preserved)."""
    rel = config._rel_paths or {"database": "database", "cache": "cache", "manga": "manga"}
    return {
        "paths": dict(rel),
        "server": {
            "host": config.server.host,
            "port": config.server.port,
            "cors_origins": list(config.server.cors_origins),
        },
        "acquisition": {
            "proxy": config.acquisition.proxy,
            "proxy_scope": config.acquisition.proxy_scope,
            "default_dltype": config.acquisition.default_dltype,
            "gp_short_behavior": config.acquisition.gp_short_behavior,
            "rate_limit_interval_seconds": config.acquisition.rate_limit_interval_seconds,
            "rate_limit_concurrency": config.acquisition.rate_limit_concurrency,
            "tag_refresh_hours": config.acquisition.tag_refresh_hours,
            "user_agent": config.acquisition.user_agent,
        },
        "reader": {
            "default_mode": config.reader.default_mode,
            "default_direction": config.reader.default_direction,
            "default_fit": config.reader.default_fit,
            "default_preload": config.reader.default_preload,
            "theme": config.reader.theme,
            "language": config.reader.language,
        },
        "thumbnails": {
            "prewarm": config.thumbnails.prewarm,
            "cover_size": config.thumbnails.cover_size,
            "page_size": config.thumbnails.page_size,
            "quality": config.thumbnails.quality,
            "max_cache_mb": config.thumbnails.max_cache_mb,
        },
        "auth": {"auto_lock_minutes": config.auth.auto_lock_minutes},
    }


def save_config(config: Config) -> Path:
    """Write the current effective config back to ``config.toml`` (preserving relative paths)."""
    path = config.base_dir / CONFIG_FILENAME
    config.base_dir.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(tomli_w.dumps(to_toml_dict(config)).encode("utf-8"))
    return path


def write_default_config(base_dir: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a fresh ``config.toml`` with default relative roots. Returns its path."""
    base = (base_dir or executable_dir()).resolve()
    path = base / CONFIG_FILENAME
    if path.exists() and not overwrite:
        return path
    default: dict[str, Any] = {
        "paths": {"database": "database", "cache": "cache", "manga": "manga"},
        "server": {
            "host": "127.0.0.1",
            "port": 8000,
            "cors_origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
        },
        "acquisition": {
            "proxy": "",
            "proxy_scope": "all",
            "default_dltype": "org",
            "gp_short_behavior": "block",
            "rate_limit_interval_seconds": 5.0,
            "rate_limit_concurrency": 1,
            "tag_refresh_hours": 24,
            "user_agent": DEFAULT_USER_AGENT,
        },
        "reader": {
            "default_mode": "scroll",
            "default_direction": "rtl",
            "default_fit": "width",
            "default_preload": 2,
            "theme": "dark",
            "language": "en",
        },
        "thumbnails": {
            "prewarm": "lazy",
            "cover_size": 512,
            "page_size": 320,
            "quality": 80,
            "max_cache_mb": 0,
        },
        "auth": {"auto_lock_minutes": 15},
    }
    base.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(tomli_w.dumps(default).encode("utf-8"))
    return path
