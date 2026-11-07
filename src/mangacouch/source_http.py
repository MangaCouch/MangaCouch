from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar, DefaultCookiePolicy
from urllib.parse import urljoin, urlsplit

import httpx
import socksio

from .errors import InvalidInput
from .models import Source
from .source_config import SourceSettings

API_PACE = 3.0
"""Seconds between two requests to the API/HTML hosts of one source site."""
CDN_CONCURRENCY = 4
"""Parallel requests per other host (thumbnail CDNs, archive servers); they are not paced."""
SITE_HOSTS = {
    "e-hentai.org": "ehentai",
    "api.e-hentai.org": "ehentai",
    "exhentai.org": "exhentai",
    "nhentai.net": "nhentai",
}
RANGE_HEADERS = {"range", "if-range"}


def source_site(url: str) -> str | None:
    """The source site whose pacing and cooldown cover this URL, or None for CDN/archive hosts."""
    return SITE_HOSTS.get(urlsplit(url).hostname or "")


class SourceError(Exception):
    def __init__(self, code: str, retry_after: int = 0):
        super().__init__(code)
        self.code, self.retry_after = code, retry_after


def gallery_url(value: str) -> Source:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 80, 443}
    ):
        raise ValueError("Expected a supported gallery URL")
    if parsed.hostname == "nhentai.net" and (match := re.fullmatch(r"/g/([1-9][0-9]*)/?", parsed.path)):
        return Source(site="nhentai", gallery_id=int(match[1]))
    if parsed.hostname in {"e-hentai.org", "exhentai.org"} and (
        match := re.fullmatch(r"/g/([1-9][0-9]*)/([a-fA-F0-9]{1,64})/?", parsed.path)
    ):
        return Source(
            site="ehentai" if parsed.hostname == "e-hentai.org" else "exhentai",
            gallery_id=int(match[1]),
            gallery_token=match[2].lower(),
        )
    raise ValueError("Expected an EH, ExHentai or nHentai gallery URL")


def public_url(url: str, *, resolve: bool = True) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise SourceError("unsafe_source_url")
    if resolve:
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(
                not ipaddress.ip_address(address[4][0]).is_global for address in addresses
            ):
                raise SourceError("unsafe_source_url")
        except socket.gaierror:
            raise SourceError("source_network") from None
    return url


