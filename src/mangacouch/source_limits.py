"""Request budget of E(x)Hentai's usage bucket.

E-Hentai keeps one leaky bucket per IP address for every page, API call and search: each request
adds points, the bucket drains at a steady rate, and a full bucket gets the IP banned (5 minutes
at first, longer on repeats). Concurrency and spacing do not matter, only the level does. Measured
on fresh IPs on 2026-11-08: about 1400 points, draining about 16 points a minute, with a gallery
page costing 1, an API call about 1.4, a search page about 4.2 and archiver.php about 0.25. The
defaults here (1100 points, 13 a minute) stay about 80% of that, since a browser on the same IP
spends from the same bucket.

Points are spent when a request is admitted, before it is sent, and are
never given back, whatever the answer (or the lack of one); each redirect hop is a new request.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

EH_COSTS = {"page": 1.0, "api": 1.5, "search": 4.5, "archiver": 0.5}
"""Usage points per E(x)Hentai request kind (`eh_kind`); a gallery page is 1."""
EH_CAPACITY = 1100
EH_LEAK_PER_MIN = 13
EH_MAX_CONCURRENCY = 4
SEARCH_PATHS = {"", "/favorites.php", "/popular", "/watched", "/toplist.php"}
"""Listing paths (trailing slash removed) that cost a search; so does any /tag/ or /uploader/ page
and any page with an f_search or next parameter."""
SEARCH_PREFIXES = ("/tag/", "/uploader/")
FULL_SPEED = 0.5
"""Remaining share of the bucket from which requests go at full concurrency with no spacing."""
SLOWEST = 0.1
"""Remaining share below which one request runs at a time, spaced at the drain rate of its cost."""
BACKGROUND_RESERVE = 0.2
"""Share of the bucket that background work (source checks) leaves to requests made by hand."""
MAX_WAIT = 60
"""Longest wait for the bucket inside a request; a longer one fails with source_rate_limited."""
PENALTY = 0.67
"""After a rate limit the bucket's capacity and drain are scaled by this for PENALTY_SECONDS;
each further one within that time scales them again, down to PENALTY_FLOOR of the settings."""
PENALTY_FLOOR = 0.3
PENALTY_SECONDS = 86400
SAVE_SECONDS = 10
SAVE_REQUESTS = 20
"""The budgets are written to the database after this many seconds or admitted requests."""
POLL = 0.1
"""Seconds between checks while a request waits for a free concurrency slot."""
EPSILON = 1e-6


def eh_kind(url: str) -> str:
    """The E(x)Hentai request kind of a source URL: 'api', 'archiver', 'search' or 'page'.

    'page' covers /g/ (comments too), /s/, /mpv/, gallerytorrents.php, gallerypopups.php and every
    other page not listed as a search.
    """
    parts = urlsplit(url)
    if parts.path.endswith("/api.php"):
        return "api"
    if parts.path == "/archiver.php":
        return "archiver"
    query = parse_qs(parts.query, keep_blank_values=True)
    if (
        parts.path.rstrip("/") in SEARCH_PATHS
        or parts.path.startswith(SEARCH_PREFIXES)
        or "f_search" in query
        or "next" in query
    ):
        return "search"
    return "page"


def ladder(remaining: float, cost: float, max_concurrency: int, leak_per_min: float) -> tuple[int, float]:
    """Concurrency and seconds between request starts for a bucket with `remaining` (0..1) left.

    Full speed from FULL_SPEED up; below it both slow down linearly until, under SLOWEST, one
    request runs at a time and starts follow the drain rate of `cost`.
    """
    slow = min(1.0, max(0.0, (FULL_SPEED - remaining) / (FULL_SPEED - SLOWEST)))
    concurrency = max(1, math.ceil(max_concurrency * (1 - slow) - EPSILON))
    return concurrency, min(MAX_WAIT - 1, slow * cost / leak_per_min * 60)


@dataclass
class Bucket:
    """E(x)Hentai usage: `level` points as of monotonic time `updated`, plus the penalty factor
    in force until wall-clock `penalty_until`."""

    level: float = 0.0
    updated: float = 0.0
    penalty: float = 1.0
    penalty_until: float = 0.0
    started: float | None = None
    """Monotonic start of the latest admitted request."""
    in_flight: int = 0

    def factor(self, wall: float) -> float:
        """The penalty factor at `wall`; an expired penalty is lifted."""
        if self.penalty < 1 and wall >= self.penalty_until:
            self.penalty, self.penalty_until = 1.0, 0.0
        return self.penalty

    def drain(self, now: float, leak_per_min: float) -> None:
        self.level = max(0.0, self.level - leak_per_min * max(0.0, now - self.updated) / 60)
        self.updated = now

    def penalize(self, wall: float) -> None:
        """Scale capacity and drain down after a rate limit; a repeat within the penalty period
        scales them again (not below PENALTY_FLOOR) and every one restarts the period."""
        repeat = self.factor(wall) < 1
        self.penalty = max(PENALTY_FLOOR, self.penalty * PENALTY) if repeat else PENALTY
        self.penalty_until = wall + PENALTY_SECONDS

    def admit(
        self,
        cost: float,
        *,
        now: float,
        capacity: float,
        leak_per_min: float,
        max_concurrency: int,
        background: bool = False,
    ) -> float | None:
        """Spend `cost` and return None, or return the seconds to wait before trying again.

        A request waits while the bucket cannot take its cost, while background work would leave
        less than BACKGROUND_RESERVE, until the spacing of `ladder` has passed since the previous
        start, and while the ladder's concurrency is in use (POLL).
        """
        self.drain(now, leak_per_min)
        remaining = (capacity - self.level) / capacity
        concurrency, interval = ladder(remaining, cost, max_concurrency, leak_per_min)
        over = self.level + cost - capacity
        if background:
            over = max(over, self.level - (1 - BACKGROUND_RESERVE) * capacity)
        wait = max(0.0, over) / leak_per_min * 60
        if self.started is not None:
            wait = max(wait, self.started + interval - now)
        if wait > EPSILON:
            return wait
        if self.in_flight >= concurrency:
            return POLL
        self.level += cost
        self.started = now
        self.in_flight += 1
        return None
