"""The application context — wires the in-process subsystems together (§3).

One :class:`AppContext` owns the config, the secret box, the thumbnail/search stores, the page
cache, the plugin registry, the rate limiter, the ingestor, the folder watcher, and the download
worker. It is built once at startup and attached to ``app.state``.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from typing import Any

import diskcache

from .acquisition.queue import DownloadWorker
from .acquisition.ratelimit import RateLimiter
from .auth.crypto import SecretBox
from .config import Config
from .core.thumbnails import ThumbStore
from .db.base import Base, init_engine, session_scope
from .db.models import AppConfig, PluginConfig
from .ingest.pipeline import Ingestor
from .ingest.watcher import LibraryWatcher
from .plugins.registry import PluginRegistry
from .search.index import SearchIndex
from .tags.translation import TagTranslator

log = logging.getLogger("mangacouch")


@dataclass
class AppContext:
    config: Config
    secret_box: SecretBox
    thumbs: ThumbStore
    search: SearchIndex
    page_cache: diskcache.Cache
    registry: PluginRegistry
    rate_limiter: RateLimiter
    ingestor: Ingestor
    translator: TagTranslator
    watcher: LibraryWatcher
    download_worker: DownloadWorker
    executor: ProcessPoolExecutor | None = None
    _started: bool = False

    # -- plugin config (decrypted) ------------------------------------------------------------

    def plugin_config(self, namespace: str) -> dict[str, str]:
        out: dict[str, str] = {}
        with session_scope() as session:
            rows = session.query(PluginConfig).filter(PluginConfig.namespace == namespace).all()
            for row in rows:
                if row.is_secret and row.value:
                    try:
                        out[row.key] = self.secret_box.decrypt(row.value)
                    except ValueError:
                        out[row.key] = ""
                else:
                    out[row.key] = row.value
        return out

    def set_plugin_config(self, namespace: str, values: dict[str, Any], secret_keys: set[str]) -> None:
        with session_scope() as session:
            for key, value in values.items():
                text = "" if value is None else str(value)
                is_secret = key in secret_keys
                stored = self.secret_box.encrypt(text) if (is_secret and text) else text
                row = (
                    session.query(PluginConfig)
                    .filter(PluginConfig.namespace == namespace, PluginConfig.key == key)
                    .one_or_none()
                )
                if row is None:
                    session.add(
                        PluginConfig(
                            namespace=namespace, key=key, value=stored, is_secret=is_secret
                        )
                    )
                else:
                    row.value = stored
                    row.is_secret = is_secret
        # Cookies changed → drop cached login sessions.
        self.download_worker.invalidate_sessions()

    # -- typed app_config ---------------------------------------------------------------------

    def get_setting(self, key: str, default: Any = None) -> Any:
        with session_scope() as session:
            row = session.get(AppConfig, key)
            if row is None:
                return default
            try:
                return json.loads(row.value)
            except json.JSONDecodeError:
                return row.value

    def set_setting(self, key: str, value: Any) -> None:
        with session_scope() as session:
            row = session.get(AppConfig, key)
            payload = json.dumps(value)
            if row is None:
                session.add(AppConfig(key=key, value=payload))
            else:
                row.value = payload

    # -- lifecycle ----------------------------------------------------------------------------

    def startup(self, *, scan_on_start: bool = True, watch: bool = True) -> None:
        if self._started:
            return
        self._started = True
        self.translator.load_safe()
        self.registry.discover(self.config.base_dir / "plugins")
        self._bootstrap_ehentai_cookies()
        self.download_worker.start()
        if watch:
            self.watcher.start()
        if scan_on_start:
            threading.Thread(
                target=self._initial_scan, name="mc-initial-scan", daemon=True
            ).start()

    def _bootstrap_ehentai_cookies(self) -> None:
        """Import e(x)hentai login cookies from config.toml into the encrypted plugin store (§5.6).

        Only non-empty values are imported; the canonical, encrypted copy then lives in
        ``plugin_config`` so the plaintext cookies can be removed from ``config.toml``.
        """
        cookies = {k: v for k, v in self.config.acquisition.ehentai.items() if v}
        if not cookies:
            return
        try:
            self.set_plugin_config(
                "ehentai_login", cookies, secret_keys={"ipb_pass_hash", "igneous"}
            )
            log.info("imported %d e-hentai cookie(s) from config into the encrypted store",
                     len(cookies))
        except Exception:
            log.exception("failed to import e-hentai cookies from config")

    def _initial_scan(self) -> None:
        try:
            stats = self.ingestor.scan()
            log.info("initial scan: %s", stats)
            self._rebuild_search_if_empty()
        except Exception:
            log.exception("initial scan failed")

    def _rebuild_search_if_empty(self) -> None:
        from sqlalchemy import select

        from .db.models import Archive, ArchiveTag, Tag

        with session_scope() as session:
            rows = session.execute(select(Archive.id, Archive.title)).all()

            def tags_for(archive_id: str) -> list[str]:
                pairs = session.execute(
                    select(Tag.namespace, Tag.value)
                    .join(ArchiveTag, ArchiveTag.tag_id == Tag.id)
                    .where(ArchiveTag.archive_id == archive_id)
                ).all()
                return [f"{ns}:{v}" if ns else v for ns, v in pairs]

            self.search.rebuild((aid, title, tags_for(aid)) for aid, title in rows)

    def shutdown(self) -> None:
        self.watcher.stop()
        self.download_worker.stop()
        self.thumbs.close()
        self.search.close()
        self.page_cache.close()
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        self._started = False


def build_context(config: Config, *, use_process_pool: bool = True) -> AppContext:
    """Construct (but do not start) the application context for ``config``."""
    config.ensure_roots()
    init_engine(config.library_db_path)
    Base.metadata.create_all(bind_engine())

    secret_box = SecretBox.from_keyfile(config.secrets_keyfile_path)
    thumbs = ThumbStore(config.thumbs_db_path)
    search = SearchIndex(config.search_db_path)
    page_cache = diskcache.Cache(str(config.page_cache_dir), size_limit=2 << 30)

    executor: ProcessPoolExecutor | None = None
    if use_process_pool:
        try:
            executor = ProcessPoolExecutor(max_workers=max(1, _cpu_fanout()))
        except Exception:  # noqa: BLE001 — fall back to in-thread compute
            executor = None

    ingestor = Ingestor(
        config.manga_root,
        thumbs,
        search,
        cover_size=config.thumbnails.cover_size,
        quality=config.thumbnails.quality,
        executor=executor,
    )
    registry = PluginRegistry()
    rate_limiter = RateLimiter(
        min_interval=config.acquisition.rate_limit_interval_seconds,
        concurrency=config.acquisition.rate_limit_concurrency,
    )
    translator = TagTranslator()
    watcher = LibraryWatcher(config.manga_root, ingestor)

    ctx = AppContext(
        config=config,
        secret_box=secret_box,
        thumbs=thumbs,
        search=search,
        page_cache=page_cache,
        registry=registry,
        rate_limiter=rate_limiter,
        ingestor=ingestor,
        translator=translator,
        watcher=watcher,
        download_worker=None,  # type: ignore[arg-type]
        executor=executor,
    )
    ctx.download_worker = DownloadWorker(
        registry=registry,
        rate_limiter=rate_limiter,
        ingestor=ingestor,
        manga_root=config.manga_root,
        staging_dir=config.cache_root / "downloads",
        plugin_config=ctx.plugin_config,
        user_agent=config.acquisition.user_agent,
        proxy=config.acquisition.proxy or None,
        proxy_scope=config.acquisition.proxy_scope,
        gp_short_behavior=config.acquisition.gp_short_behavior,
    )
    return ctx


def bind_engine():
    from .db.base import get_engine

    return get_engine()


def _cpu_fanout() -> int:
    import os

    return max(1, (os.cpu_count() or 2) - 1)
