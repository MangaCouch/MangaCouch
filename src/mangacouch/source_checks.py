from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext

from .database import RevisionConflict, busy
from .errors import InvalidInput, NotFound
from .files import RootUnavailable
from .limits import ImportCancelled
from .logs import short_id
from .models import EH_SITES_SQL, Source, SourceNotes, canonical_json
from .online import FAILED_TASK
from .source_http import SourceError, gallery_url
from .source_limits import BACKGROUND_RESERVE, MAX_WAIT

REFRESH_AFTER = 90 * 86400
"""A confirmed source is checked again for newer versions and metadata after this long."""
SETTLE_WAIT = 5.0
BATCH = 25
"""Checks taken per step: one EH gdata request covers that many galleries."""
RETRY_BASE = 60
RETRY_MAX = 3600
"""A check that meets a passing failure (`transient`) is retried after RETRY_BASE seconds, four
times longer each time up to RETRY_MAX; failures keep retrying."""
MAX_FAILURE_REASONS = 7
CANCEL_POLL = 1.0
"""Seconds between reads of a running check's state while its requests wait for a source."""
ATTENTION = ("needs_review",)
LOGGER = logging.getLogger(__name__)


def local_transient(error: Exception) -> bool:
    return busy(error) or isinstance(error, (RootUnavailable, RevisionConflict))


def transient(error: Exception) -> bool:
    """A failure that goes away by itself: no connection, or a server error with a retry time."""
    return (
        local_transient(error)
        or isinstance(error, SourceError)
        and (
            error.code == "source_network"
            or error.code == "archive_preparing"
            or (error.code == "source_unavailable" and ((error.status or 0) >= 500 or error.retry_after > 0))
        )
    )


def retry_delay(attempt: int) -> int:
    return min(RETRY_MAX, RETRY_BASE * 4 ** min(6, max(0, attempt - 1)))


def enqueue_check(db, book_id: str) -> None:
    source = db.execute(
        "SELECT json_extract(metadata_json,'$.source') FROM book WHERE id=?", (book_id,)
    ).fetchone()[0]
    error = None if source else "source_absent"
    db.execute(
        "INSERT OR IGNORE INTO source_check(book_id,state,error_code,reasons_json,updated_at) "
        "VALUES(?,?,?,?,?)",
        (
            book_id,
            "needs_review" if error else "queued",
            error,
            canonical_json([error] if error else []),
            int(time.time()),
        ),
    )


