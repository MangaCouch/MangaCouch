"""The SQLite-persisted download queue + worker (§5.3).

The ``download_job`` table *is* the queue (it survives restarts). A single ``threading`` worker
drains it by priority; an interval poll honours each job's ``next_run`` for scheduled re-checks
(no cron/Celery). Rate limiting is enforced around the plugin's network calls (§5.3).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import sidecars
from ..db.base import session_scope
from ..db.models import DownloadJob
from ..ingest.pipeline import Ingestor
from ..plugins.base import (
    DownloadContext,
    LoginContext,
    MetadataContext,
    PluginError,
)
from ..plugins.registry import PluginRegistry
from .ehentai import EHentaiError, InsufficientFundsError, NotLoggedInError, parse_gallery_url
from .ratelimit import RateLimiter

log = logging.getLogger("mangacouch.queue")


class DownloadWorker:
    def __init__(
        self,
        *,
        registry: PluginRegistry,
        rate_limiter: RateLimiter,
        ingestor: Ingestor,
        manga_root: Path,
        staging_dir: Path,
        plugin_config: Callable[[str], dict],
        user_agent: str,
        proxy: str | None,
        proxy_scope: str,
        gp_short_behavior: str,
        poll_interval: float = 2.0,
    ) -> None:
        self.registry = registry
        self.rate_limiter = rate_limiter
        self.ingestor = ingestor
        self.manga_root = manga_root
        self.staging_dir = staging_dir
        self.plugin_config = plugin_config
        self.user_agent = user_agent
        self.proxy = proxy
        self.proxy_scope = proxy_scope
        self.gp_short_behavior = gp_short_behavior
        self.poll_interval = poll_interval

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._sessions: dict[str, httpx.Client] = {}

    # -- lifecycle ----------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        self._requeue_orphans()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mc-downloader", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        for client in self._sessions.values():
            client.close()
        self._sessions.clear()

    def notify(self) -> None:
        """Wake the worker immediately (e.g. a new job was just enqueued)."""
        self._wake.set()

    # -- queue mechanics ----------------------------------------------------------------------

    def _requeue_orphans(self) -> None:
        """Jobs left 'running'/'preparing' by a crash are reset to 'queued' on startup."""
        with session_scope() as session:
            for job in session.scalars(
                select(DownloadJob).where(DownloadJob.state.in_(("running", "preparing")))
            ):
                job.state = "queued"

    def _run(self) -> None:
        while not self._stop.is_set():
            job_id = self._claim_next()
            if job_id is None:
                self._wake.wait(self.poll_interval)
                self._wake.clear()
                continue
            try:
                self._process(job_id)
            except Exception:
                log.exception("download job %s crashed", job_id)
                self._fail(job_id, "internal error (see logs)")

    def _claim_next(self) -> int | None:
        now = datetime.now(UTC)
        with session_scope() as session:
            job = session.scalars(
                select(DownloadJob)
                .where(DownloadJob.state == "queued")
                .where((DownloadJob.next_run.is_(None)) | (DownloadJob.next_run <= now))
                .order_by(DownloadJob.priority.desc(), DownloadJob.created_at.asc())
                .limit(1)
            ).first()
            if job is None:
                return None
            job.state = "running"
            job.error = None
            return job.id

    # -- job execution ------------------------------------------------------------------------

    def _process(self, job_id: int) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job is None:
                return
            url = job.url
            dltype = job.dltype
            catid = job.catid

        plugin = self.registry.find_download_plugin(url)
        if plugin is None:
            self._fail(job_id, "No download plugin handles this URL.")
            return

        info = plugin.plugin_info()
        try:
            session_http = self._login_session(info.login_from)
        except PluginError as exc:
            self._fail(job_id, f"login failed: {exc}")
            return

        source = self._source_key(url)
        self._set_state(job_id, "preparing")

        def on_progress(frac: float) -> None:
            self._set_progress(job_id, frac)

        try:
            with self.rate_limiter.slot(source):
                ctx = DownloadContext(
                    url=url,
                    config=self.plugin_config(info.namespace),
                    session=session_http,
                    dest_dir=self.staging_dir,
                    dltype=dltype,
                    on_progress=on_progress,
                )
                result = plugin.download(ctx)
        except InsufficientFundsError as exc:
            if self.gp_short_behavior == "resample" and dltype == "org":
                log.info("GP short on job %s — falling back to Resample", job_id)
                self._set_dltype(job_id, "res")
                self._process(job_id)
                return
            self._fail(job_id, f"out of GP: {exc}")
            return
        except NotLoggedInError as exc:
            self._fail(job_id, str(exc))
            return
        except (EHentaiError, PluginError) as exc:
            self._retry_or_fail(job_id, str(exc))
            return
        except httpx.HTTPError as exc:
            self._retry_or_fail(job_id, f"network error: {exc}")
            return

        if result.error or not result.archive_path:
            self._fail(job_id, result.error or "download produced no archive")
            return

        self._record_costs(job_id, result.gp_cost, result.gp_balance)
        archive_id = self._finalise(url, catid, result, session_http, info.login_from)
        self._complete(job_id, archive_id)

    def _finalise(self, url, catid, result, session_http, login_from) -> str | None:
        """Move the staged ZIP into manga/, enrich metadata, write sidecars, ingest."""
        staged: Path = result.archive_path  # type: ignore[assignment]
        final = self.manga_root / (result.suggested_filename or staged.name)
        final.parent.mkdir(parents=True, exist_ok=True)
        if final.exists():
            final = self._dedupe_name(final)
        staged.replace(final)

        meta = result.gallery_meta or {}
        source_url = meta.get("source_url") or url
        gallery = sidecars.GalleryMetadata(
            gid=meta.get("gid"),
            token=meta.get("token"),
            site=meta.get("domain"),
            source_url=source_url,
        )
        # Enrich via the Metadata plugin (tags/title) where available.
        self._enrich(gallery, source_url, session_http, login_from, final)

        sidecars.write_eze(final, gallery)
        sidecars.write_mc(
            final,
            sidecars.McSidecar(
                archive_id="",  # filled by the ingestor after hashing
                fingerprint=None,
                format="zip",
                page_count=0,
                original_filename=final.name,
                title=gallery.title,
                title_jpn=gallery.title_jpn,
                rating=gallery.rating,
                category=gallery.category,
                source_url=source_url,
                source_gid=gallery.gid,
                source_token=gallery.token,
                uploader=gallery.uploader,
                posted=gallery.posted,
                tags=gallery.tags,
                ingest={"via": "download"},
            ),
        )
        return self.ingestor.index_file(final)

    def _enrich(self, gallery, source_url, session_http, login_from, path) -> None:
        from ..plugins.base import MetadataPlugin, PluginType

        for plugin in self.registry.of_type(PluginType.METADATA):
            if not isinstance(plugin, MetadataPlugin):
                continue
            try:
                result = plugin.get_tags(
                    MetadataContext(
                        archive_id="",
                        title=gallery.title,
                        source_url=source_url,
                        config=self.plugin_config(plugin.plugin_info().namespace),
                        session=session_http,
                        file_path=path,
                    )
                )
            except Exception:
                log.exception("metadata plugin failed")
                continue
            if result.error:
                continue
            if result.title:
                gallery.title = result.title
            if result.tags:
                gallery.tags = result.tags
            break

    # -- session cache ------------------------------------------------------------------------

    def _login_session(self, login_namespace: str | None) -> httpx.Client:
        if not login_namespace:
            raise PluginError("download plugin declares no login_from")
        cached = self._sessions.get(login_namespace)
        if cached is not None:
            return cached
        login = self.registry.login_plugin(login_namespace)
        if login is None:
            raise PluginError(f"login plugin {login_namespace!r} not found")
        ctx = LoginContext(
            config=self.plugin_config(login_namespace),
            user_agent=self.user_agent,
            proxy=self.proxy,
        )
        client = login.do_login(ctx)
        self._sessions[login_namespace] = client
        return client

    def login_session(self, namespace: str | None) -> httpx.Client:
        """Public accessor for the cached, authenticated session (used by the GP calculator)."""
        return self._login_session(namespace)

    def invalidate_sessions(self) -> None:
        for client in self._sessions.values():
            client.close()
        self._sessions.clear()

    # -- helpers ------------------------------------------------------------------------------

    @staticmethod
    def _source_key(url: str) -> str:
        try:
            return parse_gallery_url(url).domain
        except EHentaiError:
            return "default"

    @staticmethod
    def _dedupe_name(path: Path) -> Path:
        stem, suffix, i = path.stem, path.suffix, 1
        while True:
            candidate = path.with_name(f"{stem} ({i}){suffix}")
            if not candidate.exists():
                return candidate
            i += 1

    def _set_state(self, job_id: int, state: str) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.state = state

    def _set_dltype(self, job_id: int, dltype: str) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.dltype = dltype
                job.state = "running"

    def _set_progress(self, job_id: int, frac: float) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.progress = max(0.0, min(1.0, frac))

    def _record_costs(self, job_id: int, cost: int | None, balance: int | None) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.gp_cost = cost
                job.gp_balance = balance

    def _complete(self, job_id: int, archive_id: str | None) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.state = "done"
                job.progress = 1.0
                job.archive_id = archive_id
                job.error = None

    def _fail(self, job_id: int, message: str) -> None:
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job:
                job.state = "failed"
                job.error = message[:500]

    def _retry_or_fail(self, job_id: int, message: str, max_attempts: int = 3) -> None:
        """Reschedule transient failures with a backoff; give up after a few attempts."""
        with session_scope() as session:
            job = session.get(DownloadJob, job_id)
            if job is None:
                return
            attempts = (job.priority and 0) or 0  # placeholder; track via error count below
            # Count retries cheaply by stashing them in the error prefix.
            prev = 0
            if job.error and job.error.startswith("[retry "):
                try:
                    prev = int(job.error[7 : job.error.index("]")])
                except ValueError:
                    prev = 0
            if prev + 1 >= max_attempts:
                job.state = "failed"
                job.error = message[:500]
            else:
                job.state = "queued"
                job.next_run = datetime.now(UTC) + timedelta(seconds=30 * (prev + 1))
                job.error = f"[retry {prev + 1}] {message}"[:500]
        _ = attempts

    # -- enqueue API --------------------------------------------------------------------------

    def enqueue(
        self,
        session: Session,
        url: str,
        *,
        dltype: str = "org",
        catid: int | None = None,
        priority: int = 0,
    ) -> DownloadJob:
        gid = token = domain = None
        try:
            ref = parse_gallery_url(url)
            gid, token, domain = ref.gid, ref.token, ref.domain
        except EHentaiError:
            pass
        job = DownloadJob(
            url=url,
            gid=gid,
            token=token,
            domain=domain,
            dltype=dltype,
            catid=catid,
            priority=priority,
            state="queued",
        )
        session.add(job)
        session.flush()
        return job
