"""Download queue: gallery URL -> inspect -> (choose/approve) -> submit -> transfer -> import.

States (download_job.state) and who moves them:

    queued            waiting for a worker (retry_at may hold it back after a transient error)
    inspecting ...    a worker runs the job's phase (inspect, submit, transfer, import, recover)
    needs_choice      nHentai URL searched on EH; the user picks a candidate, the nH gallery or skips
    needs_approval    price known; the user approves the archive type
    paused/error      stopped; the user resumes (error = permanent failure or retries exhausted)
    verification_required  credentials or a challenge need the user
    needs_attention   a paid EH archive request may or may not have been charged; only GET-based
                      recovery is allowed so the POST is never repeated
    cancelling/pausing     a worker owns the job and stops at its next check
    done/cancelled/skipped terminal

Transient source errors (network, 5xx, archive still preparing, rate limits) re-queue the job
with retry_at and count `attempts`; rate limits wait for the site's cooldown without using an
attempt. nHentai links cost nothing, so its submissions never enter needs_attention.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from dataclasses import replace
from urllib.parse import urlsplit

from .archives import ImportCancelled, zip_image_bytes
from .editions import Editions
from .errors import InvalidInput, NotFound, RevisionConflict
from .files import CHUNK_SIZE, RootUnavailable, hash_file, require_free_space
from .ingest import FileLibrary
from .models import Metadata, NativeSidecar, Source, canonical_json
from .online import OnlineMetadata
from .sidecars import SidecarSync
from .source_adapters import REUSABLE_ARCHIVE_TTL, Sources, archive_remote
from .source_checks import SourceChecks
from .source_config import SourceSettings, SourceVault, try_lock
from .source_http import SourceError, SourceHTTP, gallery_url

LOGGER = logging.getLogger(__name__)
ACTIVE = {"inspecting", "submitting", "downloading", "importing"}
TERMINAL = {"done", "cancelled", "skipped"}
WORKERS = 4
MAX_ATTEMPTS = 5
BATCH_LIMIT = 100
COMPLETED_WINDOW = 86400
"""Finished downloads count toward the task summary for one day."""
COVER_CACHE_BYTES = 16 * 1024**2
CREDENTIAL_ERRORS = {"verification_required", "source_login_required", "nh_key_required"}
REFUSED_BEFORE_CHARGE = {
    "source_login_required",
    "verification_required",
    "source_rate_limited",
    "insufficient_gp",
}


def retryable(error: SourceError) -> bool:
    """Errors that resolve themselves: the job is re-queued instead of waiting for the user."""
    return error.code in {"source_network", "archive_preparing", "source_rate_limited"} or (
        error.code == "source_unavailable" and error.retry_after > 0
    )


def startup(files: FileLibrary) -> None:
    """Library housekeeping before serving: finish interrupted imports, drop stale upload spools."""
    try:
        files.recover()
    except (RootUnavailable, RevisionConflict):
        LOGGER.warning("Import recovery deferred until library storage is ready")
    for stale in files.library.path.parent.glob(f"mangacouch-upload-{files.library.path.name}-*"):
        if stale.is_dir() and not stale.is_symlink():
            shutil.rmtree(stale)


def _uncertain(details: dict) -> bool:
    return bool(details.get("submission_uncertain")) or details.get("phase") == "uncertain"


def _cover_type(payload: bytes) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return "image/webp"
    raise SourceError("source_cover_invalid")


class Downloads:
    def __init__(self, files: FileLibrary, *, http: SourceHTTP | None = None):
        self.files, self.library = files, files.library
        self.vault = SourceVault(self.library)
        self.settings, secrets, self.config_revision = self.vault.load()
        self.http = http or SourceHTTP(self.settings, secrets)
        self.sources = Sources(self.http)
        self.online = OnlineMetadata(files, self.sources)
        self.checks = SourceChecks(self.online)
        self.directory = self.library.path.parent / (".mangacouch-downloads-" + self.library.path.name)
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.threads: list[threading.Thread] = []
        self.lock_file = None
        self.working: set[str] = set()
        self.worker_lock = threading.Lock()
        self.configuration_lock = threading.Lock()
        self.progress_lock = threading.Lock()
        self.progress: dict[str, tuple[float, int, float]] = {}
        # Serializes a job's state changes with the archive cleanup that follows them.
        self.job_locks = [threading.Lock() for _ in range(32)]
        # (sha256, size, mtime_ns) of archives this process hashed while downloading.
        self.hashed: dict[str, tuple[str, int, int]] = {}
        self.cover_lock = threading.Lock()
        self.covers: OrderedDict[tuple[str, str], tuple[bytes, str]] = OrderedDict()

    def _job_lock(self, identifier: str) -> threading.Lock:
        return self.job_locks[
            int(hashlib.sha256(identifier.encode()).hexdigest()[:8], 16) % len(self.job_locks)
        ]

    def save_settings(self, settings: SourceSettings, changes: dict[str, str], expected_revision: int):
        changes = {key: value.strip() for key, value in changes.items()}
        self.vault.validate(changes)
        with self.configuration_lock:
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
            self.wake.set()
            return self.vault.public()

    def _read(self, db, identifier: str) -> dict:
        row = db.execute("SELECT * FROM download_job WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("Download not found", code="download_not_found")
        return {
            **dict(row),
            "source": json.loads(row["source_json"]),
            "details": json.loads(row["details_json"]),
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
        job["bytes_per_second"] = (
            sample[2] if sample and job["state"] == "downloading" and time.monotonic() - sample[0] < 4 else 0
        )
        for name in ("source_json", "details_json", "remote_secret", "rowid"):
            job.pop(name, None)
        details = job["details"]
        for observation in [details.get("original"), details.get("selected"), *details.get("candidates", [])]:
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

    def list(self, before: int | None = None, limit: int = 20, scope: str = "all") -> dict:
        condition = {
            "all": "1",
            "active": "state NOT IN ('done','cancelled','skipped')",
            "history": "state IN ('done','cancelled','skipped')",
        }[scope]
        with self.library.connection() as db:
            db.execute("BEGIN")
            # Comments and gdata of the inspected gallery are the bulk of a job; never parse them here.
            rows = db.execute(
                "SELECT rowid,id,input_url,source_json,state,revision,received,total,book_id,error_code,"
                "retry_at,attempts,created_at,updated_at,json_remove(details_json,'$.selected.comments',"
                "'$.selected.gdata','$.original.comments','$.original.gdata') AS details_json "
                f"FROM download_job WHERE {condition} AND rowid<? ORDER BY rowid DESC LIMIT ?",
                (before or 2**63 - 1, limit + 1),
            ).fetchall()
            total = db.execute(f"SELECT count(*) FROM download_job WHERE {condition}").fetchone()[0]
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
        return {
            "items": items,
            "before": rows[limit - 1]["rowid"] if len(rows) > limit else None,
            "total": total,
        }

    def summary(self) -> dict:
        """Current queue: unfinished jobs plus those completed within COMPLETED_WINDOW."""
        with self.library.connection() as db:
            counts = dict(
                db.execute(
                    "SELECT state,count(*) FROM download_job WHERE "
                    "state NOT IN ('done','cancelled','skipped') GROUP BY state "
                    "UNION ALL SELECT 'done',count(*) FROM download_job WHERE state='done' AND updated_at>=?",
                    (int(time.time()) - COMPLETED_WINDOW,),
                ).fetchall()
            )
            rows = db.execute(
                "SELECT id,state,input_url,received,total,retry_at,attempts,error_code,"
                "json_extract(details_json,'$.selected.metadata.title') AS title "
                "FROM download_job WHERE state NOT IN ('done','cancelled','skipped') "
                "ORDER BY CASE WHEN state IN ('downloading','importing','inspecting','submitting') "
                "THEN 0 ELSE 1 END,created_at LIMIT 8"
            ).fetchall()
        return {
            "running": sum(counts.get(state, 0) for state in ACTIVE),
            "waiting": counts.get("queued", 0),
            "completed": counts.get("done", 0),
            "total": sum(counts.values()),
            "items": [dict(row) for row in rows],
        }

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
        values = {"error_code": error, "updated_at": int(time.time()), **extra}
        if state is not None:
            values["state"] = state
        if details is not None:
            values["details_json"] = canonical_json(details)
        with self.library.transaction() as db:
            current = db.execute("SELECT state FROM download_job WHERE id=?", (identifier,)).fetchone()
            if (
                current
                and current[0] in {"cancelling", "pausing"}
                and values.get("state") not in {"done", "cancelled", "paused", "needs_attention"}
            ):
                values.pop("state", None)
            db.execute(
                (
                    "UPDATE download_job SET "
                    if state is None and details is None and set(extra) == {"received"}
                    else "UPDATE download_job SET revision=revision+1,"
                )
                + ",".join(key + "=?" for key in values)
                + " WHERE id=?",
                (*values.values(), identifier),
            )

    def create(self, url: str, entry: str = "find_eh") -> dict:
        try:
            source = gallery_url(url)
        except ValueError:
            raise InvalidInput(
                "Expected an EH, ExHentai or nHentai gallery URL", code="unsupported_source"
            ) from None
        if entry not in {"find_eh", "nh_direct"} or (source.site != "nhentai" and entry != "find_eh"):
            raise InvalidInput("Invalid source choice", code="invalid_entry")
        identifier, now = str(uuid.uuid4()), int(time.time())
        details = {"entry": entry, "phase": "inspect", "mode": self.settings.default_quality}
        while True:
            with self._job_lock(identifier), self.library.transaction() as db:
                existing = db.execute(
                    "SELECT id,state,revision,details_json FROM download_job WHERE input_url=?", (source.url,)
                ).fetchone()
                if existing and existing["id"] != identifier:
                    # Release the transaction before waiting for the existing job's cleanup lock.
                    identifier = existing["id"]
                    continue
                if existing:
                    if existing["state"] in TERMINAL or (
                        existing["state"] in {"needs_choice", "needs_approval"}
                        and json.loads(existing["details_json"]).get("entry") != entry
                    ):
                        db.execute(
                            "UPDATE download_job SET "
                            "state='queued',source_json=?,details_json=?,error_code=NULL,"
                            "remote_secret=NULL,received=0,total=NULL,retry_at=0,attempts=0,book_id=NULL,"
                            "revision=revision+1,updated_at=? WHERE id=?",
                            (source.model_dump_json(), canonical_json(details), now, identifier),
                        )
                else:
                    db.execute(
                        "INSERT INTO "
                        "download_job(id,input_url,source_json,state,details_json,created_at,updated_at) "
                        "VALUES(?,?,?,'queued',?,?,?)",
                        (identifier, source.url, source.model_dump_json(), canonical_json(details), now, now),
                    )
            break
        self.wake.set()
        return self.get(identifier)

    def create_many(self, urls: list[str], entry: str = "find_eh") -> list[dict]:
        """One job per distinct gallery; `entry` applies to nHentai URLs, EH URLs always search EH."""
        if not 1 <= len(urls) <= BATCH_LIMIT:
            raise InvalidInput(f"Expected 1 to {BATCH_LIMIT} URLs", code="invalid_batch")
        if entry not in {"find_eh", "nh_direct"}:
            raise InvalidInput("Invalid source choice", code="invalid_entry")
        jobs: dict[str, dict] = {}
        items = []
        for url in urls:
            try:
                source = gallery_url(url)
            except ValueError:
                parsed = urlsplit(url.strip())
                web = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
                items.append(
                    {"url": url, "job": None, "error": "unsupported_source" if web else "invalid_url"}
                )
                continue
            if source.url not in jobs:
                jobs[source.url] = self.create(source.url, entry if source.site == "nhentai" else "find_eh")
            items.append({"url": url, "job": jobs[source.url], "error": None})
        return items

    def action(
        self, identifier: str, action: str, revision: int, *, url: str | None = None, mode: str = "org"
    ) -> dict:
        with self._job_lock(identifier):
            state = self._act(identifier, action, revision, url=url, mode=mode)
            if state == "cancelled":
                self._remove_files(identifier)
        self.wake.set()
        return self.get(identifier)

    def _act(self, identifier: str, action: str, revision: int, *, url: str | None, mode: str) -> str:
        with self.library.transaction() as db:
            job = self._read(db, identifier)
            if job["revision"] != revision and action != "cancel":
                raise RevisionConflict("Download changed")
            state, details, source = job["state"], job["details"], Source.model_validate(job["source"])
            if action == "cancel":
                if state == "done":
                    raise InvalidInput("Completed download is already in the library", code="download_done")
                state = "cancelling" if state in ACTIVE | {"cancelling", "pausing"} else "cancelled"
                if state == "cancelled":
                    details.pop("archive_sha256", None)
            elif action == "search" and state in {"needs_choice", "done", "error", "cancelled", "skipped"}:
                source = gallery_url(job["input_url"])
                details["entry"], details["phase"] = "find_eh", "inspect"
                state = "queued"
            elif action == "pause" and state in ACTIVE | {"queued", "pausing"}:
                state = "pausing" if state in ACTIVE | {"pausing"} else "paused"
            elif action == "skip" and state == "needs_choice":
                state = "skipped"
            elif action == "choose" and state == "needs_choice" and url:
                try:
                    selected = gallery_url(url)
                except ValueError:
                    raise InvalidInput("Expected a gallery URL", code="unsupported_source") from None
                if selected.site == "nhentai" and selected != gallery_url(job["input_url"]):
                    raise InvalidInput("Only the submitted nHentai gallery is a direct fallback")
                source = selected
                details["entry"] = "nh_direct" if selected.site == "nhentai" else "find_eh"
                details["phase"] = "inspect"
                details.pop("candidates", None)
                details.pop("selected", None)
                state = "queued"
            elif action == "approve" and state == "needs_approval":
                if mode not in {"org", "res"} or (source.site == "nhentai" and mode != "org"):
                    raise InvalidInput("Invalid archive type", code="invalid_mode")
                cost = details["quote"].get("original_gp" if mode == "org" else "resample_gp")
                if cost is None:
                    raise InvalidInput("Archive price is unavailable", code="price_unavailable")
                details.update(mode=mode, approved_gp=cost, phase="submit")
                state = "queued"
            elif action in {"resume", "recover"} and state in {
                "paused",
                "error",
                "verification_required",
                "needs_attention",
                "cancelled",
                "skipped",
            }:
                cooling = self.http.cooldowns().get(source.site)
                if cooling:
                    raise SourceError("source_rate_limited", max(1, cooling - int(time.time())))
                if state == "needs_attention" or _uncertain(details):
                    # Only GET status recovery is possible here; never repeat a remote POST.
                    details["phase"] = "recover"
                elif job["remote_secret"]:
                    details["phase"] = "import" if details.get("archive_sha256") else "transfer"
                else:
                    details["phase"] = "inspect"
                state = "queued"
            else:
                raise InvalidInput("Action is not available in this state", code="action_unavailable")
            if action in {"search", "choose"}:
                for name in ("approved_gp", "quote", "archive_sha256", "submission_uncertain"):
                    details.pop(name, None)
                db.execute(
                    "UPDATE download_job SET remote_secret=NULL,received=0,total=NULL,book_id=NULL "
                    "WHERE id=?",
                    (identifier,),
                )
            db.execute(
                "UPDATE download_job SET state=?,source_json=?,details_json=?,error_code=NULL,retry_at=0,"
                "attempts=0,revision=revision+1,updated_at=? WHERE id=?",
                (state, source.model_dump_json(), canonical_json(details), int(time.time()), identifier),
            )
        return state

    def _cancelled(self, identifier) -> bool:
        if self.stopping.is_set():
            return True
        with self.library.connection() as db:
            row = db.execute("SELECT state FROM download_job WHERE id=?", (identifier,)).fetchone()
        return row is None or row[0] in {"cancelling", "pausing"}

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
        with self.cover_lock:
            for key in [key for key in self.covers if key[0] == identifier]:
                del self.covers[key]

    def _cleanup(self, identifier, states: set[str]):
        """Delete the job's archive if it is still in one of `states`; a resumed job keeps it."""
        with self._job_lock(identifier):
            with self.library.connection() as db:
                row = db.execute("SELECT state FROM download_job WHERE id=?", (identifier,)).fetchone()
            if row is not None and row[0] in states:
                self._remove_files(identifier)

    def cover(self, identifier: str, index: int) -> tuple[bytes, str]:
        """Cover of the original (-1) or of a search candidate, fetched once per job."""
        details = self.get(identifier, private=True)["details"]
        candidates = details.get("candidates", [])
        observation = (
            details.get("original")
            if index == -1
            else candidates[index]
            if 0 <= index < len(candidates)
            else None
        )
        if not observation or not observation.get("cover_url"):
            raise NotFound("Cover not found", code="cover_not_found")
        key = (identifier, observation["cover_url"])
        with self.cover_lock:
            if key in self.covers:
                self.covers.move_to_end(key)
                return self.covers[key]
        payload = self.http.read("GET", observation["cover_url"], limit=4 * 1024**2)
        value = (payload, _cover_type(payload))
        with self.cover_lock:
            self.covers[key] = value
            while sum(len(item[0]) for item in self.covers.values()) > COVER_CACHE_BYTES:
                self.covers.popitem(last=False)
        return value

    def _inspect(self, job):
        identifier, source, details = job["id"], Source.model_validate(job["source"]), job["details"]

        def check():
            return self._cancelled(identifier)

        self._update(identifier, state="inspecting")
        observation = self.sources.fetch(source, comments=self.settings.comments, cancelled=check)
        details["selected"] = observation
        if source.site == "nhentai" and details["entry"] == "find_eh":
            details["original"] = observation
            try:
                details["candidates"] = self.sources.search_eh(observation, cancelled=check)
            except SourceError as error:
                if retryable(error):
                    raise
                details.pop("candidates", None)
                self._update(identifier, state="needs_choice", details=details, error=error.code)
                return
            self._update(identifier, state="needs_choice", details=details)
            return
        quote, reusable = self.sources.quote(source, cancelled=check)
        if reusable:
            quote["original_gp"] = 0
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL))
            self._update(identifier, remote_secret=remote)
            details["reusable"] = True
        details["quote"] = quote
        mode = "org" if source.site == "nhentai" else self.settings.default_quality
        details["mode"] = mode
        cost = quote.get("original_gp" if mode == "org" else "resample_gp")
        if self.settings.auto_free and cost == 0:
            details.update(mode=mode, approved_gp=0, phase="submit")
            self._update(identifier, state="queued", details=details)
        else:
            self._update(identifier, state="needs_approval", details=details)

    def _submit(self, job):
        identifier, source, details = job["id"], Source.model_validate(job["source"]), job["details"]

        def check():
            return self._cancelled(identifier)

        if job["remote_secret"]:
            details["phase"] = "transfer"
            self._update(identifier, state="queued", details=details)
            return
        quote, reusable = self.sources.quote(source, cancelled=check)
        cost = quote.get("original_gp" if details["mode"] == "org" else "resample_gp")
        if not reusable and cost != details["approved_gp"]:
            details["quote"] = quote
            self._update(identifier, state="needs_approval", details=details, error="source_price_changed")
            return
        if check():
            raise InterruptedError
        if source.site == "nhentai":
            # nHentai issues archive links for free; a lost answer is simply requested again.
            self._update(identifier, state="submitting")
        else:
            # Durable boundary before the only potentially charging remote request.
            details["phase"] = "uncertain"
            details["submission_uncertain"] = True
            self._update(identifier, state="submitting", details=details)
        if reusable:
            remote = archive_remote(reusable, REUSABLE_ARCHIVE_TTL)
        else:
            remote = self.sources.submit(source, details["mode"], cancelled=check)
        details["phase"] = "transfer"
        details["submission_uncertain"] = False
        self._update(identifier, remote_secret=self.vault.encrypt(remote), details=details)
        if check():
            raise InterruptedError
        self._update(identifier, state="queued")

    def _recover(self, job):
        source, details = Source.model_validate(job["source"]), job["details"]
        details.pop("archive_sha256", None)
        if source.site == "nhentai":
            # Free link issuance: ask for a fresh link instead of guessing what happened.
            details.update(
                phase="submit" if "approved_gp" in details else "inspect", submission_uncertain=False
            )
            self._update(job["id"], state="queued", details=details, remote_secret=None)
            return
        _, reusable = self.sources.quote(source, cancelled=lambda: self._cancelled(job["id"]))
        if reusable:
            details.update(phase="transfer", submission_uncertain=False)
            remote = self.vault.encrypt(archive_remote(reusable, REUSABLE_ARCHIVE_TTL))
            self._update(job["id"], state="queued", details=details, remote_secret=remote)
        elif job["remote_secret"] and not details.get("submission_uncertain"):
            details["phase"] = "inspect"
            self._update(job["id"], state="queued", details=details, remote_secret=None)
        else:
            self._update(job["id"], state="needs_attention", error="remote_result_unknown")

    def _transfer(self, job):
        identifier, details = job["id"], job["details"]
        limit = self.settings.download_limit_bytes
        remote = self.vault.decrypt(job["remote_secret"])
        if remote.get("expires_at", 0) <= time.time():
            details["phase"] = "recover"
            self._update(identifier, state="needs_attention", details=details, error="source_link_expired")
            return

        def cancelled():
            return self._cancelled(identifier)

        path = self._path(identifier)
        if path.exists() and job["total"] is not None and path.stat().st_size == job["total"]:
            self._finish_transfer(identifier, details, hash_file(path, cancelled=cancelled), job["total"])
            return
        offset = path.stat().st_size if path.exists() and details.get("etag") else 0
        headers = {"Range": f"bytes={offset}-", "If-Range": details["etag"]} if offset else {}
        self._update(identifier, state="downloading")
        with self.http.stream("GET", remote["url"], headers=headers, cancelled=cancelled) as response:
            if "html" in response.headers.get("content-type", ""):
                raise SourceError("archive_preparing", 15)
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
            if total is not None and total > limit:
                raise SourceError("download_too_large")
            require_free_space(path.parent, max(0, total - offset) if total else 1024**2)
            etag = response.headers.get("etag")
            details["etag"] = etag if etag and not etag.startswith("W/") else None
            self._update(identifier, details=details, received=offset, total=total)
            self._progress(identifier, offset)
            digest = hashlib.sha256()
            if offset:
                with path.open("rb") as existing:
                    while chunk := existing.read(min(CHUNK_SIZE, offset - existing.tell())):
                        digest.update(chunk)
            received, published = offset, time.monotonic()
            with path.open("r+b" if offset else "wb") as output:
                output.seek(offset)
                output.truncate()
                for chunk in response.iter_bytes(CHUNK_SIZE):
                    if cancelled():
                        raise InterruptedError
                    received += len(chunk)
                    if received > limit or (total is not None and received > total):
                        raise SourceError("download_too_large")
                    require_free_space(path.parent, len(chunk))
                    output.write(chunk)
                    digest.update(chunk)
                    if time.monotonic() - published >= 0.5:
                        self._progress(identifier, received)
                        self._update(identifier, received=received)
                        published = time.monotonic()
                output.flush()
                os.fsync(output.fileno())
            if total is not None and received != total:
                raise SourceError("source_truncated")
        self._finish_transfer(identifier, details, digest.hexdigest(), received)

    def _finish_transfer(self, identifier: str, details: dict, sha256: str, received: int):
        stat = self._path(identifier).stat()
        self.hashed[identifier] = (sha256, stat.st_size, stat.st_mtime_ns)
        details["archive_sha256"] = sha256
        details["phase"] = "import"
        self._update(identifier, state="queued", details=details, received=received)

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
        if (
            job["total"] is None
            and details.get("mode") == "org"
            and metadata.source
            and metadata.source.site != "nhentai"
        ):
            if metadata.declared_byte_size is not None:
                if zip_image_bytes(path, self.files.limits) != metadata.declared_byte_size:
                    raise SourceError("archive_size_mismatch")
                details["size_verification"] = "images"
        elif job["total"] is not None:
            if path.stat().st_size != job["total"]:
                raise SourceError("source_truncated")
            details["size_verification"] = "archive"
        native = NativeSidecar(
            schema="mangacouch.native/1",
            content_sha256=details["archive_sha256"],
            revision=1,
            metadata=metadata,
        )
        path.with_name(path.name + ".mangacouch.json").write_bytes(native.canonical_bytes())
        incoming = FileLibrary(
            self.library,
            self.files.root.path,
            replace(self.files.limits, max_archive_bytes=self.settings.download_limit_bytes),
        )
        title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", metadata.title or "gallery")[:160]
        # The download is ours to delete, so the importer may hard-link it instead of copying.
        result = incoming.import_file(
            path,
            original_filename=title + ".cbz",
            cancelled=cancelled,
            consume=True,
            known_sha256=details["archive_sha256"],
        )
        self.online.apply(result.book.id, details["selected"], None, link_source=True)
        with suppress(OSError, ValueError):
            SidecarSync(self.files).sync_one(result.book.id)
        details["integrity_verified"] = True
        # A committed import wins over a cancellation arriving after publication.
        related = [item["id"] for item in Editions(self.library).candidates(result.book.id)["items"]]
        details.update(phase="done", duplicate=not result.created, previous_books=related)
        self._update(identifier, state="done", details=details, book_id=result.book.id)
        self._cleanup(identifier, {"done"})

    def _finish(self, identifier: str, error: SourceError | None):
        """Settle a job whose phase stopped early: cancelled, paused, retried or failed."""
        with self.library.connection() as db:
            current = self._read(db, identifier)
        details, code = current["details"], error.code if error else None
        if error and error.code in REFUSED_BEFORE_CHARGE and details.get("phase") == "uncertain":
            # EH refused the archive request outright, so nothing was charged.
            details.update(phase="inspect", submission_uncertain=False)
            self._update(identifier, details=details, error=code)
        retry_at, attempts = 0, current["attempts"]
        if _uncertain(details):
            state = "needs_attention"
        elif current["state"] == "cancelling":
            state = "cancelled"
        elif error is None:
            state = "paused"
        elif retryable(error) and (code == "source_rate_limited" or attempts < MAX_ATTEMPTS):
            state = "queued"
            if code == "source_rate_limited":
                retry_at = int(time.time()) + error.retry_after
            else:
                retry_at = int(time.time()) + min(1800, max(error.retry_after, 15 * 2**attempts))
                attempts += 1
        elif code in CREDENTIAL_ERRORS:
            state = "verification_required"
        else:
            state = "error"
            retry_at = int(time.time()) + error.retry_after
        self._update(identifier, state=state, error=code, retry_at=retry_at, attempts=attempts)
        if state == "cancelled":
            self._cleanup(identifier, {"cancelled"})
        elif code == "download_too_large":
            self._cleanup(identifier, {"error", "paused"})

    def step(self) -> bool:
        with self.worker_lock, self.library.transaction() as db:
            row = db.execute(
                "SELECT id FROM download_job WHERE state='queued' AND retry_at<=? "
                "AND id NOT IN (SELECT value FROM json_each(?)) ORDER BY created_at,id LIMIT 1",
                (int(time.time()), json.dumps(list(self.working))),
            ).fetchone()
            if row is None:
                return False
            identifier = row[0]
            self.working.add(identifier)
            db.execute(
                "UPDATE download_job SET state='inspecting',revision=revision+1 "
                "WHERE id=? AND state='queued'",
                (identifier,),
            )
        try:
            job = self.get(identifier, private=True)
            if self._cancelled(identifier):
                raise InterruptedError
            {
                "inspect": self._inspect,
                "submit": self._submit,
                "transfer": self._transfer,
                "import": self._import,
                "recover": self._recover,
            }[job["details"]["phase"]](job)
            if self.get(identifier, private=True)["state"] != "done" and self._cancelled(identifier):
                raise InterruptedError
            with self.library.transaction() as db:
                db.execute("UPDATE download_job SET attempts=0 WHERE id=? AND attempts>0", (identifier,))
        except (InterruptedError, ImportCancelled):
            self._finish(identifier, None)
        except SourceError as error:
            self._finish(identifier, error)
        except Exception:
            LOGGER.exception("Download %s failed", identifier)
            self._finish(identifier, SourceError("download_local_error"))
        finally:
            if self.get(identifier, private=True)["state"] == "pausing":
                self._update(identifier, state="paused")
            with self.worker_lock:
                self.working.discard(identifier)
            with self.progress_lock:
                self.progress.pop(identifier, None)
        return True

    def start(self):
        self.lock_file = try_lock(
            self.library.path.with_name(self.library.path.name + ".downloads.lock"),
            "Another download worker is using this library",
        )
        # Only the lock holder may recover imports and drop upload spools: a second
        # server on the same library must not touch the running one's work.
        startup(self.files)
        self.checks.recover()
        with self.library.transaction() as db:
            db.execute(
                "UPDATE download_job SET state=CASE WHEN state='submitting' OR "
                "json_extract(details_json,'$.phase')='uncertain' OR "
                "json_extract(details_json,'$.submission_uncertain')=1 THEN 'needs_attention' ELSE "
                "CASE WHEN state='cancelling' THEN 'cancelled' ELSE 'paused' END END,"
                "revision=revision+1 WHERE state IN "
                "('queued','inspecting','submitting','downloading','importing','cancelling','pausing')"
            )
            cancelled = [row[0] for row in db.execute("SELECT id FROM download_job WHERE state='cancelled'")]
        for identifier in cancelled:
            self._cleanup(identifier, {"cancelled"})

        def run(checks=False):
            while not self.stopping.is_set():
                try:
                    if self.checks.step(self.stopping.is_set) if checks else self.step():
                        continue
                except Exception:
                    LOGGER.exception("Download worker failed")
                self.wake.wait(1)
                self.wake.clear()

        self.threads = [
            threading.Thread(target=run, name=f"mangacouch-download-{n}", daemon=True) for n in range(WORKERS)
        ]
        self.threads.append(
            threading.Thread(target=run, args=(True,), name="mangacouch-source-checks", daemon=True)
        )
        for thread in self.threads:
            thread.start()

    def close(self):
        self.stopping.set()
        self.wake.set()
        deadline = time.monotonic() + 30
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
            if thread.is_alive():
                raise TimeoutError("Download worker did not stop within the request timeout")
        self.http.close()
        if self.lock_file:
            self.lock_file.close()