class SourceChecks:
    """Background source checks, plus the source work a rate limit put off (source_task).

    `sync` writes a book's sidecar after a refresh saved its metadata, checked or deferred, as
    the refresh endpoint does (SidecarSync.publish: it never fails the saved refresh).
    """

    def __init__(self, online, sync: Callable[[str], object] | None = None):
        self.online, self.library, self.sync = online, online.library, sync
        self.pending_settlements: dict[tuple, str | None] = {}

    def get(self, book_id: str) -> dict | None:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM source_check WHERE book_id=?", (book_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value["reasons"] = json.loads(value.pop("reasons_json"))
        return value

    def recover(self) -> None:
        """Checks a restart interrupted run again; queued ones (also those waiting out a site's
        halt or a retry time) stay queued."""
        with self.library.transaction() as db:
            result = db.execute(
                "UPDATE source_check SET state='queued',generation=generation+1,error_code=NULL,"
                "reasons_json='[]',retry_at=0 WHERE state='running'"
            )
        if result.rowcount:
            LOGGER.info("Source checks: %d interrupted checks queued again", result.rowcount)

    def attention(self, page: int, limit: int) -> tuple[list[str], int]:
        """Books whose check needs review, latest first, and their count."""
        if not 1 <= page <= 1_000_000 or not 1 <= limit <= 100:
            raise InvalidInput("Invalid source check page")
        with self.library.connection() as db:
            total = db.execute("SELECT count(*) FROM source_check WHERE state IN (?)", ATTENTION).fetchone()[
                0
            ]
            rows = db.execute(
                "SELECT book_id FROM source_check WHERE state IN (?) "
                "ORDER BY updated_at DESC,book_id LIMIT ? OFFSET ?",
                (*ATTENTION, limit, (page - 1) * limit),
            ).fetchall()
        return [row[0] for row in rows], total

    def schedule_refresh(self) -> int:
        """Refresh the library over ninety days, with at least 25 checks per hour."""
        now = int(time.time())
        with self.library.transaction() as db:
            limit = max(25, db.execute("SELECT count(*) FROM book").fetchone()[0] // (90 * 24))
            result = db.execute(
                "UPDATE source_check SET state='queued',generation=generation+1,error_code=NULL,"
                "reasons_json='[]',retry_at=0,updated_at=? WHERE book_id IN (SELECT c.book_id "
                "FROM source_check c JOIN book b ON b.id=c.book_id "
                "WHERE c.state IN ('done','needs_review') AND c.updated_at<? AND "
                "NOT EXISTS (SELECT 1 FROM source_task t WHERE t.book_id=c.book_id AND kind='metadata') AND "
                f"json_extract(b.metadata_json,'$.source.site') IN {EH_SITES_SQL} "
                "ORDER BY c.updated_at,c.book_id LIMIT ?)",
                (now, now - REFRESH_AFTER, limit),
            )
        if result.rowcount:
            LOGGER.info("Source checks: %d books due for a metadata refresh", result.rowcount)
        return result.rowcount

    def retry_failed(self) -> int:
        with self.library.transaction() as db:
            count = db.execute(
                "UPDATE source_check SET state='queued',generation=generation+1,error_code=NULL,"
                "reasons_json='[]',retry_at=0,updated_at=unixepoch() WHERE state='needs_review' "
                "AND error_code IS NOT NULL AND (book_id IN (SELECT id FROM book "
                f"WHERE json_extract(metadata_json,'$.source.site') IN {EH_SITES_SQL}) OR book_id IN "
                "(SELECT book_id FROM source_task WHERE kind='metadata' AND retry_at=?))",
                (FAILED_TASK,),
            ).rowcount
            db.execute(
                "UPDATE source_task SET retry_at=0 WHERE kind='metadata' AND retry_at=?",
                (FAILED_TASK,),
            )
            return count

    def action(self, book_id: str, action: str, generation: int) -> dict:
        if action != "retry":
            raise InvalidInput("Unsupported check action")
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM source_check WHERE book_id=?", (book_id,)).fetchone()
            if row is None:
                raise NotFound("Source check not found")
            if row["generation"] != generation:
                raise RevisionConflict("Source check changed")
            if action == "retry":
                pending = db.execute(
                    "SELECT source_url FROM source_task WHERE book_id=? AND kind='metadata' AND retry_at=?",
                    (book_id, FAILED_TASK),
                ).fetchone()
                source = (
                    gallery_url(pending[0]).model_dump_json()
                    if pending
                    else db.execute(
                        "SELECT json_extract(metadata_json,'$.source') FROM book WHERE id=?", (book_id,)
                    ).fetchone()[0]
                )
                if pending:
                    db.execute(
                        "UPDATE source_task SET retry_at=0 WHERE book_id=? "
                        "AND kind='metadata' AND retry_at=?",
                        (book_id, FAILED_TASK),
                    )
                if not source:
                    raise InvalidInput("Set an explicit source before retrying", code="source_absent")
            db.execute(
                "UPDATE source_check SET state=?,generation=generation+1,error_code=NULL,"
                "reasons_json='[]',retry_at=0,updated_at=? WHERE book_id=?",
                ("queued", int(time.time()), book_id),
            )
        return self.get(book_id) or {}

    def step(self, stopping=lambda: False) -> bool:
        """Runs one batch of queued checks; True when there was something to do.

        The whole batch is marked running first, then the EH galleries' metadata is fetched
        with one API request before each book is refreshed on its own (comments only with the
        background_comments setting). Checks are background work: their requests yield each
        source's slots to downloads.
        """
        if stopping() or self.online.sources.http.blocked:
            return False
        try:
            self.online.files.root.check()
        except RootUnavailable:
            return False
        until = max(self.online.sources.http.cooldowns().values(), default=0)
        if until > time.time():
            self._delay(until, manual=True)
            return False
        budget = self.online.sources.http.budget()["ehentai"]
        wait = (budget["level"] - (1 - BACKGROUND_RESERVE) * budget["capacity"]) / budget["leak_per_min"] * 60
        if wait > MAX_WAIT:
            self._delay(math.ceil(time.time() + wait), manual=False)
        if self._deferred(stopping):
            return True
        if wait > MAX_WAIT:
            return False
        with self.online.sources.http.background():
            return self._step(stopping)

    def _delay(self, until: int, *, manual: bool) -> None:
        """Hold queued checks and tasks until `until`. The worker asks every second while a halt
        lasts: only a check or task not yet held takes the write lock."""
        with self.library.connection() as db:
            due = db.execute(
                "SELECT EXISTS(SELECT 1 FROM source_check WHERE state='queued' AND retry_at<?) "
                "OR EXISTS(SELECT 1 FROM source_task WHERE retry_at<? AND (manual=0 OR ?))",
                (until, until, manual),
            ).fetchone()[0]
        if not due:
            return
        with self.library.transaction() as db:
            db.execute(
                "UPDATE source_check SET retry_at=?,error_code='source_rate_limited' "
                "WHERE state='queued' AND retry_at<?",
                (until, until),
            )
            db.execute(
                "UPDATE source_task SET retry_at=? WHERE retry_at<? AND (manual=0 OR ?)",
                (until, until, manual),
            )

    def _deferred(self, stopping) -> bool:
        """Run one due task a rate limit put off; True when there was one.

        Work asked for by hand goes first and is not held to the background share. Another rate
        limit puts the task off again, and so does a passing failure (`transient`), with a
        capped retry delay. Any other failure ends it: a refresh asked for by hand then leaves
        its book needing review with the error, as a failed check does, and a comment fetch
        records the error with the source's notes for the comments section.
        """
        if self.pending_settlements:
            for key, error in list(self.pending_settlements.items()):
                self._settle(key, error)
            return True
        now = int(time.time())
        with self.library.connection() as db:
            row = db.execute(
                "SELECT * FROM source_task WHERE retry_at<=? "
                "ORDER BY manual DESC,retry_at,created_at LIMIT 1",
                (now,),
            ).fetchone()
        if row is None:
            return False
        book_id, url, kind = row["book_id"], row["source_url"], row["kind"]
        key = (book_id, url, kind, row["created_at"])
        http = self.online.sources.http
        try:
            source = gallery_url(url)
        except ValueError:
            self._settle(key, None)
            LOGGER.info("Retired invalid source task for %s", short_id(book_id))
            return True
        try:
            with nullcontext() if row["manual"] else http.background():
                if kind == "metadata":
                    book = self.library.get_book(book_id)
                    self.online.refresh(
                        book_id, source, book.metadata_revision, cancelled=stopping, link_source=True
                    )
                    if self.sync is not None:
                        self.sync(book_id)
                else:
                    self.online.refresh_comments(book_id, source, cancelled=stopping)
        except InterruptedError:
            return False  # stopping: the task stays for the next start
        except (RevisionConflict, RootUnavailable, sqlite3.Error) as error:
            if not local_transient(error):
                raise
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE source_task SET retry_at=? WHERE book_id=? AND source_url=? "
                    "AND kind=? AND created_at=?",
                    (now + 60, *key),
                )
            return True
        except SourceError as error:
            now = int(time.time())
            if error.code == "source_rate_limited":
                # The task counts its passing failures from here (created_at).
                wait, created = error.retry_after or 60, now
            elif transient(error):
                wait = min(RETRY_MAX, max(RETRY_BASE, error.retry_after, now - row["created_at"]))
                created = row["created_at"]
            else:
                self._settle(key, error.code)
                return True
            with self.library.transaction() as db:
                db.execute(
                    "UPDATE source_task SET retry_at=?,created_at=? WHERE book_id=? AND source_url=? "
                    "AND kind=? AND created_at=?",
                    (now + wait, created, *key),
                )
            LOGGER.info("Deferred %s of %s waits %d s more: %s", kind, short_id(book_id), wait, error.code)
            return True
        except Exception as error:
            if not isinstance(error, (NotFound, InvalidInput, ValueError)):
                LOGGER.exception("Deferred %s of %s failed", kind, short_id(book_id))
            self._settle(key, getattr(error, "code", None) or "source_check_failed")
            return True
        self._settle(key, None)
        LOGGER.info("Deferred %s of %s done", kind, short_id(book_id))
        return True

    def _settle(self, key: tuple, error: str | None) -> None:
        try:
            self._settle_task(key, error)
        except sqlite3.Error as failure:
            if not busy(failure):
                raise
            self.pending_settlements[key] = error
            LOGGER.warning("Deferred source task waits for database settlement")
        else:
            self.pending_settlements.pop(key, None)

    def _settle_task(self, key: tuple, error: str | None) -> None:
        """Drop a finished task (unless it was queued again meanwhile); a failed refresh asked
        for by hand leaves its book needing review, a failed comment fetch stores its error as
        `comments_failure` in the source's notes until comments are fetched again."""
        book_id, url, kind, _ = key
        with self.library.transaction() as db:
            if error and kind == "metadata":
                gone = db.execute(
                    "UPDATE source_task SET retry_at=? WHERE book_id=? AND source_url=? "
                    "AND kind=? AND created_at=?",
                    (FAILED_TASK, *key),
                ).rowcount
            else:
                gone = db.execute(
                    "DELETE FROM source_task WHERE book_id=? AND source_url=? AND kind=? AND created_at=?",
                    key,
                ).rowcount
            exists = db.execute("SELECT 1 FROM book WHERE id=?", (book_id,)).fetchone()
            if not error and gone and kind == "metadata":
                db.execute(
                    "UPDATE download_job SET details_json=json_set(details_json,'$.warnings',json("
                    "(SELECT json_group_array(value) FROM json_each(download_job.details_json,'$.warnings') "
                    "WHERE value<>'source_metadata_not_saved'))),revision=revision+1 "
                    "WHERE book_id=? AND state='done' AND json_type(details_json,'$.warnings')='array'",
                    (book_id,),
                )
            if error and gone and kind == "metadata" and exists:
                db.execute(
                    "INSERT INTO source_check(book_id,state,error_code,reasons_json,updated_at) "
                    "VALUES(?,'needs_review',?,?,?) ON CONFLICT(book_id) DO UPDATE SET "
                    "state='needs_review',generation=generation+1,error_code=excluded.error_code,"
                    "reasons_json=excluded.reasons_json,retry_at=0,updated_at=excluded.updated_at",
                    (book_id, error, canonical_json([error]), int(time.time())),
                )
            if error and gone and kind == "comments" and exists:
                failure = {"code": error, "at": int(time.time())}
                noted = db.execute(
                    "UPDATE source_note SET notes_json=json_set(notes_json,'$.comments_failure',json(?)) "
                    "WHERE book_id=? AND source_url=?",
                    (canonical_json(failure), book_id, gallery_url(url).key),
                ).rowcount
                if not noted:
                    # Nothing fetched from this source yet: notes that were never fetched hold the failure.
                    notes = SourceNotes(source=gallery_url(url), fetched_at=None).model_dump(mode="json")
                    db.execute(
                        "INSERT INTO source_note(book_id,source_url,notes_json) VALUES(?,?,?)",
                        (
                            book_id,
                            gallery_url(url).key,
                            canonical_json({**notes, "comments_failure": failure}),
                        ),
                    )
        if error:
            LOGGER.warning("Deferred %s of %s stopped: %s", kind, short_id(book_id), error)

    def _step(self, stopping) -> bool:
        try:
            self.online.files.root.check()
        except RootUnavailable:
            return False
        with self.library.transaction() as db:
            db.execute("UPDATE source_check SET state='queued' WHERE state='running'")
            rows = db.execute(
                "SELECT * FROM source_check WHERE state='queued' AND retry_at<=? "
                "ORDER BY updated_at,book_id LIMIT ?",
                (int(time.time()), BATCH),
            ).fetchall()
            if not rows:
                return False
            # A check queued again after passing failures keeps one reason per failure.
            taken = [
                (
                    row["book_id"],
                    row["generation"],
                    len(json.loads(row["reasons_json"])) if row["error_code"] else 0,
                )
                for row in rows
            ]
            db.executemany(
                "UPDATE source_check SET state='running' WHERE book_id=?",
                [(book_id,) for book_id, _, _ in taken],
            )
        LOGGER.info("Source checks: checking %d books", len(taken))
        prefetched = []
        try:
            try:
                if len(taken) > 1:
                    prefetched = self._prefetch([book_id for book_id, _, _ in taken], stopping)
            except Exception:
                LOGGER.exception("Source check batch prefetch failed")
            for identifier, generation, failures in taken:
                if stopping():
                    break
                try:
                    self._run(identifier, generation, stopping, failures)
                except ImportCancelled:
                    if stopping():
                        break
                    raise
                except Exception as error:
                    if busy(error):
                        LOGGER.warning("Source check %s waits: database busy", short_id(identifier))
                    else:
                        LOGGER.exception("Source check %s could not settle", short_id(identifier))
        finally:
            self.online.sources.forget_prefetched(prefetched)

            def requeue(db):
                db.executemany(
                    "UPDATE source_check SET state='queued',retry_at=? "
                    "WHERE book_id=? AND generation=? AND state='running'",
                    [(int(time.time()) + 30, identifier, generation) for identifier, generation, _ in taken],
                )

            self._settle_with_retry(requeue, stopping)
        return True

    def _settle_with_retry(self, call, stopping) -> None:
        for attempt in range(5):
            try:
                with self.library.transaction() as db:
                    call(db)
                return
            except sqlite3.Error as error:
                if not busy(error):
                    raise
                if attempt == 4 or stopping():
                    LOGGER.warning("Source checks could not settle: database busy; retry next pass")
                    return
                deadline = time.monotonic() + SETTLE_WAIT
                wake = threading.Event()
                while time.monotonic() < deadline and not stopping():
                    wake.wait(min(0.1, max(0, deadline - time.monotonic())))

    def _prefetch(self, identifiers: list[str], stopping) -> list[Source]:
        """One gdata call for the batch's E(x)Hentai galleries, which each book's `preferred`
        fetch then takes from (Sources.prefetch picks the API host by site and credentials).
        Returns the galleries asked for, whose unused entries the batch drops when it ends."""
        sources = []
        with self.library.connection() as db:
            for (value,) in db.execute(
                f"SELECT json_extract(metadata_json,'$.source') FROM book WHERE id IN "
                f"({','.join('?' * len(identifiers))})",
                identifiers,
            ):
                source = Source.model_validate_json(value) if value else None
                if source:
                    sources.append(source)
        if sources:
            try:
                self.online.sources.prefetch(sources, cancelled=stopping)
            except SourceError as error:
                # Each check then fetches on its own and records the failure for its book.
                LOGGER.info("Batch metadata fetch skipped: %s", error.code)
        return sources

    def _run(self, identifier: str, generation: int, stopping, failures: int = 0) -> None:
        """Check one book; `failures` counts the passing failures it met in a row before."""
        read_at, changed = -CANCEL_POLL, False

        def cancelled():
            # Requests waiting for the source ask often: read the check at most every CANCEL_POLL.
            nonlocal read_at, changed
            if stopping():
                return True
            if not changed and time.monotonic() - read_at >= CANCEL_POLL:
                read_at = time.monotonic()
                current = self.get(identifier)
                changed = current is None or (current["state"], current["generation"]) != (
                    "running",
                    generation,
                )
            return changed

        if cancelled():
            return  # Paused or retried while waiting in this batch: the newer state stands.
        try:
            book = self.library.get_book(identifier)
            if book.metadata.source is None:
                raise SourceError("source_absent")
            settings = self.online.sources.http.settings
            self.online.refresh(
                identifier,
                book.metadata.source,
                book.metadata_revision,
                cancelled=cancelled,
                check_generation=generation,
                comments=settings.comments and settings.background_comments,
            )
            if self.sync is not None:
                self.sync(identifier)
            LOGGER.info(
                "Source check %s done: %s",
                short_id(identifier),
                book.metadata.title or book.metadata.original_title or book.metadata.source.url,
            )
        except Exception as error:
            if isinstance(error, ImportCancelled) and stopping():
                raise
            if not local_transient(error) and not isinstance(error, (SourceError, InterruptedError)):
                LOGGER.exception("Source check %s failed", identifier)
            code = (
                error.code
                if isinstance(error, SourceError)
                else "metadata_changed"
                if isinstance(error, RevisionConflict)
                else "interrupted"
                if isinstance(error, InterruptedError)
                else "source_check_failed"
            )
            reasons = [code]
            if isinstance(error, SourceError) and error.code == "source_rate_limited":
                # The site is cooling down: wait, then continue without user action.
                state, retry_at, reasons = "queued", int(time.time()) + error.retry_after, []
                LOGGER.info(
                    "Source check %s deferred %d s: rate limited", short_id(identifier), error.retry_after
                )
            elif code == "interrupted":
                # Stopping (or a retry that already replaced this run): run it again later.
                state, retry_at, code, reasons = "queued", 0, None, []
            elif local_transient(error):
                state, retry_at, reasons = (
                    "queued",
                    int(time.time()) + 60,
                    [code] * min(MAX_FAILURE_REASONS, failures),
                )
            elif transient(error):
                wait = max(retry_delay(failures + 1), getattr(error, "retry_after", 0))
                state, retry_at, reasons = (
                    "queued",
                    int(time.time()) + wait,
                    [code] * min(MAX_FAILURE_REASONS, failures + 1),
                )
                LOGGER.info("Source check %s retries in %d s: %s", short_id(identifier), wait, code)
            else:
                state, retry_at = "needs_review", 0
                LOGGER.warning("Source check %s needs review: %s", short_id(identifier), code)

            def settle(db):
                db.execute(
                    "UPDATE source_check SET state=?,error_code=?,reasons_json=?,retry_at=?,updated_at=? "
                    "WHERE book_id=? AND generation=? AND state='running'",
                    (
                        state,
                        code,
                        canonical_json(reasons),
                        retry_at,
                        int(time.time()),
                        identifier,
                        generation,
                    ),
                )

            self._settle_with_retry(settle, stopping)
