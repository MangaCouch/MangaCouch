from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import socket
import sqlite3
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Literal
from urllib.parse import urljoin, urlsplit

import httpx
import socksio

from .database import Library
from .errors import InvalidInput
from .models import EH_SITES, Source
from .source_config import SourceSettings
from .source_limits import (
    EH_COSTS,
    MAX_WAIT,
    POLL,
    SAVE_REQUESTS,
    SAVE_SECONDS,
    Bucket,
    eh_kind,
)

NO_WARNING_COOKIE = "nw=1"
"""Sent with every gallery page request so a gallery flagged as offensive shows its page, not the
content warning. The `?nw=always` link only works through the cookie it sets, and response
cookies are never kept."""
CDN_CONCURRENCY = 4
"""Parallel requests per other host (thumbnail CDNs, archive servers); they spend no budget."""
SITE_HOSTS = {
    "e-hentai.org": "ehentai",
    "www.e-hentai.org": "ehentai",
    "g.e-hentai.org": "ehentai",
    "api.e-hentai.org": "ehentai",
    "forums.e-hentai.org": "ehentai",
    "exhentai.org": "exhentai",
    "www.exhentai.org": "exhentai",
}
"""API and HTML hosts: budgeted, halted and checked for ban pages as their site."""
RANGE_HEADERS = {"range", "if-range"}
GALLERY_HOSTS: dict[str, Literal["ehentai", "exhentai"]] = {
    "e-hentai.org": "ehentai",
    "www.e-hentai.org": "ehentai",
    "g.e-hentai.org": "ehentai",
    "exhentai.org": "exhentai",
    "www.exhentai.org": "exhentai",
}
"""Hosts whose /g/<gid>/<token>/ links name an E(x)Hentai gallery; stored links use the canonical host."""
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")
"""Benchmark range that TUN-mode proxies (Clash, Surge, ...) hand out as fake DNS answers."""
TOO_FAST_SECONDS = 600
"""Halt after E-Hentai's "You are opening pages too fast" warning, which precedes an IP ban.

Each repeat within STRIKE_WINDOW of the previous warning's halt doubles it, up to MAX_HALT."""
STRIKE_WINDOW = 6 * 3600
MAX_HALT = 86400
BAN_UNITS = {"day": 86400, "hour": 3600, "minute": 60, "second": 1}
HALT_REASONS = {
    "ban": "IP temporarily banned by E-Hentai (excessive request rate)",
    "too_fast": 'E-Hentai warned "opening pages too fast"',
    "http_429": "HTTP 429",
    "invalid_responses": "Repeated unrecognized short source responses",
}
INVALID_SECONDS = 600
"""Pause after three unrecognized short answers in a row: a fallback, not a rate limit."""
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Halt:
    """No request may reach a site group before `until` (epoch seconds)."""

    until: float
    reason: str
    strikes: int = 1


def source_site(url: str) -> str | None:
    """The source site whose budget and cooldown cover this URL, or None for CDN/archive hosts."""
    return SITE_HOSTS.get(urlsplit(url).hostname or "")


def log_url(url: str) -> str:
    """Host and path of a source page; archive servers and CDNs carry keys in the path."""
    parsed = urlsplit(url)
    return f"{parsed.hostname}{parsed.path}" if source_site(url) else f"{parsed.hostname}/..."


class NotSent(InterruptedError):
    """A request was cancelled before admission; no request reached the source."""


class SourceError(Exception):
    """A failed source request. `status` is the HTTP status that caused it, if any; `unsent` says
    the request never reached the remote host (no connection), so it cannot have had an effect."""

    def __init__(self, code: str, retry_after: int = 0, *, status: int | None = None, unsent: bool = False):
        super().__init__(code)
        self.code, self.retry_after, self.status, self.unsent = code, retry_after, status, unsent


def gallery_url(value: str) -> Source:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("Expected a supported gallery URL")
    if (site := GALLERY_HOSTS.get(parsed.hostname or "")) and (
        match := re.fullmatch(r"/g/([1-9][0-9]*)/([a-fA-F0-9]{1,64})/?", parsed.path)
    ):
        return Source(site=site, gallery_id=int(match[1]), gallery_token=match[2].lower())
    raise ValueError("Expected an E-Hentai or ExHentai gallery URL")


def _allowed_address(value: str, *, fake_ip: bool) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_global or (fake_ip and address in FAKE_IP_NETWORK)


