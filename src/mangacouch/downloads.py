"""Download queue: gallery URL -> inspect -> approve -> submit -> transfer -> import.

States (download_job.state) and who moves them:

    queued            waiting for a worker; retry_at holds it back after a transient failure
    inspecting ...    a worker owns the job and runs its phase (inspecting, submitting,
                      downloading, importing; phases inspect, submit, recover, transfer, import)
    needs_approval    price known; the user approves the archive type (always when the library
                      already has the gallery or an edition of it, even if free downloads start
                      by themselves), and again when the price rose above the approved one
    paused            stopped by the user; resume continues it
    error             an exhausted retry budget or a failure needing attention; resume continues
                      recovery or inspects again
    verification_required  credentials or a browser challenge need the user
    pausing/cancelling     the user asked while a worker owns the job; it stops at its next check
    done/cancelled    terminal; adding a done gallery again returns the job as it is, and only
                      the explicit redownload action queues it again

One job exists per gallery: E-Hentai and ExHentai links of one gallery id share it. A worker
takes a queued job only inside the claim transaction and only if no other worker of this server
still owns it (`step`).

Network/5xx and unknown paid-response failures back off by min(1800, 15 * 2**attempts)
seconds, stopping after five failures. Archive preparation, database contention and library-root
unavailability wait at least 30 seconds without counting an attempt. Rate limits wait for the site's
cooldown without counting an attempt. A job short of GP parks (retry_at = GP_PARKED) until the shared
balance probe sees enough GP.

An approved paid EH archive request whose outcome is unknown enters phase recover automatically
(with bounded retries): recovery reads the archiver page, uses a prepared link it offers, or
requests again only at no more than the approved price (EH unlocks a bought archive, so a second
request of a charged one is free); a higher price returns the job to needs_approval.

Resuming continues an approved job (phase submit, transfer or import) where it stopped and
inspects any other job again, so a link the inspection found is never fetched unapproved. A
restart continues queued and worker-owned jobs in their phase, sends an archive request that may
have been sent to recovery, and completes pauses and cancellations that were under way
(`_resume_interrupted`).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from copy import copy
from dataclasses import replace
from urllib.parse import urlsplit

import httpx

from .archives import ImportCancelled, InvalidArchive, zip_image_bytes
from .cursors import decode_cursor, encode_cursor
from .database import background_thread, busy
from .editions import Editions, display_title, listing_newer, with_source
from .errors import InvalidInput, NotFound, RevisionConflict
from .files import CHUNK_SIZE, RootUnavailable, hash_file, set_thread_cancel
from .ingest import FileLibrary, ImportResult
from .logs import short_id
from .models import EH_SITES, Metadata, NativeSidecar, Source, canonical_json, site_group
from .online import OnlineMetadata
from .sidecars import SidecarSync
from .source_adapters import GDATA_BATCH, REUSABLE_ARCHIVE_TTL, Sources, archive_remote
from .source_checks import SourceChecks
from .source_config import SourceKeyLost, SourceSettings, SourceVault, try_lock
from .source_http import NotSent, SourceError, SourceHTTP, gallery_url, refusal
from .source_limits import SAVE_SECONDS

LOGGER = logging.getLogger(__name__)
ACTIVE = {"inspecting", "submitting", "downloading", "importing"}
WAITING_WITHIN = 60
"""Seconds: a queued job whose retry is due within this counts as waiting in the summary."""
TERMINAL = {"done", "cancelled"}
CLEARABLE = TERMINAL | {"error"}
"""Finished or failed records; an unresolved paid request must be cancelled before clearing."""
WORKERS = 4
"""Workers that transfer and import archives."""
META_WORKERS = 2
"""Separate workers for inspection, archive requests and recovery."""
META_PHASES = ("inspect", "submit", "recover")
"""Phases made of a few page and API requests: inspection, the archive request, recovery."""
META_PHASES_SQL = "(" + ",".join(f"'{phase}'" for phase in META_PHASES) + ")"
UNCOUNTED_WAITS = frozenset(
    {
        "archive_preparing",
        "database_busy",
        "root_unavailable",
        "insufficient_storage",
        "insufficient_gp",
        "source_rate_limited",
    }
)
"""Waits `_finish` queues a job for without counting an attempt (web/src/downloadState.ts)."""
GP_RETRY = 60
GP_PARKED = 2**62
SPACE_CHECK_BYTES = 64 * 1024**2
WORKER_BUSY_TIMEOUT = 30.0
"""Seconds the download workers wait for another connection's write lock. Nobody is waiting on a
response, so they outwait a busy disk; only past this does a job re-queue with database_busy."""
BUSY_RETRY = 15
"""Seconds before a job that a busy database stopped is tried again."""
PARK_TRIES = 5
"""Tries, BUSY_RETRY apart, to settle a job whose worker stopped while the database stayed busy."""
BATCH_LIMIT = 100
COMPLETED_WINDOW = 86400
"""Finished downloads count toward the task summary for one day."""
RETENTION_SECONDS = 90 * 86400
"""Finished and cancelled downloads are forgotten after 90 days."""
MAX_FAILURES = 5
STORAGE_RECHECK = 300
CREDENTIAL_ERRORS = {"verification_required", "source_login_required"}
LIBRARY_MATCHES = 20
ARCHIVE_VARIANTS = {"org": "original", "res": "resample", "reuse": "unknown"}
"""What a download's archive is; a prepared archive offered by the archiver page has no known quality."""
PREPARING = re.compile(r"\bprepar(?:ing|ed)\b|\bnot (?:yet )?ready\b")
"""Markers of an archive server page that asks to come back later."""
STARTING_OVER = (
    "preexisting",
    "may_be_charged",
    "import_job",
    "approved_gp",
    "approved_mode",
    "retry_approved",
    "quote",
    "archive_sha256",
    "submission_uncertain",
    "reusable",
    "etag",
    "duplicate",
    "previous_books",
    "library",
    "archive_variant",
    "import_error",
    "requote",
)
"""Details of an earlier run that a job starting over from inspection drops."""
RESET_COLUMNS = "remote_secret=NULL,received=0,total=NULL,book_id=NULL"
"""Columns a job starting over from inspection clears: its archive link, transfer and book."""


def retryable(error: SourceError) -> bool:
    """Errors that resolve themselves: the job is re-queued instead of waiting for the user."""
    return error.code in {
        "source_network",
        "archive_preparing",
        "source_rate_limited",
        "database_busy",
        "insufficient_gp",
        "root_unavailable",
    } or (error.code == "source_unavailable" and ((error.status or 0) >= 500 or error.retry_after > 0))


def local_error(error: Exception) -> str:
    """The error code of a local failure while downloading or importing."""
    if busy(error):
        return "database_busy"
    if isinstance(error, SourceKeyLost):
        return "source_key_lost"
    if isinstance(error, RootUnavailable):
        return "root_unavailable"
    if isinstance(error, OSError) and error.errno in {errno.ENOSPC, errno.EDQUOT}:
        return "insufficient_storage"
    return "download_local_error"


def response_chunks(response: httpx.Response):
    """Expose small network chunks immediately; split large chunks without buffering small ones."""
    for data in response.iter_bytes():
        for start in range(0, len(data), CHUNK_SIZE):
            yield data[start : start + CHUNK_SIZE]


class Signal:
    """Wakes every worker. Each worker waits on its own event, so one worker clearing its event
    never swallows a wake (such as the shutdown) meant for another."""

    def __init__(self):
        self.events: list[threading.Event] = []

    def event(self) -> threading.Event:
        event = threading.Event()
        self.events.append(event)
        return event

    def set(self) -> None:
        for event in self.events:
            event.set()