def retry_seconds(value: str | None) -> int:
    """Seconds from a Retry-After header (delta or HTTP date), bounded to 5 s .. 1 day."""
    try:
        seconds = int(value or "300")
    except ValueError:
        try:
            seconds = int((parsedate_to_datetime(value or "") - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError):
            seconds = 300
    return max(5, min(seconds, 86400))


class NoResponseCookies(DefaultCookiePolicy):
    def set_ok(self, cookie, request):
        return False


class SourceHTTP:
    """Outbound requests to E(x)Hentai and nHentai.

    Requests to one source site (API and HTML hosts) are spaced `pace` seconds apart; other hosts
    get CDN_CONCURRENCY parallel requests. A 429 starts a per-site cooldown during which further
    requests to that site fail fast with source_rate_limited. Settings and secrets swap atomically;
    the httpx client is rebuilt only when the proxy changes, and a replaced client stays open
    until the streams that use it finish.
    """

    def __init__(self, settings: SourceSettings, secrets: dict, *, transport=None, pace: float = API_PACE):
        self.settings, self.secrets = settings, secrets
        self.pace, self.transport = pace, transport
        self.lock = threading.Lock()
        self.next_slot: dict[str, float] = {}
        self.cooldown: dict[str, float] = {}
        self.slots: dict[str, threading.BoundedSemaphore] = {}
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

    def apply(self, settings: SourceSettings, secrets: dict, client: httpx.Client | None = None) -> None:
        """Use new settings for later requests; `client` replaces the current one (proxy change)."""
        with self.lock:
            self.settings, self.secrets = settings, secrets
            if client is not None:
                previous, self.client = self.client, client
                self._retire(previous)

    def _retire(self, client: httpx.Client) -> None:
        if self.readers.get(client):
            self.retired.add(client)
        else:
            client.close()

    def close(self):
        with self.lock:
            self._retire(self.client)

    def cooldowns(self) -> dict[str, int]:
        """Active per-site cooldowns as epoch seconds."""
        now = time.time()
        with self.lock:
            return {site: math.ceil(until) for site, until in self.cooldown.items() if until > now}

    def _headers(self, url: str, secrets: dict, user_agent: str, extra: dict | None = None) -> dict:
        parsed = urlsplit(url)
        headers = {"User-Agent": user_agent, **(extra or {})}
        cookie = {
            "e-hentai.org": "eh_cookie",
            "api.e-hentai.org": "eh_cookie",
            "exhentai.org": "ex_cookie",
            "nhentai.net": "nh_cookie",
        }.get(parsed.hostname or "")
        if cookie and secrets.get(cookie):
            headers["Cookie"] = secrets[cookie]
        if parsed.hostname == "nhentai.net" and parsed.path.startswith("/api/v2/") and secrets.get("nh_key"):
            headers["Authorization"] = "Key " + secrets["nh_key"]
        return headers

    def _admit(self, url: str, cancelled):
        """Wait for this URL's turn; returns the release callback. Never sleeps holding the lock."""

        def check():
            if cancelled and cancelled():
                raise InterruptedError("Cancelled")

        check()
        site = source_site(url)
        if site is None:
            host = urlsplit(url).hostname or ""
            with self.lock:
                semaphore = self.slots.setdefault(host, threading.BoundedSemaphore(CDN_CONCURRENCY))
            while not semaphore.acquire(timeout=0.1):
                check()
            return semaphore.release
        with self.lock:
            remaining = self.cooldown.get(site, 0) - time.time()
            if remaining > 0:
                raise SourceError("source_rate_limited", math.ceil(remaining))
            slot = max(time.monotonic(), self.next_slot.get(site, 0.0))
            self.next_slot[site] = slot + self.pace
        while True:
            check()
            with self.lock:
                remaining = self.cooldown.get(site, 0) - time.time()
                if remaining > 0:
                    raise SourceError("source_rate_limited", math.ceil(remaining))
            delay = slot - time.monotonic()
            if delay <= 0:
                break
            time.sleep(min(delay, 0.1))
        return lambda: None

    @contextmanager
    def stream(self, method: str, url: str, *, cancelled=None, headers=None, **kwargs):
        release = self._admit(url, cancelled)
        try:
            with self.lock:
                client, secrets, settings = self.client, self.secrets, self.settings
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
        finally:
            release()

    @contextmanager
    def _stream(self, client, secrets, settings, method, url, *, cancelled=None, headers=None, **kwargs):
        try:
            for _ in range(6):
                public_url(url, resolve=self.transport is None and not secrets.get("proxy"))
                if cancelled and cancelled():
                    raise InterruptedError("Cancelled")
                with client.stream(
                    method, url, headers=self._headers(url, secrets, settings.user_agent, headers), **kwargs
                ) as response:
                    if response.is_redirect:
                        if method != "GET":
                            raise SourceError("source_protocol")
                        url = urljoin(url, response.headers.get("location", ""))
                        # Resume headers survive the redirect; Referer and credentials do not.
                        headers = {k: v for k, v in (headers or {}).items() if k.lower() in RANGE_HEADERS}
                        continue
                    if response.headers.get("cf-mitigated") == "challenge" or (
                        response.status_code in {403, 503}
                        and "cloudflare" in response.headers.get("server", "").lower()
                    ):
                        raise SourceError("verification_required")
                    if response.status_code == 429:
                        seconds = retry_seconds(response.headers.get("retry-after"))
                        if site := source_site(url):
                            with self.lock:
                                self.cooldown[site] = max(self.cooldown.get(site, 0), time.time() + seconds)
                        raise SourceError("source_rate_limited", seconds)
                    if response.status_code in {401, 403}:
                        raise SourceError("source_login_required")
                    if response.status_code in {404, 410}:
                        raise SourceError("source_unavailable")
                    if response.status_code >= 400:
                        raise SourceError("source_unavailable", 30)
                    yield response
                    return
            raise SourceError("source_redirect_limit")
        except (httpx.HTTPError, socksio.ProtocolError):
            raise SourceError("source_network", 30) from None

    def read(self, method: str, url: str, *, limit: int = 2 * 1024**2, **kwargs) -> bytes:
        with self.stream(method, url, **kwargs) as response:
            result = bytearray()
            for chunk in response.iter_bytes(65536):
                if kwargs.get("cancelled") and kwargs["cancelled"]():
                    raise InterruptedError("Cancelled")
                if len(result) + len(chunk) > limit:
                    raise SourceError("source_response_too_large")
                result.extend(chunk)
        lower = bytes(result[:65536]).lower()
        if b"cf-chl-" in lower or b"challenge-platform" in lower:
            raise SourceError("verification_required")
        if b"requires you to log on" in lower or b"sad panda" in lower:
            raise SourceError("source_login_required")
        return bytes(result)

    def json(self, method: str, url: str, **kwargs):
        try:
            return json.loads(self.read(method, url, **kwargs))
        except (ValueError, UnicodeError):
            raise SourceError("source_protocol") from None