def public_url(url: str, *, resolve: bool = True, fake_ip: bool = False) -> str:
    """`url` if it is an https URL whose host resolves only to public addresses.

    `fake_ip` additionally accepts DNS answers in FAKE_IP_NETWORK (a TUN proxy's fake-IP DNS); it
    never applies to IP-literal hosts, and every other non-public address stays rejected with
    source_private_address.
    """
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.port in {None, 443}
            and not parsed.fragment
        )
    except ValueError:
        raise SourceError("unsafe_source_url") from None
    if not valid:
        raise SourceError("unsafe_source_url")
    host = (parsed.hostname or "").rstrip(".").lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if host == "localhost" or host.endswith(".localhost") or (literal and not literal.is_global):
        raise SourceError("source_private_address")
    if resolve:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise SourceError("source_network", unsent=True) from None
        if not addresses:
            raise SourceError("source_network", unsent=True)
        rejected = sorted(
            {
                str(address[4][0])
                for address in addresses
                if not _allowed_address(str(address[4][0]), fake_ip=fake_ip and literal is None)
            }
        )
        if rejected:
            LOGGER.warning(
                "%s resolved to non-public address %s; set a proxy URL%s",
                parsed.hostname,
                ", ".join(rejected[:4]),
                "" if fake_ip else " or allow fake-IP DNS answers in source settings",
            )
            raise SourceError("source_private_address")
    return url


def retry_seconds(value: str | None, default: int = 300) -> int:
    """Seconds from a Retry-After header (delta or HTTP date), bounded to 5 s .. 1 day; `default`
    without one."""
    if not value:
        return default
    try:
        seconds = int(value)
    except ValueError:
        try:
            seconds = int((parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError):
            seconds = default
    return max(5, min(seconds, 86400))


def short_error_page(body: bytes) -> bool:
    lower = body.lower()
    return len(body) < 2048 and not any(
        marker in lower
        for marker in (
            b'id="gdt"',
            b"id='gdt'",
            b'id="cdiv"',
            b"id='cdiv'",
            b'<div class="gm"',
            b"<div class='gm'",
        )
    )


def too_fast(body: bytes) -> bool:
    """E-Hentai's warning page ("You are opening pages too fast...") that precedes an IP ban."""
    return (
        short_error_page(body) and "opening pages too fast" in body[:4096].decode("utf-8", "replace").lower()
    )


def ban_seconds(body: bytes) -> int | None:
    """Seconds left on an E-Hentai IP ban page, independent of status or content type.

    The page reads "...temporarily banned due to an excessive request rate... The ban expires in
    1 day, 2 hours, 4 minutes and 9 seconds"; unparseable durations fall back to one hour.
    """
    if not short_error_page(body) or too_fast(body):
        return None
    text = body[:4096].decode("utf-8", "replace").lower()
    if "temporarily banned" not in text and not re.search(r"ip(?: address)?.{0,80}banned", text):
        return None
    tail = text.partition("ban expires in")[2].split(".", 1)[0]
    seconds = sum(
        int(amount) * BAN_UNITS[unit]
        for amount, unit in re.findall(r"(\d+)\s*(day|hour|minute|second)", tail)
    )
    return max(5, min(seconds + 5, 86400)) if seconds else 3600


def refusal(page: bytes) -> str | None:
    """The error a Cloudflare challenge or a login ("sad panda") page stands for, else None."""
    lower = page[:65536].lower()
    if b"cf-chl-" in lower or b"challenge-platform" in lower:
        return "verification_required"
    if short_error_page(page) and (b"requires you to log on" in lower or b"sad panda" in lower):
        return "source_login_required"
    return None


def login_page(body: bytes, content_type: str, disposition: str) -> bool:
    """An ExHentai answer for a visitor without a valid session.

    Without the igneous cookie ExHentai serves a blank page, the "sad panda" image (an image/*
    response, sometimes named sadpanda in Content-Disposition) or the kokomade page.
    """
    return (
        not body.strip()
        or content_type.startswith("image/")
        or (short_error_page(body) and ("sadpanda" in disposition or b"kokomade" in body.lower()))
    )


class NoResponseCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, deadline: float, cancelled):
        self.stream, self.deadline, self.cancelled = stream, deadline, cancelled

    def __iter__(self):
        for chunk in self.stream:
            if self.cancelled and self.cancelled():
                raise InterruptedError("Cancelled")
            if time.monotonic() >= self.deadline:
                raise SourceError("source_network", 30)
            yield chunk

    def close(self) -> None:
        self.stream.close()