def startup(files: FileLibrary) -> None:
    """Library housekeeping before serving: finish interrupted imports, drop stale upload spools."""
    try:
        files.recover()
    except (OSError, sqlite3.Error, RevisionConflict):
        LOGGER.warning("Import recovery deferred until library storage is ready")
    from .snapshots import snapshot_directory

    directory = snapshot_directory(files.library)
    if directory.is_dir() and not directory.is_symlink():
        shutil.rmtree(directory)
    for stale in files.library.path.parent.glob(f"mangacouch-upload-{files.library.path.name}-*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)


def settle(details: dict) -> dict:
    """The details a settled (done or cancelled) job keeps: its observations lose their
    comments and raw gdata, the bulk of a job. comments_state 'not_requested' lets an import that
    resumes a cancelled job keep the comments already stored for its book."""
    if "source_metadata_not_saved" in details.get("warnings", []):
        return details
    for observation in [details.get("selected")]:
        if observation:
            observation.pop("gdata", None)
            if observation.get("comments"):
                observation.update(comments=[], comments_state="not_requested", comments_error=None)
    return details


def _stored_details(details: dict) -> str:
    saved = {**details}
    if "quote" in saved:
        saved["quote"] = {key: value for key, value in saved["quote"].items() if key != "balance_gp"}
    return canonical_json(saved)


def _uncertain(details: dict) -> bool:
    return bool(details.get("submission_uncertain")) or details.get("phase") == "uncertain"


class Downloads:
    def __init__(self, files: FileLibrary, *, http: SourceHTTP | None = None):
        self.files, self.library = files, files.library
        self.vault = SourceVault(self.library)
        key_lost = False
        try:
            self.settings, secrets, self.config_revision = self.vault.load()
        except SourceKeyLost:
            # Online sources stay off until the credentials are reset (settings page or CLI).
            LOGGER.warning("Source credentials cannot be read; online sources are off until they are reset")
            (self.settings, self.config_revision), secrets, key_lost = self.vault.settings(), {}, True
        self.http = http or SourceHTTP(self.settings, secrets)
        if key_lost:
            self.http.blocked = "source_key_lost"
        self.http.persist(self.library)
        self.sources = Sources(self.http)
        self.online = OnlineMetadata(files, self.sources)
        self.sync = SidecarSync(files)
        self.checks = SourceChecks(self.online, self.sync.publish)
        self.editions = Editions(self.library)
        self.directory = self.library.path.parent / (".mangacouch-downloads-" + self.library.path.name)
        self.stopping = threading.Event()
        self.wake = Signal()
        self.threads: list[threading.Thread] = []
        self.lock_file = None
        self.working: dict[str, str] = {}
        """Jobs currently owned by workers."""
        self.meta_working: set[str] = set()
        """Jobs owned by metadata workers; they take no transfer slot."""
        self.worker_lock = threading.Lock()
        self.storing_links: set[str] = set()
        self.stop_requests: dict[str, str] = {}
        self.gp_lock = threading.Lock()
        self.gp_next_at = 0.0
        self.gp_interval = GP_RETRY
        self.pending_parks: dict[str, bool] = {}
        self.progress_lock = threading.Lock()
        self.progress: dict[str, tuple[float, int, float]] = {}
        # Serializes a job's state changes with the archive cleanup that follows them.
        self.job_locks = [threading.Lock() for _ in range(32)]
        # (sha256, size, mtime_ns) of archives this process hashed while downloading.
        self.hashed: dict[str, tuple[str, int, int]] = {}
        # One batch metadata fetch at a time: the other meta workers then find theirs fetched.
        self.prefetch_lock = threading.Lock()
        # Jobs this process asked to pause or cancel.
        self.interrupted: set[str] = set()

    def _lock_index(self, identifier: str) -> int:
        return int(hashlib.sha256(identifier.encode()).hexdigest()[:8], 16) % len(self.job_locks)

    def _job_lock(self, identifier: str) -> threading.Lock:
        return self.job_locks[self._lock_index(identifier)]

    def save_settings(self, settings: SourceSettings, changes: dict[str, str], expected_revision: int):
        changes = {key: value.strip() for key, value in changes.items()}
        self.vault.validate(changes)
        with self.http.configuration_lock:
            current, secrets, revision = self.vault.load()
            if settings == current and all(secrets.get(key, "") == value for key, value in changes.items()):
                return self.vault.public()
            if revision != expected_revision:
                raise RevisionConflict("Source settings changed")
            proxy = secrets.get("proxy")
            secrets.update(changes)
            client = self.http.build_client(secrets) if secrets.get("proxy") != proxy else None
            try:
                revision = self.vault.save(settings, changes, expected_revision)
            except Exception:
                if client is not None:
                    client.close()
                raise
            self.http.apply(settings, secrets, client)
            self.settings, self.config_revision = settings, revision
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE download_job SET retry_at=0 WHERE state='queued' "
                    "AND error_code='insufficient_storage'"
                )
            self.wake.set()
            return self.vault.public()

    def verify(self, site: str, typed: dict[str, str], *, cancelled=None) -> dict:
        """Check a site's credentials as typed, never storing them; a field left empty uses the
        stored value."""
        typed = {key: value.strip() for key, value in typed.items() if value.strip()}
        self.vault.validate(typed)
        with self.http.trial({**self.http.secrets, **typed}):
            return self.sources.verify(site, cancelled=cancelled)

    def reset_credentials(self) -> dict:
        """Discard the stored credentials under a new key and pause online requests.

        For a lost or mismatched key; settings and books stay, downloads holding a remote link
        pause (SourceVault.discard_credentials)."""
        with self.http.configuration_lock:
            proxy = self.http.secrets.get("proxy")
            with self.worker_lock:
                working = tuple(self.working)
                for identifier in working:
                    self.stop_requests.setdefault(identifier, "pausing")
                self.interrupted.update(working)
            for identifier in working:
                self.http.interrupt(identifier)
            self.vault.discard_credentials(working)
            settings, secrets, revision = self.vault.load()
            self.http.apply(settings, secrets, self.http.build_client(secrets) if proxy else None)
            self.http.blocked = "source_restore_paused" if settings.online_paused else None
            self.settings, self.config_revision = settings, revision
        LOGGER.warning("Source credentials reset; online sources stay paused until settings are checked")
        self.wake.set()
        return self.vault.public()

    def _read(self, db, identifier: str) -> dict:
        row = db.execute("SELECT * FROM download_job WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("Download not found", code="download_not_found")
        source, details = json.loads(row["source_json"]), json.loads(row["details_json"])
        account = db.execute(
            "SELECT balance_gp FROM source_account WHERE site_group=?", (site_group(source["site"])[0],)
        ).fetchone()
        if details.get("quote") is not None:
            details["quote"]["balance_gp"] = account[0] if account else None
        return {
            **dict(row),
            "source": source,
            "details": details,
            "balance_gp": account[0] if account else None,
        }

    def get(self, identifier: str, *, private=False) -> dict:
        with self.library.connection() as db:
            job = self._read(db, identifier)
        return self._output(job, private=private)

    def _output(self, job: dict, *, private=False) -> dict:
        if private:
            return job
        with self.progress_lock:
            sample = self.progress.get(job["id"])
        moving = sample is not None and job["state"] == "downloading"
        job["bytes_per_second"] = sample[2] if sample and moving and time.monotonic() - sample[0] < 4 else 0
        if sample and moving:
            job["received"] = max(job["received"], sample[1])
        for name in ("source_json", "details_json", "remote_secret", "rowid", "phase", "gallery_key"):
            job.pop(name, None)
        details = job["details"]
        for observation in [details.get("selected")]:
            if observation:
                for name in ("cover_url", "gdata", "comments", "notes"):
                    observation.pop(name, None)
                observation["metadata"] = {
                    key: value
                    for key, value in observation["metadata"].items()
                    if key
                    in {
                        "title",
                        "original_title",
                        "language",
                        "declared_page_count",
                        "declared_byte_size",
                        "source",
                        "source_rating",
                        "uploader",
                    }
                }
        details.pop("etag", None)
        details.pop("archive_sha256", None)
        return job

    def list(
        self,
        after: str = "",
        limit: int = 20,
        scope: str = "all",
        *,
        page: int | None = None,
        through: int | None = None,
        focus: str = "",
    ) -> dict:
        """Newest first; `after` continues with the jobs older than the previous page."""
        conditions = {
            "all": "1",
            "active": "state NOT IN ('done','cancelled')",
            "history": "state IN ('done','cancelled')",
        }
        (before,) = decode_cursor(after, int) if after else (2**63 - 1,)
        with self.library.connection() as db:
            db.execute("BEGIN")
            target = (
                db.execute("SELECT rowid,state FROM download_job WHERE id=?", (focus,)).fetchone()
                if focus
                else None
            )
            if target:
                scope = "history" if target["state"] in TERMINAL else "active"
                through = None
            through = (
                through
                if through is not None
                else int(db.execute("SELECT coalesce(max(rowid),0) FROM download_job").fetchone()[0])
            )
            condition = f"({conditions[scope]}) AND rowid<={int(through)}"
            total = db.execute(f"SELECT count(*) FROM download_job WHERE {condition}").fetchone()[0]
            if target:
                position = db.execute(
                    f"SELECT count(*) FROM download_job WHERE {condition} AND rowid>?", (target["rowid"],)
                ).fetchone()[0]
                page = position // limit + 1
            offset = 0
            if page is not None:
                page = max(1, min(page, (total + limit - 1) // limit))
                offset = (page - 1) * limit
                before = 2**63 - 1
            rows = db.execute(
                "SELECT rowid,id,input_url,source_json,state,revision,received,total,book_id,error_code,"
                "retry_at,attempts,created_at,updated_at,json_remove(details_json,'$.selected.comments',"
                "'$.selected.gdata') AS details_json "
                f"FROM download_job WHERE {condition} AND rowid<? ORDER BY rowid DESC LIMIT ? OFFSET ?",
                (before, limit + 1, offset),
            ).fetchall()
            balances = dict(db.execute("SELECT site_group,balance_gp FROM source_account"))
        items = [
            self._output(
                {
                    **dict(row),
                    "source": json.loads(row["source_json"]),
                    "details": json.loads(row["details_json"]),
                }
            )
            for row in rows[:limit]
        ]
        for item in items:
            balance = balances.get(site_group(item["source"]["site"])[0])
            item["balance_gp"] = balance
            if item["details"].get("quote") is not None:
                item["details"]["quote"]["balance_gp"] = balance
        return {
            "items": items,
            "after": encode_cursor(rows[limit - 1]["rowid"]) if len(rows) > limit else None,
            "total": total,
            "page": page,
            "through": through,
            "scope": scope,
        }

    def summary(self) -> dict:
        """Current queue: unfinished jobs plus those completed within COMPLETED_WINDOW."""
        with self.library.connection() as db:
            db.execute("BEGIN")
            counts = dict(
                db.execute(
                    "SELECT state,count(*) FROM download_job WHERE "
                    "state NOT IN ('done','cancelled') GROUP BY state "
                    "UNION ALL SELECT 'done',count(*) FROM download_job WHERE state='done' AND updated_at>=?",
                    (int(time.time()) - COMPLETED_WINDOW,),
                ).fetchall()
            )
            # A job retrying later (backoff, GP top-up) is not about to run: it is not "waiting".
            waiting = db.execute(
                "SELECT count(*) FROM download_job WHERE state='queued' AND retry_at<=?",
                (int(time.time()) + WAITING_WITHIN,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT id,state,input_url,received,total,retry_at,attempts,error_code,"
                "json_extract(details_json,'$.selected.metadata.title') AS title "
                "FROM download_job WHERE state NOT IN ('done','cancelled') "
                "ORDER BY CASE WHEN state IN ('downloading','importing','inspecting','submitting') "
                "THEN 0 ELSE 1 END,created_at LIMIT 8"
            ).fetchall()
            storage_wait_until = db.execute(
                "SELECT coalesce(max(retry_at),0) FROM download_job WHERE state='queued' "
                "AND error_code='insufficient_storage'"
            ).fetchone()[0]
        items = [dict(row) for row in rows]
        with self.progress_lock:
            for item in items:
                if item["state"] in ACTIVE and (sample := self.progress.get(item["id"])):
                    item["received"] = sample[1]
        return {
            "blocked": self.http.blocked,
            "running": sum(counts.get(state, 0) for state in ACTIVE),
            "waiting": waiting,
            "needs_you": sum(
                counts.get(state, 0) for state in ("needs_approval", "verification_required", "error")
            ),
            "completed": counts.get("done", 0),
            "total": sum(counts.values()),
            "items": items,
            "storage_wait_until": storage_wait_until,
            "storage_reserve_bytes": self.settings.download_reserve_bytes,
        }

    def _require_download_space(self, path, additional_bytes: int) -> None:
        # Transfers write only to the download spool. Each job has its own retry_at on low space.
        if shutil.disk_usage(path).free < self.settings.download_reserve_bytes + additional_bytes:
            raise SourceError("insufficient_storage", STORAGE_RECHECK)

    def _progress(self, identifier: str, received: int):
        now = time.monotonic()
        with self.progress_lock:
            previous = self.progress.get(identifier)
            speed = max(0, received - previous[1]) / max(now - previous[0], 0.001) if previous else 0
            self.progress[identifier] = (now, received, speed)

    def _update(
        self,
        identifier: str,
        *,
        state: str | None = None,
        details: dict | None = None,
        error: str | None = None,
        **extra,
    ):
        """Persist job fields; while the user is cancelling/pausing, only a final state may land."""
        allowed = {"remote_secret", "source_json", "book_id", "received", "total", "retry_at", "attempts"}
        if not extra.keys() <= allowed:
            raise ValueError("Unknown download field")
        if state is not None and "received" not in extra:
            with self.progress_lock:
                sample = self.progress.get(identifier)
            if sample:
                extra["received"] = sample[1]
        values = {"error_code": error, "updated_at": int(time.time()), **extra}
        if state is not None:
            values["state"] = state
        if details is not None:
            saved = {**details}
            if "quote" in saved:
                saved["quote"] = {**saved["quote"]}
                saved["quote"].pop("balance_gp", None)
            values["details_json"] = canonical_json(saved)
        with self.library.transaction() as db:
            current = db.execute(
                "SELECT state,details_json FROM download_job WHERE id=?", (identifier,)
            ).fetchone()
            if (
                current
                and current[0] in {"cancelling", "pausing"}
                and values.get("state") not in {"done", "cancelled", "paused"}
            ):
                values.pop("state", None)
            if current and values.get("state") in TERMINAL:
                values["details_json"] = _stored_details(
                    settle(details if details is not None else json.loads(current[1]))
                )
            db.execute(
                "UPDATE download_job SET revision=revision+1,"
                + ",".join(key + "=?" for key in values)
                + " WHERE id=?",
                (*values.values(), identifier),
            )

    @staticmethod
    def _same_gallery(db, source: Source, *, exclude: str = ""):
        """The job of `source`'s gallery: E-Hentai and ExHentai links of one gallery id share a job."""
        return db.execute(
            "SELECT id,state,revision,details_json FROM download_job WHERE id!=? AND "
            "gallery_key=? ORDER BY rowid LIMIT 1",
            (exclude, f"{site_group(source.site)[0]}:{source.gallery_id}"),
        ).fetchone()

    def create(self, url: str) -> dict:
        """Queue a gallery, or return its existing job; `outcome` says which.

        outcome is "created" (new job) or "existing"
        (the job is returned as it is; a done job stays done until explicitly redownloaded).
        """
        try:
            source = gallery_url(url)
        except ValueError:
            raise InvalidInput(
                "Expected an E-Hentai or ExHentai gallery URL", code="unsupported_source"
            ) from None
        return self._enqueue_many([source])[0]

    def _enqueue_many(self, entries: list[Source]) -> list[dict]:
        if not entries:
            return []
        results = []
        while True:
            with self.library.connection() as db:
                candidates = [self._same_gallery(db, source) for source in entries]
            identifiers = [row["id"] if row else str(uuid.uuid4()) for row in candidates]
            locks_by_index = {self._lock_index(identifier): identifier for identifier in identifiers}
            # Acquire job locks in a fixed order, outside the transaction. Revalidate after
            # locking so a concurrent creation/deletion cannot race a requeue's cleanup.
            with ExitStack() as locks:
                for index in sorted(locks_by_index):
                    locks.enter_context(self._job_lock(locks_by_index[index]))
                with self.library.transaction() as db:
                    current = [self._same_gallery(db, source) for source in entries]
                    if any(
                        row and row["id"] != identifier
                        for row, identifier in zip(current, identifiers, strict=True)
                    ):
                        continue
                    for source, identifier in zip(entries, identifiers, strict=True):
                        existing = self._same_gallery(db, source)
                        now = int(time.time())
                        details = {"phase": "inspect", "mode": self.settings.default_quality}
                        outcome = "existing"
                        if not existing:
                            outcome = "created"
                            db.execute(
                                "INSERT INTO download_job"
                                "(id,input_url,source_json,state,details_json,created_at,updated_at) "
                                "VALUES(?,?,?,'queued',?,?,?)",
                                (
                                    identifier,
                                    source.url,
                                    source.model_dump_json(),
                                    _stored_details(details),
                                    now,
                                    now,
                                ),
                            )
                        results.append({**self._output(self._read(db, identifier)), "outcome": outcome})
            break
        if any(job["outcome"] != "existing" for job in results):
            self.wake.set()
        return results

    def create_many(self, urls: list[str]) -> list[dict]:
        """Validate each line, commit all distinct galleries together, then wake workers once."""
        if not 1 <= len(urls) <= BATCH_LIMIT:
            raise InvalidInput(f"Expected 1 to {BATCH_LIMIT} URLs", code="invalid_batch")
        entries: dict[str, Source] = {}
        lines = []
        for url in urls:
            try:
                source = gallery_url(url)
            except ValueError:
                try:
                    parsed = urlsplit(url.strip())
                    web = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                except ValueError:
                    web = False
                lines.append((url, None, "unsupported_source" if web else "invalid_url"))
                continue
            key = f"eh:{source.gallery_id}"
            entries.setdefault(key, source)
            lines.append((url, key, None))
        jobs = dict(zip(entries, self._enqueue_many(list(entries.values())), strict=True))
        return [{"url": url, "job": jobs.get(key), "error": error} for url, key, error in lines]

    def action(
        self,
        identifier: str,
        action: str,
        revision: int,
        *,
        mode: str = "org",
        strict_revision: bool = False,
    ) -> dict:
        with self._job_lock(identifier):
            job = self.get(identifier, private=True)
            if strict_revision and job["revision"] != revision:
                raise RevisionConflict("Download changed")
            if action in {"cancel", "redownload"} and job["state"] not in ACTIVE | {
                "cancelling",
                "pausing",
            }:
                # Validate the action before abandoning its import. A stale command changes nothing.
                if action == "cancel" and job["state"] != "done":
                    if self._abandon_download_import(job):
                        self._remove_files(identifier)
                        return self.get(identifier)
                elif action == "redownload" and job["revision"] == revision and job["state"] in TERMINAL:
                    self._abandon_download_import(job)
                    job = self.get(identifier, private=True)
                    revision = job["revision"]
                    self._remove_files(identifier)
            state = self._act(identifier, action, revision, mode=mode, strict_revision=strict_revision)
            if state in {"cancelling", "pausing"}:
                self.stop_requests[identifier] = state
                self.interrupted.add(identifier)
                self.http.interrupt(identifier)
            else:
                self.interrupted.discard(identifier)
            LOGGER.info("Download %s: %s requested; now %s", short_id(identifier), action, state)
            if state == "cancelled":
                self._remove_files(identifier)
        self.wake.set()
        return self.get(identifier)

    def _act(
        self,
        identifier: str,
        action: str,
        revision: int,
        *,
        mode: str,
        strict_revision: bool = False,
    ) -> str:
        error, retry_at = None, 0
        with self.library.transaction() as db:
            job = self._read(db, identifier)
            if job["revision"] != revision and (action != "cancel" or strict_revision):
                raise RevisionConflict("Download changed")
            state, details, source = job["state"], job["details"], Source.model_validate(job["source"])
            if source.site not in EH_SITES and action != "cancel":
                raise InvalidInput("This source is no longer supported", code="source_retired")
            if action == "cancel":
                if state == "done":
                    raise InvalidInput("Completed download is already in the library", code="download_done")
                state = "cancelling" if state in ACTIVE | {"cancelling", "pausing"} else "cancelled"
                if state == "cancelled":
                    details.pop("archive_sha256", None)
            elif action == "pause" and state in ACTIVE | {"queued", "pausing"}:
                state = "pausing" if state in ACTIVE | {"pausing"} else "paused"
            elif action == "approve" and state == "needs_approval":
                if mode not in {"org", "res", "reuse"} or (mode == "reuse" and not details.get("reusable")):
                    raise InvalidInput("Invalid archive type", code="invalid_mode")
                price = "original_gp" if mode == "org" else "resample_gp"
                cost = 0 if mode == "reuse" else details["quote"].get(price)
                if cost is None:
                    raise InvalidInput("Archive price is unavailable", code="price_unavailable")
                if mode != "reuse" and details.get("reusable"):
                    # The user chose a new archive over the prepared one the archiver page offered.
                    db.execute("UPDATE download_job SET remote_secret=NULL WHERE id=?", (identifier,))
                details.update(mode=mode, approved_mode=mode, approved_gp=cost, phase="submit")
                state = "queued"
            elif action in {"resume", "recover"} and state in {
                "paused",
                "error",
                "verification_required",
            }:
                phase = details.get("phase")
                linked = bool(job["remote_secret"]) and phase in {"transfer", "import"}
                imported = phase == "import" and bool(details.get("archive_sha256"))
                if _uncertain(details) or phase == "recover":
                    # Recovery reads the archiver page first; it requests again only at the approved price.
                    details["phase"] = "recover"
                elif (linked or imported) and job["error_code"] in {
                    "download_invalid_archive",
                    "archive_size_mismatch",
                    "source_range_invalid",
                }:
                    # Importing the same bytes again fails the same way: fetch the archive again
                    # through the link already obtained (no new archive request, so no new charge).
                    for name in ("archive_sha256", "etag", "import_error"):
                        details.pop(name, None)
                    details["phase"] = "transfer"
                    db.execute("UPDATE download_job SET received=0,total=NULL WHERE id=?", (identifier,))
                elif linked or imported:
                    details["phase"] = "import" if details.get("archive_sha256") else "transfer"
                elif phase == "submit" and "approved_gp" in details:
                    pass
                else:
                    for name in STARTING_OVER:
                        details.pop(name, None)
                    details["phase"] = "inspect"
                    db.execute("UPDATE download_job SET remote_secret=NULL WHERE id=?", (identifier,))
                state = "queued"
                if halted := self.http.cooldowns().get(source.site):
                    # The job starts by itself once the site's halt ends.
                    LOGGER.info("Download %s waits for the %s halt to end", short_id(identifier), source.site)
                    error, retry_at = "source_rate_limited", halted
            elif action == "redownload" and state in TERMINAL:
                # Explicit only: adding the gallery again returns the done job unchanged. The
                # inspection finds the book in the library, so a new archive still needs approval.
                details["phase"] = "inspect"
                state = "queued"
            else:
                raise InvalidInput("Action is not available in this state", code="action_unavailable")
            if action == "redownload":
                approval = details.get("approved_gp") if job["state"] == "cancelled" else None
                approved_mode = details.get("approved_mode", details.get("mode", "org"))
                for name in STARTING_OVER:
                    details.pop(name, None)
                if approval is not None:
                    details.update(
                        approved_gp=approval,
                        approved_mode=approved_mode,
                        mode=approved_mode,
                        phase="submit",
                        retry_approved=True,
                    )
                db.execute(f"UPDATE download_job SET {RESET_COLUMNS} WHERE id=?", (identifier,))
            if state in TERMINAL:
                settle(details)
            attempts = 0 if action in {"approve", "resume", "recover", "redownload"} else job["attempts"]
            db.execute(
                "UPDATE download_job SET state=?,source_json=?,details_json=?,error_code=?,retry_at=?,"
                "attempts=?,revision=revision+1,updated_at=? WHERE id=?",
                (
                    state,
                    source.model_dump_json(),
                    _stored_details(details),
                    error,
                    retry_at,
                    attempts,
                    int(time.time()),
                    identifier,
                ),
            )
        return state

    def _share_balance(self, db, balance: int):
        db.execute(
            "INSERT INTO source_account VALUES('ehentai',?,?) ON CONFLICT(site_group) DO UPDATE SET "
            "balance_gp=excluded.balance_gp,checked_at=excluded.checked_at",
            (max(0, balance), int(time.time())),
        )

    def _quote(self, source: Source, *, cancelled=None):
        quote, reusable = self.sources.quote(source, cancelled=cancelled)
        if quote.get("balance_gp") is not None:
            with self.library.transaction() as db:
                self._share_balance(db, quote["balance_gp"])
        return quote, reusable

    def _gp_probe(self, *, force: bool = False, cancelled=None) -> dict:
        with self.library.connection() as db:
            previous = db.execute(
                "SELECT balance_gp,checked_at FROM source_account WHERE site_group='ehentai'"
            ).fetchone()
        result = {
            "balance_gp": previous[0] if previous else None,
            "checked_at": previous[1] if previous else None,
            "released": 0,
        }
        if not self.gp_lock.acquire(blocking=False):
            return {**result, "probing": True}
        try:
            now = time.time()
            if self.http.blocked:
                raise SourceError(self.http.blocked, unsent=True)
            with self.library.connection() as db:
                waiting = db.execute(
                    "SELECT id,source_json,details_json FROM download_job WHERE state='queued' AND "
                    "error_code='insufficient_gp' ORDER BY created_at,rowid"
                ).fetchall()
                if not force and (not waiting or now < self.gp_next_at):
                    return result
                candidates = [
                    *waiting,
                    *db.execute(
                        "SELECT source_json FROM download_job WHERE gallery_key LIKE 'ehentai:%' "
                        "ORDER BY created_at DESC LIMIT 10"
                    ).fetchall(),
                ]
            if not candidates:
                return {**result, "no_gallery": True}
            next_interval = GP_RETRY if force else min(900, self.gp_interval * 2)
            self.gp_next_at = now + self.gp_interval
            self.gp_interval = next_interval

            def check():
                return self.stopping.is_set() or bool(cancelled and cancelled())

            balance = None
            failure = None
            seen = set()
            for candidate in candidates:
                if candidate["source_json"] in seen:
                    continue
                seen.add(candidate["source_json"])
                try:
                    quote, _ = self.sources.quote(
                        Source.model_validate_json(candidate["source_json"]), cancelled=check
                    )
                    balance = quote.get("balance_gp")
                    if balance is not None:
                        break
                except SourceError as error:
                    failure = error
                    LOGGER.warning("GP balance probe: %s", error.code)
                    if error.code not in {
                        "gallery_unavailable",
                        "source_unavailable",
                        "source_protocol",
                        "source_login_required",
                    }:
                        break
            if balance is None:
                if failure and force:
                    raise failure
                return result
            released, available = 0, balance
            with self.library.transaction() as db:
                self._share_balance(db, balance)
                for row in waiting:
                    cost = json.loads(row["details_json"]).get("approved_gp")
                    if cost is None or cost > available:
                        continue
                    updated = db.execute(
                        "UPDATE download_job SET retry_at=0,error_code=NULL,revision=revision+1 "
                        "WHERE id=? AND "
                        "state='queued' AND error_code='insufficient_gp'",
                        (row["id"],),
                    ).rowcount
                    if updated:
                        available -= cost
                        released += 1
            self.gp_interval = GP_RETRY if released or force else next_interval
            self.gp_next_at = now + self.gp_interval
            self.wake.set()
            return {"balance_gp": balance, "checked_at": int(now), "released": released}
        finally:
            self.gp_lock.release()

    def delete(self, identifier: str, *, expected_revision: int | None = None) -> None:
        """Forget a finished or failed job; a book it imported stays in the library."""
        with self._job_lock(identifier):
            job = self.get(identifier, private=True)
            if expected_revision is not None and job["revision"] != expected_revision:
                raise RevisionConflict("Download changed")
            if job["state"] not in CLEARABLE or (job["state"] == "error" and _uncertain(job["details"])):
                raise InvalidInput("Only finished downloads can be cleared", code="download_active")
            self._abandon_download_import(job)
            with self.library.transaction() as db:
                job = self._read(db, identifier)
                if expected_revision is not None and job["revision"] != expected_revision:
                    raise RevisionConflict("Download changed")
                if job["state"] not in CLEARABLE or (job["state"] == "error" and _uncertain(job["details"])):
                    raise InvalidInput("Only finished downloads can be cleared", code="download_active")
                db.execute("DELETE FROM download_job WHERE id=?", (identifier,))
            self._remove_files(identifier)
            with self.progress_lock:
                self.progress.pop(identifier, None)

    def purge(self) -> int:
        """Forget jobs settled more than RETENTION_SECONDS ago."""
        with self.library.connection() as db:
            candidates = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM download_job WHERE state IN ('done','cancelled') AND updated_at<?",
                    (int(time.time()) - RETENTION_SECONDS,),
                )
            ]
        removed = []
        for identifier in candidates:
            with self._job_lock(identifier):
                job = self.get(identifier, private=True)
                if (
                    job["state"] not in {"done", "cancelled"}
                    or job["updated_at"] >= int(time.time()) - RETENTION_SECONDS
                ):
                    continue
                self._abandon_download_import(job)
                with self.library.transaction() as db:
                    db.execute("DELETE FROM download_job WHERE id=?", (identifier,))
                removed.append(identifier)
        for identifier in removed:
            self._remove_files(identifier)
        return len(removed)

    def _cancelled(self, identifier: str) -> bool:
        """Stop promptly for shutdown or an action recorded by this process."""
        return self.stopping.is_set() or identifier in self.interrupted

    def _path(self, identifier):
        if str(uuid.UUID(identifier)) != identifier:
            raise ValueError("Invalid download identifier")
        self.directory.mkdir(mode=0o700, exist_ok=True)
        return self.directory / (identifier + ".zip")

    def _remove_files(self, identifier):
        path = self._path(identifier)
        path.unlink(missing_ok=True)
        path.with_name(path.name + ".mangacouch.json").unlink(missing_ok=True)
        self.hashed.pop(identifier, None)

    def _abandon_download_import(self, job: dict) -> bool:
        import_id = job["details"].get("import_job")
        if not import_id:
            return False
        book_id = self.files.abandon_import(import_id)
        if book_id is not None and job["state"] != "done":
            self._complete_import(job, ImportResult(self.library.get_book(book_id), False, import_id, ()))
            return True
        return False

    def _cleanup(self, identifier, states: set[str]):
        """Delete the job's archive if it is still in one of `states`; a resumed job keeps it."""
        with self._job_lock(identifier):
            try:
                job = self.get(identifier, private=True)
            except NotFound:
                return
            if job["state"] in states:
                try:
                    self._abandon_download_import(job)
                    self._remove_files(identifier)
                except (OSError, sqlite3.Error):
                    LOGGER.warning("Download %s: temporary file cleanup deferred", short_id(identifier))

    def _library_matches(self, source: Source, metadata: Metadata) -> list[dict]:
        """Books that already hold this gallery ("same", any EH host) or an edition of it ("edition").

        Editions are the gallery's parent and newer versions, and books that list this gallery as
        their newer version; at most LIBRARY_MATCHES books.
        """
        found: dict[str, dict] = {}
        with self.library.connection() as db:
            db.execute("BEGIN")
            for row in with_source(db, source, LIBRARY_MATCHES):
                found.setdefault(
                    row["id"], {"id": row["id"], "title": display_title(row), "relation": "same"}
                )
            editions = [
                *(
                    with_source(db, item, LIBRARY_MATCHES)
                    for item in (metadata.parent, *metadata.newer_versions)
                    if item
                ),
                listing_newer(db, source, LIBRARY_MATCHES),
            ]
            for rows in editions:
                for row in rows:
                    found.setdefault(
                        row["id"], {"id": row["id"], "title": display_title(row), "relation": "edition"}
                    )
        return list(found.values())[:LIBRARY_MATCHES]

    def _prefetch_queued(self, identifier: str, source: Source, cancelled) -> None:
        """Fetch this gallery's gdata together with the queued inspections of its site that lack
        it: one EH API call for up to GDATA_BATCH galleries instead of one each, when many
        galleries are added at once. A failed batch leaves every inspection to fetch on its own."""
        while not self.prefetch_lock.acquire(timeout=0.1):
            if cancelled():
                raise InterruptedError
        try:
            if self.sources.prefetched(source):
                return
            with self.library.connection() as db:
                rows = db.execute(
                    "SELECT source_json FROM download_job WHERE state='queued' AND id<>? "
                    "AND phase='inspect' "
                    "AND json_extract(source_json,'$.site')=? ORDER BY created_at,rowid LIMIT ?",
                    (identifier, source.site, BATCH_LIMIT),
                ).fetchall()
            waiting = [Source.model_validate_json(row[0]) for row in rows]
            batch = [source, *[other for other in waiting if not self.sources.prefetched(other)]]
            if len(batch) == 1:
                return
            try:
                self.sources.prefetch(batch[:GDATA_BATCH], cancelled=cancelled)
            except SourceError as error:
                LOGGER.info("Download %s: batch metadata fetch skipped: %s", short_id(identifier), error.code)
        finally:
            self.prefetch_lock.release()

    def _inspect(self, job):
        identifier, source, details = job["id"], Source.model_validate(job["source"]), job["details"]

        def check():
            return self._cancelled(identifier)

        self._update(identifier, state="inspecting")
        self._prefetch_queued(identifier, source, check)
        observation = self.sources.fetch(source, comments=self.settings.comments, cancelled=check)
        details["library"] = self._library_matches(source, Metadata.model_validate(observation["metadata"]))
        details["selected"] = observation
        quote, reusable = self._quote(source, cancelled=check)
        # A prepared link the archiver page offers costs nothing more, but its quality is unknown:
        # it is its own choice ("reuse"), never passed off as the original or the resample.
        if reusable:
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL))
            self._update(identifier, remote_secret=remote)
        elif details.get("reusable"):
            # The prepared link an earlier inspection stored is no longer offered.
            self._update(identifier, remote_secret=None)
        details["reusable"] = bool(reusable)
        details["quote"] = quote
        # The chosen quality is preselected; the prepared link only when that quality has no price,
        # and it is never started without approval, since its quality is unknown.
        mode = self.settings.default_quality
        if reusable and quote.get("original_gp" if mode == "org" else "resample_gp") is None:
            mode = "reuse"
        details["mode"] = mode
        cost = 0 if mode == "reuse" else quote.get("original_gp" if mode == "org" else "resample_gp")
        title = observation["metadata"].get("title") or source.url
        unasked = self.settings.auto_free and not details["library"]
        if unasked and mode != "reuse" and cost == 0:
            details.update(mode=mode, approved_mode=mode, approved_gp=0, phase="submit")
            # A free archive of the chosen quality, not the prepared link of unknown quality.
            self._update(identifier, state="queued", details=details, remote_secret=None, attempts=0)
            LOGGER.info(
                "Download %s inspected: %s; free %s archive, starting", short_id(identifier), title, mode
            )
        else:
            self._update(identifier, state="needs_approval", details=details, attempts=0)
            LOGGER.info(
                "Download %s inspected: %s; %s archive costs %s GP%s, waiting for approval",
                short_id(identifier),
                title,
                mode,
                "?" if cost is None else cost,
                f"; {len(details['library'])} matching book(s) already in the library"
                if details["library"]
                else "",
            )

    def _submit(self, job):
        identifier, source, details = job["id"], Source.model_validate(job["source"]), job["details"]

        def check():
            return self._cancelled(identifier)

        details.setdefault("approved_mode", details["mode"])
        if job["remote_secret"]:
            details["phase"] = "transfer"
            self._update(identifier, state="queued", details=details)
            return
        quote, reusable = self._quote(source, cancelled=check)
        cost = quote.get("original_gp" if details["mode"] == "org" else "resample_gp")
        if details["mode"] == "reuse" and reusable:
            details["phase"] = "transfer"
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL))
            self._update(identifier, state="queued", details=details, remote_secret=remote)
            return
        if details["mode"] == "reuse" or (
            reusable and not details.get("reusable") and (not details.get("retry_approved") or cost is None)
        ):
            # The prepared link the approval was for is gone, or one appeared since: ask again
            # rather than pay for an archive the user did not choose, or twice for one they have.
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL)) if reusable else None
            details.update(
                quote=quote, reusable=bool(reusable), mode="reuse" if reusable else "org", phase="inspect"
            )
            self._update(
                identifier,
                state="needs_approval",
                details=details,
                error="source_price_changed",
                remote_secret=remote,
            )
            LOGGER.info("Download %s: prepared archive changed; waiting for approval", short_id(identifier))
            return
        if not self._approved_price_holds(details, quote):
            details.update(quote=quote, phase="inspect")
            self._update(identifier, state="needs_approval", details=details, error="source_price_changed")
            LOGGER.info(
                "Download %s: price changed to %s GP; waiting for approval", short_id(identifier), cost
            )
            return
        if cost is not None and quote.get("balance_gp") is not None and quote["balance_gp"] < cost:
            details["quote"] = quote
            self._update(identifier, details=details)
            raise SourceError("insufficient_gp")
        if check():
            raise InterruptedError
        # Durable boundary before the only potentially charging remote request.
        details["phase"] = "uncertain"
        details["submission_uncertain"] = True
        self._update(identifier, state="submitting", details=details)
        try:
            remote = self.sources.submit(source, details["mode"], cancelled=check)
        except NotSent:
            details.update(phase="submit", submission_uncertain=False)
            self._update(identifier, details=details)
            raise
        details["phase"] = "transfer"
        details["submission_uncertain"] = False
        # The link exists only in memory until stored, and was paid for: store it first, outwaiting
        # a busy database, before anything else may fail.
        self._store_paid_link(identifier, self.vault.encrypt(remote), details)
        LOGGER.info("Download %s: %s archive requested (%s GP)", short_id(identifier), details["mode"], cost)
        if quote.get("balance_gp") is not None and cost is not None:
            # The archiver page quoted the balance before this charge; the others now see the rest.
            try:
                with self.library.transaction() as db:
                    self._share_balance(db, quote["balance_gp"] - cost)
            except sqlite3.Error as error:
                # Only the GP figure other approvals show; their submissions quote again anyway.
                LOGGER.warning("Download %s: GP balance not shared: %s", short_id(identifier), error)
        if check():
            raise InterruptedError
        self._update(identifier, state="queued")

    def _store_paid_link(self, identifier: str, remote_secret: str, details: dict) -> None:
        """Keep a paid link through transient lock contention, including one final stop attempt."""
        with self.worker_lock:
            self.storing_links.add(identifier)
        try:
            stopping_retry = False
            for attempt in range(PARK_TRIES):
                try:
                    self._update(identifier, remote_secret=remote_secret, details=details)
                    return
                except sqlite3.Error as error:
                    if not busy(error) or stopping_retry or attempt + 1 == PARK_TRIES:
                        raise
                    LOGGER.warning("Download %s: database busy; storing the link again", short_id(identifier))
                    stopping_retry = self.stopping.wait(BUSY_RETRY)
        finally:
            with self.worker_lock:
                self.storing_links.discard(identifier)

    def _recover(self, job):
        source, details = Source.model_validate(job["source"]), job["details"]
        charged = _uncertain(details) or bool(details.get("may_be_charged"))
        for name in ("archive_sha256", "etag"):
            details.pop(name, None)
        quote, reusable = self._quote(source, cancelled=lambda: self._cancelled(job["id"]))
        if reusable:
            details.pop("etag", None)
            details.update(phase="transfer", submission_uncertain=False, mode="reuse", reusable=True)
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL))
            self._update(
                job["id"], state="queued", details=details, remote_secret=remote, received=0, total=None
            )
        elif self._approved_price_holds(details, quote):
            details.update(phase="submit", submission_uncertain=False)
            self._update(
                job["id"], state="queued", details=details, remote_secret=None, received=0, total=None
            )
        else:
            details.update(phase="inspect", quote=quote, may_be_charged=charged, submission_uncertain=False)
            self._update(
                job["id"],
                state="needs_approval",
                details=details,
                error="source_price_changed" if charged else "source_link_expired",
            )

    @staticmethod
    def _approved_price_holds(details: dict, quote: dict) -> bool:
        """The approved quality costs at most what the user approved (nothing once EH unlocked it)."""
        if "approved_gp" not in details or details.get("mode") not in {"org", "res"}:
            return False
        cost = quote.get("original_gp" if details["mode"] == "org" else "resample_gp")
        return cost is not None and cost <= details["approved_gp"]

    def _transfer(self, job):
        identifier, details = job["id"], job["details"]
        remote = self.vault.decrypt(job["remote_secret"])
        if remote.get("expires_at", 0) <= time.time():
            self._relink(job, details, "source_link_expired")
            return
        try:
            self._receive(job, details, remote)
        except SourceError as error:
            # An archive server refusing the link (403) or no longer knowing it (404, 410) answers
            # every retry the same way.
            if error.status == 416 and self._path(identifier).is_file():
                path = self._path(identifier)
                digest = hash_file(path, cancelled=lambda: self._cancelled(identifier))
                if details.get("archive_sha256") and digest != details["archive_sha256"]:
                    raise SourceError("download_invalid_archive") from error
                self._finish_transfer(identifier, details, digest, path.stat().st_size)
                return
            if error.status not in {403, 404, 410}:
                raise
            LOGGER.warning("Download %s: archive server answered HTTP %d", short_id(identifier), error.status)
            self._relink(job, details, "archive_link_invalid")
        finally:
            path = self._path(identifier)
            if path.is_file():
                self._progress(identifier, path.stat().st_size)

    def _receive(self, job, details: dict, remote: dict):
        identifier, limit = job["id"], self.settings.download_limit_bytes

        def cancelled():
            return self._cancelled(identifier)

        path = self._path(identifier)
        if path.exists() and path.stat().st_nlink > 1:
            path.unlink()
            details.pop("etag", None)
        if path.exists() and job["total"] is not None and path.stat().st_size == job["total"]:
            self._finish_transfer(identifier, details, hash_file(path, cancelled=cancelled), job["total"])
            return
        offset = path.stat().st_size if path.exists() and details.get("etag") else 0
        headers = {"Range": f"bytes={offset}-", "If-Range": details["etag"]} if offset else {}
        self._update(identifier, state="downloading")
        with self.http.stream(
            "GET", remote["url"], headers=headers, total_timeout=24 * 3600, cancelled=cancelled
        ) as response:
            if "html" in response.headers.get("content-type", ""):
                self._archive_page(job, details, response)
                return
            declared = response.headers.get("content-length")
            length = int(declared) if declared and declared.isdecimal() else None
            total = length
            if response.status_code == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                if (
                    not match
                    or (offset > 0 and response.headers.get("etag") != details.get("etag"))
                    or int(match[1]) != offset
                    or int(match[2]) != int(match[3]) - 1
                    or (length is not None and length != int(match[3]) - offset)
                ):
                    raise SourceError("source_range_invalid")
                total = int(match[3])
            else:
                offset = 0
            LOGGER.info(
                "Download %s: transferring %s%s",
                short_id(identifier),
                "?" if total is None else f"{total / 1024**2:.1f} MiB",
                f" from {offset / 1024**2:.1f} MiB" if offset else "",
            )
            started = time.monotonic()
            if total is not None and total > limit:
                raise SourceError("download_too_large")
            self._require_download_space(path.parent, max(0, total - offset) if total else 1024**2)
            etag = response.headers.get("etag")
            details["etag"] = etag if etag and not etag.startswith("W/") else None
            self._update(identifier, details=details, received=offset, total=total)
            self._progress(identifier, offset)
            digest = hashlib.sha256()
            if offset:
                with path.open("rb") as existing:
                    while chunk := existing.read(min(CHUNK_SIZE, offset - existing.tell())):
                        digest.update(chunk)
            received, sampled = offset, time.monotonic()
            next_space_check = offset + SPACE_CHECK_BYTES
            if not offset:
                path.unlink(missing_ok=True)
            with path.open("r+b" if offset else "xb") as output:
                output.seek(offset)
                output.truncate()
                for chunk in response_chunks(response):
                    if cancelled():
                        raise InterruptedError
                    received += len(chunk)
                    if received > limit or (total is not None and received > total):
                        raise SourceError("download_too_large")
                    if received >= next_space_check:
                        self._require_download_space(path.parent, max(SPACE_CHECK_BYTES, len(chunk)))
                        next_space_check = received + SPACE_CHECK_BYTES
                    output.write(chunk)
                    digest.update(chunk)
                    now = time.monotonic()
                    if now - sampled >= 0.5:
                        self._progress(identifier, received)
                        sampled = now
                output.flush()
                os.fsync(output.fileno())
            if total is not None and received != total:
                raise SourceError("source_truncated")
        elapsed = time.monotonic() - started
        LOGGER.info(
            "Download %s: received %.1f MiB in %.0f s (%.1f MiB/s)",
            short_id(identifier),
            (received - offset) / 1024**2,
            elapsed,
            (received - offset) / 1024**2 / max(elapsed, 0.001),
        )
        self._finish_transfer(identifier, details, digest.hexdigest(), received)

    def _relink(self, job, details: dict, code: str) -> None:
        """Recover an expired or refused archive link within the approved price."""
        details["phase"] = "recover"
        failures = job["attempts"] + 1
        self._update(
            job["id"],
            state="error" if failures >= MAX_FAILURES else "queued",
            details=details,
            error=code,
            attempts=failures,
            retry_at=0 if failures >= MAX_FAILURES else int(time.time()) + min(1800, 15 * 2**failures),
        )

    def _archive_page(self, job, details: dict, response) -> None:
        """Settle a transfer answered with a web page instead of the archive.

        Only a page that says the archive is still being prepared is worth waiting for. Any other
        page (an expired or invalid link, an error) would answer the same way on every retry, so
        the link is replaced (`_relink`).
        """
        body = bytearray()
        for chunk in response.iter_bytes():
            body.extend(chunk)
            if len(body) >= 65536:
                break
        page = bytes(body[:65536])
        if code := refusal(page) or ("source_login_required" if b"kokomade" in page.lower() else None):
            raise SourceError(code)
        text = " ".join(page.decode("utf-8", "replace").split()).lower()
        if PREPARING.search(text):
            raise SourceError("archive_preparing", 15)
        LOGGER.warning(
            "Download %s: archive server answered with an unrecognized page (%d bytes): %.200s",
            short_id(job["id"]),
            len(page),
            # Links on such a page may carry archive keys.
            re.sub(r"(?:https?:)?//\S+", "<link>", text),
        )
        self._relink(job, details, "archive_link_invalid")

    def _finish_transfer(self, identifier: str, details: dict, sha256: str, received: int):
        stat = self._path(identifier).stat()
        self.hashed[identifier] = (sha256, stat.st_size, stat.st_mtime_ns)
        details["archive_sha256"] = sha256
        details["phase"] = "import"
        details.pop("retry_approved", None)
        self._update(identifier, state="queued", details=details, received=received, attempts=0)

    def _verified(self, identifier: str, sha256: str, cancelled) -> bool:
        """The archive still has `sha256`; trust this process's own streaming hash if unchanged."""
        path = self._path(identifier)
        if not path.exists():
            return False
        stat = path.stat()
        if self.hashed.get(identifier) == (sha256, stat.st_size, stat.st_mtime_ns):
            return True
        return hash_file(path, cancelled=cancelled) == sha256

    def _import(self, job):
        identifier, details = job["id"], job["details"]

        def cancelled():
            return self._cancelled(identifier)

        path = self._path(identifier)
        if not self._verified(identifier, details["archive_sha256"], cancelled):
            details.pop("archive_sha256", None)
            details["phase"] = "transfer"
            self._update(identifier, state="paused", details=details, error="download_file_missing")
            return
        self._update(identifier, state="importing")
        metadata = Metadata.model_validate(details["selected"]["metadata"])
        details["archive_variant"] = ARCHIVE_VARIANTS.get(details.get("mode", "org"), "unknown")
        if (
            job["total"] is None
            and details.get("mode") == "org"
            and metadata.source
            and metadata.declared_byte_size is not None
            and zip_image_bytes(path, self.files.limits) != metadata.declared_byte_size
        ):
            raise SourceError("archive_size_mismatch")
        native = NativeSidecar(
            schema="mangacouch.native/1",
            content_sha256=details["archive_sha256"],
            revision=1,
            metadata=metadata,
        )
        path.with_name(path.name + ".mangacouch.json").write_bytes(native.canonical_bytes())
        incoming = copy(self.files)
        incoming.limits = replace(self.files.limits, max_archive_bytes=self.settings.download_limit_bytes)
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", metadata.title or "gallery")[:160]
        if "preexisting" not in details:
            with self.library.connection() as db:
                details["preexisting"] = (
                    db.execute("SELECT 1 FROM book WHERE sha256=?", (details["archive_sha256"],)).fetchone()
                    is not None
                )
            self._update(identifier, details=details)
        if not details.get("import_job"):
            details["import_job"] = str(uuid.uuid4())
            self._update(identifier, details=details)
        # The download is ours to delete, so the importer may hard-link it instead of copying.
        result = incoming.import_file(
            path,
            original_filename=title + ".cbz",
            cancelled=cancelled,
            consume=True,
            known_sha256=details["archive_sha256"],
            job_id=details["import_job"],
        )
        self._complete_import(job, result)
        self._cleanup(identifier, {"done"})

    def _complete_import(self, job: dict, result: ImportResult) -> None:
        identifier, details = job["id"], job["details"]
        # The book is committed: what follows only adds to it, so a failure is a warning on the
        # finished job, never a reason to download the gallery again.
        warnings = []
        try:
            self.online.apply(
                result.book.id,
                {**details["selected"], "archive_variant": details["archive_variant"]},
                None,
                link_source=True,
            )
        except Exception:
            LOGGER.exception(
                "Download %s: source metadata of the imported book was not saved", short_id(identifier)
            )
            warnings.append("source_metadata_not_saved")
            source = Metadata.model_validate(details["selected"]["metadata"]).source
            if source is not None:
                self.online.defer(result.book.id, source, "metadata", 0)
        try:
            self.sync.sync_one(result.book.id, blocking=False)
        except Exception:
            LOGGER.warning("Download %s: sidecar sync remains pending", short_id(identifier), exc_info=True)
        # A committed import wins over a cancellation arriving after publication.
        related = [item["id"] for item in self.editions.candidates(result.book.id)]
        details.update(
            phase="done", duplicate=details.get("preexisting", not result.created), previous_books=related
        )
        if warnings:
            details["warnings"] = warnings
        self._update(identifier, state="done", details=details, book_id=result.book.id)
        LOGGER.info(
            "Download %s done: book %s %s%s",
            short_id(identifier),
            short_id(result.book.id),
            result.book.metadata.title or result.book.content.original_filename,
            " (already in the library)" if not result.created else "",
        )

    def _finish(self, identifier: str, error: SourceError | None, *, stop: str | None = None):
        """Settle a job whose phase stopped early: cancelled, paused, retried or failed."""
        with self.library.connection() as db:
            current = self._read(db, identifier)
        if current["state"] in TERMINAL:
            LOGGER.warning("Download %s: ignoring failure after %s", short_id(identifier), current["state"])
            return
        details, code = current["details"], error.code if error else None
        if current["state"] in {"cancelling", "pausing"}:
            stop = current["state"]
        stopping = error is None and self.stopping.is_set() and stop is None
        if stopping and current["state"] not in ACTIVE:
            return
        if error and details.get("phase") == "uncertain" and (error.unsent or code == "insufficient_gp"):
            # Only a known pre-send failure or an explicit insufficient-funds refusal proves no charge.
            details.update(phase="submit", submission_uncertain=False)
        uncertain = _uncertain(details)
        first_unknown = details.get("phase") == "uncertain"
        if uncertain:
            details.update(phase="recover", may_be_charged=True)
        retry_at, attempts = 0, current["attempts"]
        retry = error is not None and (retryable(error) or first_unknown)
        if stop in {"cancelling", "pausing"}:
            state = "cancelled" if stop == "cancelling" else "paused"
        elif stopping:
            state = "queued"
            if uncertain:
                retry_at = int(time.time()) + 15
        elif error is None:
            state = "paused"
        elif code in CREDENTIAL_ERRORS or code == "source_key_lost":
            state = "verification_required"
            if code == "source_key_lost":
                self.http.blocked = code
        elif code == "insufficient_storage":
            state, retry_at = "queued", int(time.time()) + STORAGE_RECHECK
        elif retry:
            state = "queued"
            if code == "insufficient_gp":
                retry_at = GP_PARKED
                if self.gp_next_at <= time.time():
                    self.gp_next_at = time.time() + self.gp_interval
            elif code == "source_rate_limited":
                retry_at = int(time.time()) + error.retry_after
            elif code in {"database_busy", "root_unavailable", "archive_preparing"}:
                retry_at = int(time.time()) + max(30, error.retry_after)
            else:
                attempts += 1
                if attempts >= MAX_FAILURES:
                    state = "error"
                else:
                    retry_at = int(time.time()) + min(
                        1800, max(error.retry_after, 15 * 2 ** min(attempts, 7))
                    )
        else:
            state = "error"
            retry_at = int(time.time()) + error.retry_after
        if state == "cancelled":
            details.pop("archive_sha256", None)
        self._update(identifier, details=details)
        self._update(identifier, state=state, error=code, retry_at=retry_at, attempts=attempts)
        if state in {"paused", "cancelled"} or stopping:
            LOGGER.info("Download %s %s", short_id(identifier), state)
        elif state == "queued":
            LOGGER.warning(
                "Download %s: %s; retrying in %d s%s",
                short_id(identifier),
                code,
                max(0, retry_at - int(time.time())),
                f" (attempt {attempts + 1})" if code not in UNCOUNTED_WAITS else "",
            )
        else:
            LOGGER.warning("Download %s stopped: %s (%s)", short_id(identifier), state, code)
        if state == "cancelled":
            self._cleanup(identifier, {"cancelled"})
        elif code == "download_too_large":
            self._cleanup(identifier, {"error", "paused"})

    def _invalid_archive(self, identifier: str, error: InvalidArchive) -> None:
        """The downloaded archive cannot become a book (damaged or unreadable page, bad ZIP, ...).

        This is not a storage problem and importing the same bytes again fails the same way, so the
        job stops in error with download_invalid_archive and keeps the file for the user. Resume
        transfers the archive again through the link it already has; nothing is paid again.
        """
        LOGGER.warning(
            "Download %s: archive cannot be imported: %s; the file stays at %s",
            short_id(identifier),
            error,
            self._path(identifier),
        )
        with self.library.connection() as db:
            details = self._read(db, identifier)["details"]
        details["import_error"] = (str(error) or type(error).__name__)[:500]
        self._update(identifier, details=details)
        self._finish(
            identifier, SourceError("download_invalid_archive"), stop=self.stop_requests.get(identifier)
        )

    def step(self, pool: str | None = None) -> bool:
        """Work on the oldest due job: of META_PHASES for pool "meta", of the other phases for
        "transfer", of any phase without a pool."""
        with self.worker_lock:
            pending = self.pending_parks.popitem() if self.pending_parks else None
        if pending:
            identifier, busy_failure = pending
            self._release_worker(identifier, busy_failure=busy_failure)
            return False
        if self.http.blocked and (self.http.blocked != "source_restore_paused" or pool == "meta"):
            return False
        if pool == "meta":
            self._gp_probe()
        condition = {
            None: "",
            "meta": f" AND phase IN {META_PHASES_SQL}",
            "transfer": f" AND phase NOT IN {META_PHASES_SQL}",
        }[pool]
        if self.http.blocked:
            condition += " AND phase='import'"
        capacity = META_WORKERS if pool == "meta" else WORKERS
        with self.worker_lock:
            working = list(self.working)
            owned = len(self.meta_working) if pool == "meta" else len(self.working) - len(self.meta_working)
            if owned >= capacity:
                return False
        query = (
            "SELECT id,phase FROM download_job WHERE state='queued' "
            "AND retry_at<=? AND id NOT IN (SELECT value FROM json_each(?)) "
            + condition
            + " ORDER BY created_at,rowid LIMIT 1"
        )
        parameters = (int(time.time()), json.dumps(working))
        with self.library.connection() as db:
            if db.execute(query, parameters).fetchone() is None:
                return False
        registered: str | None = None
        try:
            with self.library.transaction() as db:
                row = db.execute(query, parameters).fetchone()
                if row is None:
                    return False
                identifier, busy_failure = row[0], False
                # Take ownership while the write lock is held. A worker that has just queued the job
                # again (a shutdown, a retry) owns it until it has parked it; claiming it then would
                # briefly mark it inspecting, and that worker would park it as failed.
                with self.worker_lock:
                    if identifier in self.working:
                        return False
                    owned = (
                        len(self.meta_working)
                        if pool == "meta"
                        else len(self.working) - len(self.meta_working)
                    )
                    if owned >= capacity:
                        return False
                    self.working[identifier] = "ehentai"
                    if pool == "meta":
                        self.meta_working.add(identifier)
                registered = identifier
                self.interrupted.discard(identifier)
                self.stop_requests.pop(identifier, None)
                db.execute(
                    "UPDATE download_job SET state=?,revision=revision+1 WHERE id=? AND state='queued'",
                    (
                        {"transfer": "downloading", "import": "importing"}.get(row[1], "inspecting"),
                        identifier,
                    ),
                )
        except BaseException:
            if registered is not None:
                # The claim did not commit: the job is still queued for any worker.
                with self.worker_lock:
                    self.working.pop(registered, None)
                    self.meta_working.discard(registered)
            raise
        try:
            try:
                job = self.get(identifier, private=True)
                LOGGER.debug("Download %s: %s phase", short_id(identifier), job["details"]["phase"])
                if self._cancelled(identifier):
                    raise InterruptedError
                with self.http.interruptible(identifier):
                    {
                        "inspect": self._inspect,
                        "submit": self._submit,
                        "transfer": self._transfer,
                        "import": self._import,
                        "recover": self._recover,
                    }[job["details"]["phase"]](job)
                if self.get(identifier, private=True)["state"] != "done" and self._cancelled(identifier):
                    raise InterruptedError
            except (InterruptedError, ImportCancelled):
                self._finish(identifier, None, stop=self.stop_requests.get(identifier))
            except SourceError as error:
                self._finish(identifier, error, stop=self.stop_requests.get(identifier))
            except InvalidArchive as error:
                self._invalid_archive(identifier, error)
            except Exception as error:
                code = local_error(error)
                if code == "download_local_error":
                    LOGGER.exception("Download %s failed", identifier)
                elif code != "database_busy":
                    LOGGER.warning("Download %s stopped: %s (%s)", short_id(identifier), code, error)
                busy_failure = code == "database_busy"
                self._finish(identifier, SourceError(code), stop=self.stop_requests.get(identifier))
        except sqlite3.Error as error:
            if not busy(error):
                raise
            busy_failure = True
        finally:
            self._release_worker(identifier, busy_failure=busy_failure)
        return True

    def _release_worker(self, identifier: str, *, busy_failure: bool) -> None:
        parked = self._park(identifier, busy_failure=busy_failure)
        with self.worker_lock:
            if not parked:
                # Keep ownership until the database lets us settle; another step retries this.
                self.pending_parks[identifier] = busy_failure
                return
            self.interrupted.discard(identifier)
            self.stop_requests.pop(identifier, None)
            self.working.pop(identifier, None)
            self.meta_working.discard(identifier)
        with self.progress_lock:
            self.progress.pop(identifier, None)

    def _park(self, identifier: str, *, busy_failure: bool = False):
        """Leave no job in a worker-owned state once the worker is done with it.

        Normally the phase or `_finish` has already settled the job. If `_finish` itself raised,
        the job would otherwise stay inspecting/pausing/cancelling with no worker on it, and pause
        or cancel would wait forever. A job a busy database stopped is queued again (unless its
        archive request may have been charged); the parking itself retries a busy database.
        """
        for attempt in range(PARK_TRIES):
            try:
                job = self.get(identifier, private=True)
                if job["state"] == "pausing":
                    self._update(identifier, state="paused")
                elif job["state"] == "cancelling":
                    self._update(identifier, state="cancelled")
                    self._cleanup(identifier, {"cancelled"})
                elif job["state"] in ACTIVE:
                    if _uncertain(job["details"]):
                        job["details"]["phase"] = "recover"
                        self._update(
                            identifier, state="queued", details=job["details"], retry_at=int(time.time()) + 15
                        )
                    elif busy_failure:
                        self._update(
                            identifier,
                            state="queued",
                            error="database_busy",
                            retry_at=int(time.time()) + BUSY_RETRY,
                        )
                    else:
                        self._update(identifier, state="error", error="download_local_error")
                return True
            except NotFound:
                # A settled record may be cleared while its failed cleanup is pending.
                return True
            except Exception as error:
                if busy(error) and attempt + 1 < PARK_TRIES and not self.stopping.wait(BUSY_RETRY):
                    continue
                LOGGER.exception("Download %s could not be parked", identifier)
                return False

    def start(self):
        self.lock_file = try_lock(
            self.library.path.with_name(self.library.path.name + ".downloads.lock"),
            "Another download worker is using this library",
        )
        # Only the lock holder may recover imports and drop upload spools: a second
        # server on the same library must not touch the running one's work.
        startup(self.files)
        self.checks.recover()
        self._resume_interrupted()

        def run(wake: threading.Event, work: Callable[[], bool]):
            background_thread(WORKER_BUSY_TIMEOUT)
            set_thread_cancel(self.stopping.is_set)
            while not self.stopping.is_set():
                try:
                    if work():
                        continue
                except ImportCancelled:
                    if self.stopping.is_set():
                        return
                    LOGGER.exception("Download worker cancelled unexpectedly")
                except Exception as error:
                    if busy(error):
                        LOGGER.warning("Download worker waits: the library database is busy")
                    else:
                        LOGGER.exception("Download worker failed")
                wake.wait(1)
                wake.clear()

        def worker(name: str, work: Callable[[], bool]) -> threading.Thread:
            return threading.Thread(target=run, args=(self.wake.event(), work), name=name, daemon=True)

        def budget():
            background_thread(WORKER_BUSY_TIMEOUT)
            while not self.stopping.is_set():
                self.http.save_wake.wait(SAVE_SECONDS)
                self.http.save_wake.clear()
                if not self.stopping.is_set():
                    self.http.save_if_dirty()

        self.threads = [
            threading.Thread(target=budget, name="mangacouch-budget", daemon=True),
            *(worker(f"mangacouch-download-{n}", lambda: self.step("transfer")) for n in range(WORKERS)),
            *(worker(f"mangacouch-meta-{n}", lambda: self.step("meta")) for n in range(META_WORKERS)),
            worker("mangacouch-source-checks", lambda: self.checks.step(self.stopping.is_set)),
        ]
        for thread in self.threads:
            thread.start()

    def _resume_interrupted(self) -> None:
        """Settle the jobs a stopped server left behind: queued and running jobs continue in their
        phase (a transfer resumes where it stopped); an E(x)Hentai archive request that may have
        been sent enters automatic recovery; pauses and cancellations that
        were under way complete. Retry delays that have passed are cleared."""
        with self.library.transaction() as db:
            db.execute(
                "UPDATE download_job SET details_json=json_set(details_json,'$.phase','recover'),"
                "state='queued',retry_at=?,revision=revision+1 WHERE "
                "state IN ('queued','inspecting','submitting','downloading','importing') AND "
                "(json_extract(details_json,'$.submission_uncertain')=1 OR phase='uncertain')",
                (int(time.time()) + 15,),
            )
            db.execute(
                "UPDATE download_job SET state=CASE WHEN state='cancelling' THEN 'cancelled' "
                "WHEN state='pausing' THEN 'paused' ELSE 'queued' END,revision=revision+1 "
                "WHERE state IN ('inspecting','submitting','downloading','importing','cancelling','pausing')"
            )
            db.execute("UPDATE download_job SET retry_at=0 WHERE retry_at>0 AND retry_at<=unixepoch()")
            cancelled = [
                row[0]
                for row in db.execute("SELECT id FROM download_job WHERE state IN ('done','cancelled')")
            ]
        for identifier in cancelled:
            path = self.directory / (identifier + ".zip")
            if path.exists() or path.with_name(path.name + ".mangacouch.json").exists():
                self._cleanup(identifier, TERMINAL)

    def close(self):
        """Stop the workers, then release the HTTP client and the worker lock even if one of them
        does not stop in time (TimeoutError)."""
        self.stopping.set()
        self.http.interrupt()
        self.http.save_wake.set()
        self.wake.set()
        started = time.monotonic()
        deadline = started + 30
        try:
            for thread in self.threads:
                with self.worker_lock:
                    if self.storing_links:
                        deadline = started + 60
                thread.join(timeout=max(0, deadline - time.monotonic()))
            with self.worker_lock:
                pending_links = bool(self.storing_links)
            if pending_links:
                for thread in self.threads:
                    thread.join(timeout=max(0, started + 60 - time.monotonic()))
            if any(thread.is_alive() for thread in self.threads):
                raise TimeoutError("Download worker did not stop within the request timeout")
        finally:
            self.http.close()
            if self.lock_file:
                self.lock_file.close()
