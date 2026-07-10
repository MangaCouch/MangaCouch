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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
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
    _maint_stop: threading.Event = field(default_factory=threading.Event)
    _prewarm_running: threading.Lock = field(default_factory=threading.Lock)

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
                        log.warning(
                            "could not decrypt plugin secret %s.%s — was secrets.key replaced? "
                            "Re-enter the value in Settings → Plugins.",
                            namespace, row.key,
                        )
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
        self._maint_stop.clear()
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
        elif self.config.thumbnails.prewarm == "full":
            threading.Thread(
                target=self.prewarm_thumbnails, name="mc-prewarm", daemon=True
            ).start()
        if self.config.acquisition.tag_refresh_hours > 0:
            threading.Thread(
                target=self._tag_refresh_loop, name="mc-tag-refresh", daemon=True
            ).start()

    def _bootstrap_ehentai_cookies(self) -> None:
        """Import e(x)hentai login cookies from config.toml into the encrypted plugin store (§5.6).

        First-run seeding only: a key that already has a stored value is never overwritten —
        cookies rotate, and re-importing stale config.toml values on every boot would silently
        clobber fresher ones the user set in Settings → Plugins.
        """
        cookies = {k: v for k, v in self.config.acquisition.ehentai.items() if v}
        if not cookies:
            return
        try:
            stored = self.plugin_config("ehentai_login")
            fresh = {k: v for k, v in cookies.items() if not stored.get(k)}
            if not fresh:
                return
            self.set_plugin_config(
                "ehentai_login", fresh, secret_keys={"ipb_pass_hash", "igneous"}
            )
            log.info("imported %d e-hentai cookie(s) from config into the encrypted store",
                     len(fresh))
        except Exception:
            log.exception("failed to import e-hentai cookies from config")

    def _initial_scan(self) -> None:
        try:
            stats = self.ingestor.scan()
            log.info("initial scan: %s", stats)
            self._rebuild_search_if_empty()
        except Exception:
            log.exception("initial scan failed")
        if self.config.thumbnails.prewarm == "full":
            self.prewarm_thumbnails()

    # -- background maintenance ------------------------------------------------------------------

    def prewarm_thumbnails(self) -> dict[str, int]:
        """``thumbnails.prewarm = full``: pre-generate every page-grid thumbnail as a background
        sweep so the first visit to any detail page is instant. Idempotent (skips existing thumbs)
        and cheap to re-run. Note: with ``max_cache_mb`` set, the LRU cap still wins."""
        from sqlalchemy import select

        from .core.archives import open_archive
        from .core.thumbnails import VARIANT_PAGE, generate_page_thumb
        from .db.models import Archive

        if not self._prewarm_running.acquire(blocking=False):
            log.info("thumbnail prewarm already running — skipping duplicate request")
            return {"generated": 0, "skipped": 0, "failed": 0, "already_running": 1}
        try:
            cfg = self.config.thumbnails
            stats = {"generated": 0, "skipped": 0, "failed": 0}
            with session_scope() as session:
                rows = session.execute(select(Archive.id, Archive.rel_path, Archive.page_count)).all()
            log.info("thumbnail prewarm: sweeping %d archives", len(rows))
            for archive_id, rel_path, page_count in rows:
                if self._maint_stop.is_set():
                    log.info("thumbnail prewarm interrupted by shutdown")
                    break
                missing = [
                    page for page in range(page_count)
                    if not self.thumbs.has(archive_id, page, VARIANT_PAGE)
                ]
                if not missing:
                    stats["skipped"] += 1
                    continue
                path = self.config.manga_root / rel_path
                if not path.exists():
                    continue
                try:
                    with open_archive(path) as reader:
                        for page in missing:
                            if self._maint_stop.is_set():
                                break
                            generate_page_thumb(
                                self.thumbs, archive_id, reader, page,
                                size=cfg.page_size, quality=cfg.quality,
                            )
                            stats["generated"] += 1
                except Exception:
                    log.exception("prewarm failed for %s", rel_path)
                    stats["failed"] += 1
            log.info("thumbnail prewarm done: %s", stats)
            return stats
        finally:
            self._prewarm_running.release()

    def _tag_refresh_loop(self) -> None:
        """Keep the EhTagTranslation database fresh (``acquisition.tag_refresh_hours``)."""
        check_interval = 1800.0  # re-check staleness every 30 min; refresh only when due
        # Let the startup scan settle first — the ingest holds write transactions, and the
        # 40k-row tag replace is itself one big write.
        if self._maint_stop.wait(30.0):
            return
        while not self._maint_stop.is_set():
            try:
                self._refresh_tagdb_if_stale()
            except Exception:
                log.exception("tag refresh failed (will retry)")
            self._maint_stop.wait(check_interval)

    def _refresh_tagdb_if_stale(self) -> None:
        import asyncio

        import httpx

        from .tags.translation import fetch_tagdb, ingest_tagdb

        hours = self.config.acquisition.tag_refresh_hours
        if hours <= 0:
            return
        last_raw = self.get_setting("tagdb_refreshed_at")
        if last_raw:
            try:
                last = datetime.fromisoformat(str(last_raw))
                if datetime.now(UTC) - last < timedelta(hours=hours):
                    return
            except ValueError:
                pass  # unparseable timestamp → treat as stale

        async def _go() -> dict:
            async with httpx.AsyncClient(
                proxy=self.config.acquisition.proxy or None, follow_redirects=True
            ) as client:
                return await fetch_tagdb(client)

        log.info("refreshing EhTagTranslation database (older than %dh)", hours)
        data = asyncio.run(_go())
        with session_scope() as session:
            count = ingest_tagdb(session, data)
        self.set_setting("tagdb_refreshed_at", datetime.now(UTC).isoformat())
        self.translator.load_safe()  # reload the in-memory map
        log.info("tag database refreshed: %d entries", count)
        # Translated names are part of the FTS text — refresh them so localized search stays true.
        self.rebuild_search()

    def _rebuild_search_if_empty(self) -> None:
        """Rebuild the FTS index only when it's actually empty (cache/ was deleted) — a full
        rebuild on every boot is slow and leaves a search-returns-nothing window."""
        if self.search.count() > 0:
            return
        self.rebuild_search()

    def rebuild_search(self) -> None:
        """Full FTS rebuild from the authoritative DB (includes translated tag names)."""
        from sqlalchemy import select

        from .db.models import Archive, ArchiveTag, Tag

        with session_scope() as session:
            rows = session.execute(select(Archive.id, Archive.title)).all()
            # One pass over the link table instead of a query per archive.
            tag_map: dict[str, list[str]] = {}
            pairs = session.execute(
                select(ArchiveTag.archive_id, Tag.namespace, Tag.value).join(
                    Tag, Tag.id == ArchiveTag.tag_id
                )
            ).all()
            for archive_id, ns, value in pairs:
                tag_map.setdefault(archive_id, []).append(f"{ns}:{value}" if ns else value)

            self.search.rebuild((aid, title, tag_map.get(aid, [])) for aid, title in rows)

    def shutdown(self) -> None:
        self._maint_stop.set()
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
    _ensure_schema_upgrades()

    secret_box = SecretBox.from_keyfile(config.secrets_keyfile_path)
    thumbs = ThumbStore(
        config.thumbs_db_path, max_bytes=config.thumbnails.max_cache_mb * 1024 * 1024
    )
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
    # Index translated tag names too, so searching in the EhTagTranslation language works.
    search.translate = translator.translate
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


def _ensure_schema_upgrades() -> None:
    """Tiny in-place column additions for pre-existing DBs (``create_all`` never ALTERs).

    Mirrors the alembic migrations so the click-to-run flow keeps working for users who never
    run ``alembic upgrade head``. Keep in sync with ``alembic/versions/``.
    """
    from sqlalchemy import inspect, text

    engine = bind_engine()
    try:
        cols = {c["name"] for c in inspect(engine).get_columns("download_job")}
        if "attempts" not in cols:  # 0002_dl_attempts
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE download_job "
                        "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                    )
                )
            log.info("schema upgrade: added download_job.attempts")
    except Exception:
        log.exception("schema upgrade check failed")


def _cpu_fanout() -> int:
    import os

    return max(1, (os.cpu_count() or 2) - 1)