class SourceHTTP:
    """Outbound requests to E(x)Hentai and its archive hosts.

    Requests to a source site group (API and HTML hosts; E-Hentai and ExHentai are one group) are
    admitted against its budget (source_limits): E(x)Hentai's usage bucket, whose remaining share
    sets the concurrency (`eh_max_concurrency` at most) and the spacing of request starts. A request that
    would wait for the bucket longer than MAX_WAIT fails with source_rate_limited and its
    retry_after instead, so a job can come back later. Each redirect hop is admitted like a new
    request. Requests made inside `background()` take E(x)Hentai's budget only while no other
    request waits for it and only while BACKGROUND_RESERVE of the bucket stays free. Other hosts
    get CDN_CONCURRENCY parallel requests each (Downloads caps archive transfers per site).

    A 429, an E-Hentai IP ban page or its "opening pages too fast" warning halts the whole group:
    further requests fail fast with source_rate_limited until the halt ends (a ban until it
    expires, a 429 for its Retry-After or 300 seconds), then resume. For E(x)Hentai it also
    fills the bucket and scales its capacity and drain down for a day (Bucket.penalize);
    responses that arrive while the group is already halted (requests that were in flight
    together) only extend the halt.
    Three unrecognized short answers in a row (5xx excluded: a failing site is a transient error)
    pause the group for INVALID_SECONDS as well, but without that penalty and only in memory
    (`paused_until`); a ban page or warning met later still halts and penalizes as above.
    Once `persist` binds a library, halts are stored in its source_halt table, the budgets in
    source_budget (every SAVE_SECONDS or SAVE_REQUESTS and on close), and each E(x)Hentai rate
    limit in source_rate_event, so all of them outlive a restart.
    Settings and secrets swap atomically; the httpx client is rebuilt only when the proxy changes,
    and a replaced client stays open until the streams that use it finish.
    """

    def __init__(
        self,
        settings: SourceSettings,
        secrets: dict,
        *,
        transport=None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings, self.secrets = settings, secrets
        self.transport, self.clock = transport, clock
        self.blocked: str | None = "source_restore_paused" if settings.online_paused else None
        """An error code every request fails with: source_key_lost until the credentials are reset."""
        self.lock = threading.Lock()
        # Serialize halt changes with configuration changes. Persistence re-reads the latest
        # halt inside its database transaction so concurrent replies cannot write older state.
        self.configuration_lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.save_wake = threading.Event()
        self.library: Library | None = None
        self.halts: dict[str, Halt] = {}
        self.paused_until = 0.0
        """End of the fallback pause after unrecognized answers (epoch seconds); never stored."""
        self.halt_dirty = False
        self.waiting = 0
        # Downloads waiting for the E-Hentai bucket, first come first served.
        self.queue: deque[object] = deque()
        self.bucket = Bucket(updated=time.monotonic())
        self.unsaved = 0
        self.invalid_responses = 0
        """Requests admitted since the budgets were last saved."""
        self.saved_at = time.monotonic()
        self.local = threading.local()
        self.slots: OrderedDict[str, threading.BoundedSemaphore] = OrderedDict()
        self.slot_users: dict[str, int] = {}
        self.readers: dict[httpx.Client, int] = {}
        self.retired: set[httpx.Client] = set()
        self.client = self.build_client(secrets)

    def build_client(self, secrets: dict) -> httpx.Client:
        try:
            return httpx.Client(
                cookies=CookieJar(policy=NoResponseCookies()),
                proxy=secrets.get("proxy") or None,
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(20, connect=10),
                headers={"Accept-Encoding": "identity"},
            )
        except (ValueError, TypeError, httpx.InvalidURL):
            raise InvalidInput("Expected an HTTP or SOCKS proxy URL") from None

    @property
    def secrets(self) -> dict:
        """The credentials requests use: within a `trial` block those of this thread's trial."""
        trial = getattr(self.local, "trial", None)
        return trial[0] if trial else self._secrets

    @secrets.setter
    def secrets(self, value: dict) -> None:
        self._secrets = value

    @contextmanager
    def trial(self, secrets: dict):
        """Requests of this thread use `secrets`, with their own client when the proxy differs;
        the stored credentials stay as they are (checking typed credentials before a save)."""
        client = self.build_client(secrets) if secrets.get("proxy") != self._secrets.get("proxy") else None
        self.local.trial = (secrets, client)
        try:
            yield
        finally:
            self.local.trial = None
            if client is not None:
                client.close()

    def apply(self, settings: SourceSettings, secrets: dict, client: httpx.Client | None = None) -> None:
        """Use new settings for later requests; `client` replaces the current one (proxy change)."""
        with self.configuration_lock, self.lock:
            self.settings, self.secrets = settings, secrets
            if settings.online_paused:
                self.blocked = "source_restore_paused"
            elif self.blocked == "source_restore_paused":
                self.blocked = None
            if client is not None:
                previous, self.client = self.client, client
                self._retire(previous)

    def persist(self, library: Library) -> None:
        """Store halts and budgets in `library` from now on and honour the ones it already holds.

        The stored bucket drains for the time the server was off.
        """
        with library.connection() as db:
            rows = db.execute("SELECT site,until,reason,strikes FROM source_halt").fetchall()
            budgets = db.execute("SELECT * FROM source_budget").fetchall()
        with self.lock:
            self.library = library
            for row in rows:
                current = self.halts.get(row["site"])
                if current is None or row["until"] > current.until:
                    self.halts[row["site"]] = Halt(
                        min(row["until"], self.clock() + MAX_HALT), row["reason"], row["strikes"]
                    )
            wall, now = self.clock(), time.monotonic()
            for row in budgets:
                bucket = self.bucket
                if row["penalty_until"] > wall and row["penalty"] < bucket.factor(wall):
                    bucket.penalty, bucket.penalty_until = row["penalty"], row["penalty_until"]
                _, leak = self._eh_limits(wall)
                stored = row["level"] - leak * max(0.0, wall - row["updated"]) / 60
                bucket.drain(now, leak)
                bucket.level = max(bucket.level, stored)

    def _retire(self, client: httpx.Client) -> None:
        if self.readers.get(client):
            self.retired.add(client)
        else:
            client.close()

    def close(self):
        # Halts save the budgets at once; only requests admitted since the last save are owed.
        if self.unsaved or self.halt_dirty:
            self._save(force=True)
        with self.lock:
            self._retire(self.client)

    def cooldowns(self) -> dict[str, int]:
        """Active halts per site as epoch seconds; every site of a halted group is listed."""
        now = self.clock()
        with self.lock:
            until = max([halt.until for halt in self.halts.values()], default=0.0)
            until = max(until, self.paused_until)
            return {site: math.ceil(until) for site in EH_SITES} if until > now else {}

    def budget(self) -> dict:
        """The E(x)Hentai usage budget as it stands."""
        with self.lock:
            wall, now = self.clock(), time.monotonic()
            capacity, leak = self._eh_limits(wall)
            bucket = self.bucket
            bucket.drain(now, leak)
            return {
                "ehentai": {
                    "level": round(bucket.level, 1),
                    "capacity": round(capacity, 1),
                    "leak_per_min": round(leak, 2),
                    "remaining": round(min(1.0, max(0.0, (capacity - bucket.level) / capacity)), 3),
                    # Seconds until the bucket has drained empty, i.e. the whole budget is free.
                    "empty_in": math.ceil(bucket.level / leak * 60),
                    "penalty": round(bucket.penalty, 3),
                    "penalty_until": math.ceil(bucket.penalty_until) if bucket.penalty < 1 else None,
                },
            }

    def _eh_limits(self, wall: float) -> tuple[float, float]:
        """E(x)Hentai bucket capacity and drain per minute at `wall`, penalty included."""
        factor = self.bucket.factor(wall)
        return self.settings.eh_budget_capacity * factor, self.settings.eh_budget_leak_per_min * factor

    def _halt(self, url: str, reason: str, seconds: int = 0) -> int:
        """Serialize a rate-limit response with other halts."""
        seconds = self._save_halt(url, reason, seconds)
        self._save(force=True)
        return seconds

    def _save_halt(self, url: str, reason: str, seconds: int) -> int:
        """Halt requests to the site group of `url` and, for E(x)Hentai, fill and shrink its
        bucket; returns the seconds left on the halt.

        A "too fast" warning ignores `seconds`: its halt grows with each repeat (TOO_FAST_SECONDS).
        """
        key = EH_SITES[0]
        kind = eh_kind(url)
        with self.configuration_lock, self.lock:
            now = self.clock()
            previous = self.halts.get(key)
            # Requests in flight together meet the same rate limit; only the first one counts.
            repeat = previous is not None and previous.until > now
            strikes = previous.strikes if repeat and previous else 1
            if reason == "too_fast":
                if (
                    not repeat
                    and previous
                    and previous.reason == "too_fast"
                    and previous.until > now - STRIKE_WINDOW
                ):
                    strikes = previous.strikes + 1
                seconds = min(TOO_FAST_SECONDS * 2 ** min(strikes - 1, 16), MAX_HALT)
            until = max(now + seconds, previous.until if previous else 0)
            if repeat and previous:
                if until == previous.until:
                    return max(1, math.ceil(until - now))
                reason = reason if now + seconds > previous.until else previous.reason
            self.halts[key] = Halt(until, reason, strikes)
            event, shrunk = None, ""
            if not repeat:
                capacity, leak = self._eh_limits(now)
                self.bucket.drain(time.monotonic(), leak)
                level = self.bucket.level
                self.bucket.penalize(now)
                capacity_after, leak_after = self._eh_limits(now)
                self.bucket.level = capacity_after
                event = (level, (capacity - level) / capacity, self.bucket.penalty)
                shrunk = (
                    f"; the usage bucket was {level:.0f} of {capacity:.0f} points full, and holds "
                    f"{capacity_after:.0f} points draining {leak_after:.1f} a minute for 24 hours"
                )
            library = self.library
        LOGGER.warning(
            "%s: %s; halting requests until %s%s",
            "/".join(EH_SITES),
            HALT_REASONS[reason],
            datetime.fromtimestamp(until).isoformat(sep=" ", timespec="seconds"),
            shrunk,
        )
        if library is not None:
            try:
                with library.transaction() as db:
                    with self.lock:
                        latest = self.halts[key]
                    db.execute(
                        "INSERT INTO source_halt(site,until,reason,strikes) VALUES(?,?,?,?) "
                        "ON CONFLICT(site) DO UPDATE SET until=excluded.until,reason=excluded.reason,"
                        "strikes=excluded.strikes",
                        (key, math.ceil(latest.until), latest.reason, latest.strikes),
                    )
                    if event:
                        self._record(db, now, key, kind, reason, until, *event)
            except sqlite3.Error:
                with self.lock:
                    self.halt_dirty = True
                LOGGER.warning("The halt for %s remains pending for persistence", key)
        return max(1, math.ceil(until - now))

    @staticmethod
    def _record(db, now, site, kind, reason, until, level, remaining, penalty) -> None:
        """Log an E(x)Hentai rate limit with the bucket as it stood (last 100 kept)."""
        db.execute(
            "INSERT INTO source_rate_event(at,site,kind,reason,until,level,remaining,penalty) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (math.floor(now), site, kind, reason, math.ceil(until), level, remaining, penalty),
        )
        db.execute(
            "DELETE FROM source_rate_event WHERE id NOT IN "
            "(SELECT id FROM source_rate_event ORDER BY id DESC LIMIT 100)"
        )

    def _retry_halts(self) -> None:
        with self.lock:
            if not self.halt_dirty or self.library is None:
                return
            snapshot, library = self.halts.copy(), self.library
        try:
            with library.transaction() as db:
                db.executemany(
                    "INSERT INTO source_halt VALUES(?,?,?,?) ON CONFLICT(site) DO UPDATE SET "
                    "until=excluded.until,reason=excluded.reason,strikes=excluded.strikes",
                    [
                        (key, math.ceil(halt.until), halt.reason, halt.strikes)
                        for key, halt in snapshot.items()
                    ],
                )
        except sqlite3.Error:
            LOGGER.warning("Source halts remain pending for persistence")
        else:
            with self.lock:
                if self.halts == snapshot:
                    self.halt_dirty = False

    def save_if_dirty(self) -> None:
        self._save()

    def _save(self, *, force: bool = False) -> None:
        """Store the budgets once SAVE_REQUESTS were admitted or SAVE_SECONDS passed since the
        last save; `force` stores them now. A save already running makes an unforced one skip."""
        if self.library is None:
            return
        if not self.save_lock.acquire(blocking=force):
            return
        try:
            self._retry_halts()
            with self.lock:
                now = time.monotonic()
                if not force and (
                    not self.unsaved or (self.unsaved < SAVE_REQUESTS and now - self.saved_at < SAVE_SECONDS)
                ):
                    return
                wall = self.clock()
                _, leak = self._eh_limits(wall)
                bucket = self.bucket
                bucket.drain(now, leak)
                row = (EH_SITES[0], bucket.level, wall, bucket.penalty, bucket.penalty_until)
                saving = self.unsaved
                library = self.library
            try:
                with library.transaction() as db:
                    db.execute(
                        "INSERT INTO source_budget(site,level,updated,penalty,penalty_until) "
                        "VALUES(?,?,?,?,?) ON CONFLICT(site) DO UPDATE SET level=excluded.level,"
                        "updated=excluded.updated,penalty=excluded.penalty,"
                        "penalty_until=excluded.penalty_until",
                        row,
                    )
            except sqlite3.Error:
                LOGGER.exception("The source request budgets are not saved")
            else:
                with self.lock:
                    self.unsaved -= saving
                    self.saved_at = now
        finally:
            self.save_lock.release()

    def _check_halt(self, key: str) -> None:
        """Raise source_rate_limited while `key`'s group is halted or paused; call with the lock held."""
        halt = self.halts.get(key)
        until = max(halt.until if halt is not None else 0.0, self.paused_until)
        if (remaining := until - self.clock()) > 0:
            raise SourceError("source_rate_limited", math.ceil(remaining))

    @contextmanager
    def background(self):
        """Requests of this thread yield their site's budget to requests made outside the block."""
        self.local.background = True
        try:
            yield
        finally:
            self.local.background = False

    def _headers(self, url: str, secrets: dict, user_agent: str, extra: dict | None = None) -> dict:
        parsed = urlsplit(url)
        headers = {"User-Agent": user_agent, **(extra or {})}
        cookie = {"ehentai": "eh_cookie", "exhentai": "ex_cookie"}.get(
            SITE_HOSTS.get(parsed.hostname or "", "")
        )
        cookies = [secrets[cookie]] if cookie and secrets.get(cookie) else []
        gallery = parsed.hostname in GALLERY_HOSTS and parsed.path.startswith("/g/")
        if gallery and not any(part.strip().startswith("nw=") for part in ";".join(cookies).split(";")):
            cookies.append(NO_WARNING_COOKIE)
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        return headers

    def _admit(self, url: str, cancelled) -> Callable[[], None]:
        """Wait for this URL's turn; returns the release callback. Never sleeps holding the lock."""

        entered = time.monotonic()

        def check():
            if time.monotonic() - entered >= MAX_WAIT:
                raise SourceError("source_rate_limited", MAX_WAIT)
            if cancelled and cancelled():
                raise NotSent("Cancelled before request admission")

        check()
        site = source_site(url)
        if site is None:
            host = urlsplit(url).hostname or ""
            with self.lock:
                semaphore = self.slots.setdefault(host, threading.BoundedSemaphore(CDN_CONCURRENCY))
                self.slots.move_to_end(host)
                self.slot_users[host] = self.slot_users.get(host, 0) + 1
                self._trim_slots()
            acquired = False

            def release():
                if acquired:
                    semaphore.release()
                with self.lock:
                    self.slot_users[host] -= 1
                    self._trim_slots()

            try:
                while not semaphore.acquire(timeout=POLL):
                    check()
                acquired = True
                return release
            except BaseException:
                release()
                raise
        key = EH_SITES[0]
        kind = eh_kind(url)
        background = getattr(self.local, "background", False)
        counted = False
        ticket = object()
        try:
            while True:
                check()
                with self.lock:
                    self._check_halt(key)
                    queue = self.queue
                    if not background and not counted:
                        self.waiting += 1
                        queue.append(ticket)
                        counted = True
                    if background:
                        wait = POLL if self.waiting else self._admit_eh(kind, background)
                    else:
                        wait = POLL if queue[0] is not ticket else self._admit_eh(kind, background)
                    if wait is None:
                        self.unsaved += 1
                        if self.unsaved >= SAVE_REQUESTS:
                            self.save_wake.set()
                        break
                time.sleep(min(wait, POLL))
        finally:
            if counted:
                with self.lock:
                    self.waiting -= 1
                    self.queue.remove(ticket)

        def done():
            with self.lock:
                self.bucket.in_flight -= 1

        return done

    def _trim_slots(self) -> None:
        for host in list(self.slots):
            if len(self.slots) <= 64:
                break
            if not self.slot_users.get(host):
                self.slots.pop(host)
                self.slot_users.pop(host, None)

    def _admit_eh(self, kind: str, background: bool) -> float | None:
        """Spend the bucket for one request of `kind` (None) or return the seconds to wait;
        raise source_rate_limited when that is longer than MAX_WAIT. Call with the lock held."""
        wall = self.clock()
        capacity, leak = self._eh_limits(wall)
        wait = self.bucket.admit(
            EH_COSTS[kind],
            now=time.monotonic(),
            capacity=capacity,
            leak_per_min=leak,
            max_concurrency=self.settings.eh_max_concurrency,
            background=background,
        )
        if wait is not None and wait > MAX_WAIT:
            LOGGER.info(
                "E-Hentai usage bucket at %.0f of %.0f points; %s %s request deferred %d s",
                self.bucket.level,
                capacity,
                "background" if background else "a",
                kind,
                math.ceil(wait),
            )
            raise SourceError("source_rate_limited", math.ceil(wait))
        return wait

    @contextmanager
    def stream(self, method: str, url: str, *, cancelled=None, headers=None, **kwargs):
        if self.blocked:
            raise SourceError(self.blocked, unsent=True)
        with self.lock:
            trial = getattr(self.local, "trial", None)
            client = trial[1] if trial and trial[1] is not None else self.client
            secrets, settings = self.secrets, self.settings
            self.readers[client] = self.readers.get(client, 0) + 1
        try:
            with self._stream(
                client, secrets, settings, method, url, cancelled=cancelled, headers=headers, **kwargs
            ) as response:
                yield response
        finally:
            with self.lock:
                self.readers[client] -= 1
                if not self.readers[client]:
                    del self.readers[client]
                    if client in self.retired:
                        self.retired.remove(client)
                        client.close()

    @contextmanager
    def _stream(
        self,
        client,
        secrets,
        settings,
        method,
        url,
        *,
        cancelled=None,
        headers=None,
        total_timeout: float = 120,
        **kwargs,
    ):
        sent = False
        deadline = time.monotonic() + total_timeout
        try:
            for _ in range(6):
                host = (urlsplit(url).hostname or "").rstrip(".").lower()
                if host == "nhentai.net" or host.endswith(".nhentai.net"):
                    raise SourceError("unsupported_source", unsent=True)
                public_url(
                    url,
                    resolve=self.transport is None and not secrets.get("proxy"),
                    fake_ip=settings.allow_fake_ip,
                )
                checked_addresses: set[str] = set()
                if self.transport is None and not secrets.get("proxy"):
                    try:
                        checked_addresses = {
                            str(entry[4][0])
                            for entry in socket.getaddrinfo(
                                urlsplit(url).hostname, 443, type=socket.SOCK_STREAM
                            )
                        }
                    except OSError:
                        raise SourceError("source_network", 30) from None
                    if not checked_addresses or any(
                        not _allowed_address(address, fake_ip=settings.allow_fake_ip)
                        for address in checked_addresses
                    ):
                        raise SourceError("source_private_address")
                release = self._admit(url, cancelled)
                try:
                    started = time.monotonic()
                    request_headers = self._headers(url, secrets, settings.user_agent, headers)
                    sent = True
                    with client.stream(
                        method,
                        url,
                        headers=request_headers,
                        **kwargs,
                    ) as response:
                        if time.monotonic() >= deadline:
                            raise SourceError("source_network", 30)
                        assert isinstance(response.stream, httpx.SyncByteStream)
                        response.stream = DeadlineStream(response.stream, deadline, cancelled)
                        LOGGER.debug(
                            "%s %s -> %d (%.2f s)",
                            method,
                            log_url(url),
                            response.status_code,
                            time.monotonic() - started,
                        )
                        if not self.transport and not secrets.get("proxy"):
                            stream = response.extensions.get("network_stream")
                            peer = stream.get_extra_info("server_addr") if stream else None
                            if peer is None or str(peer[0]) not in checked_addresses:
                                raise SourceError("source_private_address")
                        if response.has_redirect_location:
                            if method != "GET":
                                raise SourceError("source_protocol")
                            try:
                                url = urljoin(url, response.headers.get("location", ""))
                                _ = urlsplit(url).port  # a malformed host or port raises ValueError
                            except ValueError:
                                # A broken Location header is a bad answer, like a dropped connection.
                                raise SourceError("source_network", 30) from None
                            # Resume headers survive the redirect; Referer and credentials do not.
                            headers = {k: v for k, v in (headers or {}).items() if k.lower() in RANGE_HEADERS}
                            continue
                        if 300 <= response.status_code < 400:
                            raise SourceError("source_protocol")
                        if response.headers.get("cf-mitigated") == "challenge":
                            raise SourceError("verification_required")
                        if response.status_code == 429:
                            seconds = retry_seconds(
                                response.headers.get("retry-after"),
                                300,
                            )
                            if source_site(url):
                                seconds = self._halt(url, "http_429", seconds)
                            raise SourceError("source_rate_limited", seconds)
                        status = response.status_code
                        prefix = bytearray()
                        if status >= 400:
                            for chunk in response.iter_bytes():
                                prefix.extend(chunk)
                                if len(prefix) >= 65536:
                                    break
                            self.check_ban(url, bytes(prefix), error_response=True)
                            if refusal(bytes(prefix)) == "verification_required":
                                raise SourceError("verification_required")
                        if (
                            status == 404
                            and source_site(url) is not None
                            and b"gallery not available" in bytes(prefix).lower()
                        ):
                            raise SourceError("gallery_unavailable", status=status)
                        if status in {401, 403}:
                            raise SourceError("source_login_required", status=status)
                        if status in {404, 410}:
                            raise SourceError("source_unavailable", status=status)
                        if status >= 400:
                            if status < 500:  # a failing site (5xx) is transient, not unrecognized
                                self.protocol_result(url, False, bytes(prefix))
                            raise SourceError("source_unavailable", 30, status=status)
                        yield response
                        return
                finally:
                    release()
            raise SourceError("source_redirect_limit")
        except SourceError as error:
            if not sent:
                error.unsent = True
            raise
        except (httpx.HTTPError, socksio.ProtocolError) as error:
            LOGGER.info("%s %s failed: %s", method, log_url(url), type(error).__name__)
            unsent = isinstance(
                error, httpx.ConnectError | httpx.ConnectTimeout | httpx.ProxyError | socksio.ProtocolError
            )
            raise SourceError("source_network", 30, unsent=unsent) from None

    def read(self, method: str, url: str, *, limit: int = 2 * 1024**2, **kwargs) -> bytes:
        with self.stream(method, url, **kwargs) as response:
            final_url = str(response.url)
            site = source_site(final_url)
            content_type = response.headers.get("content-type", "").lower()
            disposition = response.headers.get("content-disposition", "").lower()
            result = bytearray()
            for chunk in response.iter_bytes():
                if kwargs.get("cancelled") and kwargs["cancelled"]():
                    raise InterruptedError("Cancelled")
                if len(result) + len(chunk) > limit:
                    raise SourceError("source_response_too_large")
                result.extend(chunk)
        is_json = "application/json" in content_type or bytes(result).lstrip().startswith((b"{", b"["))
        self.check_ban(final_url, bytes(result))
        if not is_json:
            if code := refusal(bytes(result)):
                raise SourceError(code)
            if site == "exhentai" and login_page(bytes(result), content_type, disposition):
                raise SourceError("source_login_required")
        return bytes(result)

    def check_ban(self, url: str, body: bytes, *, error_response: bool = False) -> None:
        if source_site(url) is None:
            return
        if not error_response and body.lstrip().startswith((b"{", b"[")):
            try:
                value = json.loads(body)
            except (ValueError, UnicodeError):
                pass
            else:
                if isinstance(value, (dict, list)) and (
                    isinstance(value, list)
                    or any(key in value for key in ("gmetadata", "title", "title_jpn", "tags"))
                ):
                    return
        if (seconds := ban_seconds(body)) is not None:
            raise SourceError("source_rate_limited", self._halt(url, "ban", seconds))
        if too_fast(body):
            raise SourceError("source_rate_limited", self._halt(url, "too_fast"))

    def protocol_result(self, url: str, valid: bool, body: bytes = b"") -> None:
        """Count unrecognized short answers; the third in a row pauses the group for
        INVALID_SECONDS (no bucket penalty, no rate event, not stored)."""
        if source_site(url) is None:
            return
        with self.lock:
            self.invalid_responses = 0 if valid or not short_error_page(body) else self.invalid_responses + 1
            if self.invalid_responses < 3:
                return
            self.invalid_responses = 0
            self.paused_until = max(self.paused_until, self.clock() + INVALID_SECONDS)
            until = self.paused_until
        LOGGER.warning(
            "%s: %s; pausing requests until %s",
            "/".join(EH_SITES),
            HALT_REASONS["invalid_responses"],
            datetime.fromtimestamp(until).isoformat(sep=" ", timespec="seconds"),
        )
        raise SourceError("source_rate_limited", max(1, math.ceil(until - self.clock())))

    def json(self, method: str, url: str, **kwargs):
        body = self.read(method, url, **kwargs)
        try:
            value = json.loads(body)
        except (ValueError, UnicodeError):
            self.protocol_result(url, False, body)
            raise SourceError("source_protocol") from None
        return value
