"""Source request budgets (E(x)Hentai's usage bucket), persisted rate-limit
halts and background-check priority. Budget tests run on a fake clock and a mock transport."""

from __future__ import annotations

import threading
import time

import httpx
import pytest
from helpers import download_step, finish_download, finish_scan, inbox, make_zip, native_file, ready
from pydantic import ValidationError
from source_fixtures import EH, TOO_FAST, SourceFixture

from mangacouch import downloads as downloads_module
from mangacouch import source_checks as source_checks_module
from mangacouch import source_http as source_http_module
from mangacouch.downloads import Downloads
from mangacouch.models import EH_SITES, Metadata, Source
from mangacouch.scanning import Scanner
from mangacouch.source_config import SourceSettings, SourceVault
from mangacouch.source_http import (
    MAX_HALT,
    STRIKE_WINDOW,
    TOO_FAST_SECONDS,
    SourceError,
    SourceHTTP,
    ban_seconds,
    source_site,
)
from mangacouch.source_limits import (
    EH_COSTS,
    PENALTY_SECONDS,
    POLL,
    Bucket,
    eh_kind,
    ladder,
)

BAN = (
    b"This IP address has been temporarily banned due to an excessive request rate. "
    b"The ban expires in 4 minutes and 9 seconds"
)
API_URL = "https://api.e-hentai.org/api.php"
EX_API_URL = "https://exhentai.org/api.php"
PAGE_URL = "https://e-hentai.org/g/1/abcdef0123/"
EX_PAGE_URL = "https://exhentai.org/archiver.php?gid=1"
SEARCH_URL = "https://e-hentai.org/?f_search=title"
EH_HOSTS = [
    "e-hentai.org",
    "www.e-hentai.org",
    "g.e-hentai.org",
    "api.e-hentai.org",
    "forums.e-hentai.org",
    "exhentai.org",
    "www.exhentai.org",
]
ROUND = SourceSettings(eh_budget_capacity=1000, eh_budget_leak_per_min=10)
"""Round numbers for the arithmetic below."""


class Clock:
    def __init__(self):
        self.now = 2_000_000_000.0

    def __call__(self) -> float:
        return self.now


class FakeTime:
    """Monotonic and wall clocks that only `sleep` (or a test) moves, together."""

    def __init__(self):
        self.mono, self.wall = 1000.0, 2_000_000_000.0

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


@pytest.fixture
def fake(monkeypatch) -> FakeTime:
    clock = FakeTime()
    monkeypatch.setattr(source_http_module, "time", clock)
    return clock


def budgeted(fake: FakeTime, handler=None, settings: SourceSettings = ROUND, files=None) -> SourceHTTP:
    """A SourceHTTP that admits requests against the real budgets on the fake clock."""
    respond = handler or (lambda request: httpx.Response(200, content=b"<html>ok</html>"))
    http = SourceHTTP(settings, {}, transport=httpx.MockTransport(respond), clock=fake.time)
    if files is not None:
        http.persist(files.library)
    return http


def use_clock(monkeypatch, clock: FakeTime) -> None:
    """Workers, checks and the HTTP layer (halts and the bucket) share one fake clock."""
    for module in (downloads_module, source_checks_module, source_http_module):
        monkeypatch.setattr(module, "time", clock)


def test_budget_settings_default_validate_and_reject_the_old_intervals():
    settings = SourceSettings()
    assert (settings.eh_budget_capacity, settings.eh_budget_leak_per_min, settings.eh_max_concurrency) == (
        1100,
        13,
        4,
    )
    for field, valid, invalid in (
        ("eh_budget_capacity", (100, 3000), (99, 3001)),
        ("eh_budget_leak_per_min", (1, 60), (0, 61)),
        ("eh_max_concurrency", (1, 8), (0, 9)),
    ):
        for value in valid:
            assert getattr(SourceSettings.model_validate({field: value}), field) == value
        for value in invalid:
            with pytest.raises(ValidationError):
                SourceSettings.model_validate({field: value})
    # Unknown per-kind interval fields are rejected.
    for unknown in ({"eh_page_pace_seconds": 15}, {"eh_pace": 3}):
        with pytest.raises(ValidationError):
            SourceSettings.model_validate(unknown)


@pytest.mark.parametrize(
    "url, kind",
    [
        (API_URL, "api"),
        (EX_API_URL, "api"),
        (PAGE_URL, "page"),
        (PAGE_URL + "?hc=1", "page"),
        (PAGE_URL + "?p=2", "page"),
        ("https://e-hentai.org/s/0123456789/1-2", "page"),
        ("https://e-hentai.org/mpv/1/abcdef0123/", "page"),
        ("https://e-hentai.org/gallerytorrents.php?gid=1&t=abc", "page"),
        ("https://e-hentai.org/gallerypopups.php?gid=1&t=abc&act=addfav", "page"),
        ("https://e-hentai.org/home.php", "page"),
        ("https://forums.e-hentai.org/index.php", "page"),
        ("https://e-hentai.org/", "search"),
        ("https://exhentai.org", "search"),
        (SEARCH_URL, "search"),
        ("https://e-hentai.org/?next=12345", "search"),
        ("https://e-hentai.org/tag/female:glasses", "search"),
        ("https://e-hentai.org/uploader/someone?next=5", "search"),
        ("https://e-hentai.org/favorites.php", "search"),
        ("https://e-hentai.org/popular", "search"),
        ("https://exhentai.org/watched", "search"),
        ("https://e-hentai.org/toplist.php?tl=15", "search"),
        (EX_PAGE_URL, "archiver"),
        ("https://e-hentai.org/archiver.php?gid=1&token=abc", "archiver"),
    ],
)
def test_eh_urls_cost_their_kind(url, kind):
    assert eh_kind(url) == kind
    assert EH_COSTS[kind] == {"page": 1, "api": 1.5, "search": 4.5, "archiver": 0.5}[kind]


def test_the_bucket_drains_with_time():
    bucket = Bucket(level=100, updated=0)
    bucket.drain(60, 10)
    assert bucket.level == pytest.approx(90)
    bucket.drain(90, 10)
    assert bucket.level == pytest.approx(85)
    bucket.drain(10_000, 10)
    assert bucket.level == 0


@pytest.mark.parametrize(
    "remaining, concurrency, spacing",
    [
        # r >= 0.5: full speed; 0.1 <= r < 0.5: linear; r < 0.1: one at a time at the drain rate.
        (0.6, 4, {"api": 0, "page": 0, "search": 0}),
        (0.5, 4, {"api": 0, "page": 0, "search": 0}),
        (0.3, 2, {"api": 4.5, "page": 3, "search": 13.5}),
        (0.1, 1, {"api": 9, "page": 6, "search": 27}),
        (0.05, 1, {"api": 9, "page": 6, "search": 27}),
    ],
)
def test_concurrency_and_spacing_follow_the_remaining_share(remaining, concurrency, spacing):
    for kind, seconds in spacing.items():
        assert ladder(remaining, EH_COSTS[kind], 4, 10) == (concurrency, pytest.approx(seconds))
    # In the bucket: the ladder's concurrency caps requests in flight.
    bucket = Bucket(level=(1 - remaining) * 1000, updated=0, in_flight=concurrency)
    assert bucket.admit(1, now=0, capacity=1000, leak_per_min=0.001, max_concurrency=4) == POLL
    bucket.in_flight -= 1
    assert bucket.admit(1, now=0, capacity=1000, leak_per_min=0.001, max_concurrency=4) is None
    assert bucket.in_flight == concurrency


@pytest.mark.parametrize("remaining, gap", [(0.6, 0), (0.3, 3), (0.05, 6)])
def test_page_requests_start_apart_by_the_remaining_share(fake, remaining, gap):
    starts: list[float] = []

    def handler(request):
        starts.append(fake.mono)
        return httpx.Response(200, content=b"ok")

    http = budgeted(fake, handler)
    try:
        http.bucket.level = (1 - remaining) * 1000
        for _ in range(3):
            http.read("GET", PAGE_URL)
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
        # Pages spent meanwhile and the drain move r a little between the two gaps.
        assert gaps == [pytest.approx(gap, abs=0.1)] * 2
    finally:
        http.close()


def test_background_work_leaves_a_fifth_of_the_bucket_to_requests_made_by_hand(fake):
    http = budgeted(fake)
    try:
        http.bucket.level = 850  # r = 0.15
        with http.background(), pytest.raises(SourceError) as error:
            http.read("POST", API_URL)
        # 50 points over the 80% line drain in 300 s: too long to wait inside the request.
        assert (error.value.code, error.value.retry_after) == ("source_rate_limited", 300)
        assert http.bucket.level == 850
        http.read("POST", API_URL)  # a request made by hand still goes
        assert http.bucket.level == pytest.approx(851.5)
        http.bucket.level = 805
        started = fake.mono
        with http.background():
            http.read("POST", API_URL)  # 5 points over: waits 30 s, then goes
        assert fake.mono - started == pytest.approx(30, abs=0.01)
    finally:
        http.close()


def test_a_request_waits_for_room_in_the_bucket_or_defers_a_long_wait(fake):
    http = budgeted(fake)
    try:
        http.bucket.level = 999.5
        started = fake.mono
        http.read("GET", PAGE_URL)  # half a point over: 3 s
        assert fake.mono - started == pytest.approx(3, abs=0.01)
        assert http.bucket.level == pytest.approx(1000, abs=0.01)
        started = fake.mono
        http.read("GET", SEARCH_URL)  # 4.5 points over: 27 s
        assert fake.mono - started == pytest.approx(27, abs=0.01)
        http.bucket.level = 1020
        with pytest.raises(SourceError) as error:
            http.read("GET", PAGE_URL)  # 21 points over: 126 s
        assert (error.value.code, error.value.retry_after) == ("source_rate_limited", 126)
        assert http.bucket.level == pytest.approx(1020)
    finally:
        http.close()


def test_points_are_spent_before_sending_and_never_given_back(fake):
    def handler(request):
        if request.url.path == "/offline":
            raise httpx.ConnectError("down")
        if request.url.path == "/missing":
            return httpx.Response(404)
        if request.url.path == "/hop":
            return httpx.Response(302, headers={"location": API_URL})
        if request.url.path == "/to-cdn":
            return httpx.Response(302, headers={"location": "https://ehgt.org/t/1.jpg"})
        return httpx.Response(200, content=b"ok")

    http = budgeted(fake, handler)
    try:
        for path, code in (("/offline", "source_network"), ("/missing", "source_unavailable")):
            with pytest.raises(SourceError, match=code):
                http.read("GET", "https://e-hentai.org" + path)
        assert http.bucket.level == pytest.approx(2)
        http.read("GET", "https://e-hentai.org/hop")  # a page, then the API it redirects to
        assert http.bucket.level == pytest.approx(4.5)
        http.read("GET", "https://e-hentai.org/to-cdn")  # the CDN hop spends nothing
        assert http.bucket.level == pytest.approx(5.5)
        http.read("GET", "https://ehgt.org/t/2.jpg")
        assert http.bucket.level == pytest.approx(5.5) and http.bucket.in_flight == 0
    finally:
        http.close()


def test_eh_concurrency_is_capped_while_the_bucket_has_room():
    entered, release = threading.Semaphore(0), threading.Event()
    active, peak = [0], [0]
    guard = threading.Lock()

    def handler(request):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        entered.release()
        release.wait(5)
        with guard:
            active[0] -= 1
        return httpx.Response(200, content=b"ok")

    http = SourceHTTP(SourceSettings(eh_max_concurrency=2), {}, transport=httpx.MockTransport(handler))
    threads = [threading.Thread(target=http.read, args=("GET", PAGE_URL)) for _ in range(4)]
    try:
        for thread in threads:
            thread.start()
        assert entered.acquire(timeout=5) and entered.acquire(timeout=5)
        assert not entered.acquire(timeout=0.3)
        release.set()
        for thread in threads:
            thread.join(5)
        assert peak[0] == 2 and http.bucket.in_flight == 0
    finally:
        release.set()
        http.close()


def test_a_rate_limit_fills_the_bucket_and_shrinks_it_for_a_day(files, fake):
    http = budgeted(fake, lambda request: httpx.Response(200, content=BAN), SourceSettings(), files)
    vault = SourceVault(files.library)
    try:
        http.bucket.level = 330
        with pytest.raises(SourceError, match="source_rate_limited"):
            http.read("GET", PAGE_URL)
        state = http.budget()["ehentai"]
        assert (state["capacity"], state["leak_per_min"], state["level"]) == (737, 8.71, 737)
        assert state["penalty"] == 0.67 and state["penalty_until"] == fake.wall + PENALTY_SECONDS
        event = vault.public()["rate_events"][0]
        assert (event["kind"], event["reason"], event["penalty"]) == ("page", "ban", 0.67)
        assert event["level"] == pytest.approx(331) and event["remaining"] == pytest.approx(769 / 1100)
        penalties = []
        for _ in range(4):  # each ban within the day scales again, down to 0.3
            fake.advance(3600)
            with pytest.raises(SourceError, match="source_rate_limited"):
                http.read("POST", API_URL)
            penalties.append(http.budget()["ehentai"]["penalty"])
        assert penalties == [0.449, 0.301, 0.3, 0.3]
        assert http.budget()["ehentai"]["capacity"] == 330
        events = vault.public()["rate_events"]
        assert len(events) == 5 and [event["kind"] for event in events] == ["api"] * 4 + ["page"]
        fake.advance(PENALTY_SECONDS)  # a day without a rate limit lifts the penalty
        state = http.budget()["ehentai"]
        assert (state["capacity"], state["leak_per_min"], state["penalty"], state["penalty_until"]) == (
            1100,
            13,
            1,
            None,
        )
        fake.advance(PENALTY_SECONDS)
        with pytest.raises(SourceError, match="source_rate_limited"):
            http.read("GET", SEARCH_URL)  # a new period starts from 0.67 again
        assert http.budget()["ehentai"]["penalty"] == 0.67
    finally:
        http.close()


def test_budgets_are_saved_periodically_and_outlive_a_restart(files, fake):
    def stored():
        with files.library.connection() as db:
            return {row["site"]: dict(row) for row in db.execute("SELECT * FROM source_budget")}

    def handler(request):
        return httpx.Response(200, content=b"{}")

    http = budgeted(fake, handler, files=files)
    try:
        for _ in range(19):
            http.read("GET", PAGE_URL)
        assert stored() == {}
        http.read("GET", PAGE_URL)
        assert stored() == {}  # request admission wakes the persistence worker
        assert http.save_wake.is_set()
        http.save_if_dirty()
        assert stored()["ehentai"]["level"] == pytest.approx(20)
        fake.advance(11)
        http.read("GET", SEARCH_URL)
        http.save_if_dirty()
        assert stored()["ehentai"]["level"] == pytest.approx(20 - 11 / 6 + 4.5)
        http.read("GET", PAGE_URL)
        http.bucket.penalize(fake.wall)
    finally:
        http.close()  # closing saves too
    saved = stored()
    assert set(saved) == {"ehentai"}
    assert saved["ehentai"]["penalty"] == 0.67
    fake.advance(30)  # the server is off for 30 s
    restarted = budgeted(fake, handler, files=files)
    try:
        # The level drained while the server was off (at the penalized 6.7 points a minute).
        assert restarted.bucket.level == pytest.approx(saved["ehentai"]["level"] - 6.7 * 30 / 60)
        assert restarted.budget()["ehentai"]["penalty"] == 0.67
    finally:
        restarted.close()


def test_budget_settings_and_state_over_http(api):
    current = api.get("/api/sources/settings").json()
    assert current["settings"]["eh_budget_capacity"] == 1100
    assert current["rate_events"] == []
    assert current["budget"]["ehentai"]["remaining"] == 1
    assert set(current["budget"]) == {"ehentai"}
    rejected = api.put(
        "/api/sources/settings",
        params={"expected_revision": 0},
        json={"settings": {**current["settings"], "eh_max_concurrency": 9}},
    )
    assert rejected.status_code == 422
    # The old intervals are unknown keys like any other.
    stale = {"settings": {**current["settings"], "eh_page_pace_seconds": 15}}
    rejected = api.put("/api/sources/settings", params={"expected_revision": current["revision"]}, json=stale)
    assert rejected.status_code == 422 and rejected.json()["error"] == "invalid_input"
    body = {"settings": {**current["settings"], "eh_budget_capacity": 600}}
    saved = api.put("/api/sources/settings", params={"expected_revision": current["revision"]}, json=body)
    assert saved.status_code == 200
    assert saved.json()["settings"]["eh_budget_capacity"] == 600
    usage = api.get("/api/sources/budget").json()
    assert usage["budget"]["ehentai"]["capacity"] == 600 and usage["cooldowns"] == {}


@pytest.mark.parametrize("host", EH_HOSTS)
def test_every_eh_host_spends_the_bucket_and_is_ban_checked(host, fake):
    assert source_site(f"https://{host}/") in EH_SITES
    calls: list[str] = []

    def handler(request):
        calls.append(request.url.host)
        return httpx.Response(200, content=BAN if len(calls) == 2 else b"ok")

    http = budgeted(fake, handler)
    try:
        http.read("GET", f"https://{host}/a")
        assert http.bucket.level == pytest.approx(EH_COSTS["page"])
        with pytest.raises(SourceError, match="source_rate_limited"):
            http.read("GET", f"https://{host}/b")
        assert set(http.cooldowns()) == set(EH_SITES)
        with pytest.raises(SourceError, match="source_rate_limited"):
            http.read("GET", "https://e-hentai.org/c")
        assert len(calls) == 2
    finally:
        http.close()


def test_each_redirect_hop_spends_the_bucket_and_honours_a_halt(fake):
    hosts: list[str] = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "e-hentai.org":
            return httpx.Response(302, headers={"location": "https://g.e-hentai.org/g/1/a/"})
        if request.url.host == "cdn.example":
            return httpx.Response(302, headers={"location": "https://e-hentai.org/x"})
        return httpx.Response(200, content=b"ok")

    http = budgeted(fake, handler)
    try:
        assert http.read("GET", "https://e-hentai.org/g/1/a/") == b"ok"
        assert hosts == ["e-hentai.org", "g.e-hentai.org"]
        assert http.bucket.level == pytest.approx(2 * EH_COSTS["page"])
        http._halt(EX_PAGE_URL, "http_429", 60)
        hosts.clear()
        # A CDN host redirecting into the halted group stops before the second hop.
        with pytest.raises(SourceError, match="source_rate_limited"):
            http.read("GET", "https://cdn.example/redirect")
        assert hosts == ["cdn.example"]
    finally:
        http.close()


def test_too_fast_halts_escalate_on_repeats_and_reset_later(fake):
    http = budgeted(fake, lambda r: httpx.Response(200, content=TOO_FAST), SourceSettings())
    seen = []
    try:
        for advance in (
            0,
            TOO_FAST_SECONDS + 1,
            2 * TOO_FAST_SECONDS + 1,
            4 * TOO_FAST_SECONDS + STRIKE_WINDOW,
        ):
            fake.advance(advance)
            with pytest.raises(SourceError) as caught:
                http.read("GET", "https://e-hentai.org/")
            seen.append(caught.value.retry_after)
        assert seen == [TOO_FAST_SECONDS, 2 * TOO_FAST_SECONDS, 4 * TOO_FAST_SECONDS, TOO_FAST_SECONDS]
        # Each repeat doubles the halt, never beyond MAX_HALT.
        for _ in range(8):
            assert http._halt(PAGE_URL, "too_fast") <= MAX_HALT
    finally:
        http.close()


def eh_queue(files, tmp_path, clock: FakeTime, handler):
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler), clock=clock.time)
    return Downloads(files, http=http)


def test_persisted_halt_survives_restart_and_downloads_resume_after_it(files, tmp_path, monkeypatch):
    clock = FakeTime()
    use_clock(monkeypatch, clock)
    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    banned = [True]

    def handler(request):
        if banned[0] and request.url.host.endswith("e-hentai.org"):
            return httpx.Response(200, content=BAN)
        return remote(request)

    first = eh_queue(files, tmp_path, clock, handler)
    job = first.create(EH)
    assert download_step(first)
    waiting = first.get(job["id"])
    assert waiting["state"] == "queued" and waiting["error_code"] == "source_rate_limited"
    assert waiting["attempts"] == 0 and waiting["retry_at"] == int(clock.wall) + 4 * 60 + 9 + 5
    first.close()
    with files.library.connection() as db:
        halt = db.execute("SELECT * FROM source_halt").fetchone()
    assert (halt["site"], halt["reason"], halt["until"]) == ("ehentai", "ban", waiting["retry_at"])

    banned[0] = False
    remote.requests.clear()
    served = eh_queue(files, tmp_path, clock, handler)
    assert served.http.cooldowns() == {"ehentai": halt["until"], "exhentai": halt["until"]}
    served.start()
    served.close()
    # The restart parks interrupted work but keeps the job that waits for the halt.
    assert served.get(job["id"])["state"] == "queued"
    restarted = eh_queue(files, tmp_path, clock, handler)
    try:
        paused = restarted.create("https://exhentai.org/g/101/abcdef/")
        restarted.action(paused["id"], "pause", paused["revision"])
        resumed = restarted.action(paused["id"], "resume", restarted.get(paused["id"])["revision"])
        assert resumed["state"] == "queued" and resumed["retry_at"] == halt["until"]
        assert resumed["error_code"] == "source_rate_limited"
        assert not download_step(restarted) and remote.requests == []
        with pytest.raises(SourceError, match="source_rate_limited"):
            restarted.http.read("GET", "https://api.e-hentai.org/api.php")

        clock.advance(halt["until"] + 1 - clock.wall)
        assert restarted.http.cooldowns() == {}
        while download_step(restarted):
            pass
        assert restarted.get(job["id"])["state"] == "needs_approval"
        assert restarted.get(paused["id"])["state"] == "needs_approval"
        assert remote.requests
    finally:
        restarted.http.close()


def local_with_source(files, tmp_path):
    path = make_zip(tmp_path / "local.zip")
    native_file(
        path, Metadata(title="Local", source=Source(site="ehentai", gallery_id=100, gallery_token="abcdef"))
    )
    return files.import_file(path).book


def queue_check(files, book_id: str) -> None:
    with files.library.transaction() as db:
        db.execute(
            "INSERT INTO source_check(book_id,state,updated_at) VALUES(?,'queued',0) ON CONFLICT(book_id) "
            "DO UPDATE SET state='queued',generation=generation+1",
            (book_id,),
        )


def test_halted_source_check_survives_restart_and_runs_after_the_halt(files, tmp_path, monkeypatch):
    clock = FakeTime()
    use_clock(monkeypatch, clock)
    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    first = eh_queue(files, tmp_path, clock, remote)
    book = local_with_source(files, tmp_path)
    queue_check(files, book.id)
    until = int(clock.wall) + 600
    first.http._halt(PAGE_URL, "http_429", 600)
    assert not first.checks.step()
    check = first.checks.get(book.id)
    assert check and (check["state"], check["error_code"], check["retry_at"]) == (
        "queued",
        "source_rate_limited",
        until,
    )
    assert remote.requests == []

    restarted = eh_queue(files, tmp_path, clock, remote)
    restarted.checks.recover()
    assert (restarted.checks.get(book.id) or {}).get("state") == "queued"
    assert not restarted.checks.step()
    # After the halt the check waits for its background share: the halt filled the bucket.
    clock.advance(until + 600 - clock.wall)
    assert restarted.checks.step()
    check = restarted.checks.get(book.id)
    assert check and check["state"] in {"done", "needs_review"} and check["error_code"] is None
    assert remote.requests
    first.http.close()
    restarted.http.close()


def test_background_requests_yield_to_waiting_downloads():
    order: list[str] = []

    def handler(request):
        order.append(request.url.path)
        return httpx.Response(200, content=b"ok")

    # A quarter of the bucket left spaces page starts about 0.6 s apart: the third download must
    # queue before the second one's turn, and waiting on the queue (not fixed sleeps) keeps the
    # order deterministic on slow runners.
    settings = SourceSettings(eh_budget_capacity=1000, eh_budget_leak_per_min=60)
    http = SourceHTTP(settings, {}, transport=httpx.MockTransport(handler))
    with http.lock:
        http.bucket.level, http.bucket.updated = 750, time.monotonic()

    def foreground(name):
        http.read("GET", f"https://e-hentai.org/{name}")

    def background():
        with http.background():
            http.read("GET", "https://api.e-hentai.org/check")

    def queued(count):
        deadline = time.monotonic() + 0.5
        while http.waiting != count:
            assert time.monotonic() < deadline, f"expected {count} queued downloads"
            time.sleep(0.005)

    try:
        foreground("first")
        threads = [threading.Thread(target=foreground, args=("second",))]
        threads[0].start()
        queued(1)
        threads.append(threading.Thread(target=background))
        threads[1].start()
        time.sleep(0.05)
        threads.append(threading.Thread(target=foreground, args=("third",)))
        threads[2].start()
        queued(2)
        for thread in threads:
            thread.join(5)
        # The check started before the third download request and still went last.
        assert order == ["/first", "/second", "/third", "/check"]
    finally:
        http.close()


def test_background_checks_skip_comments_unless_asked(files, tmp_path):
    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote))
    queue = Downloads(files, http=http)
    try:
        book = local_with_source(files, tmp_path)
        queue_check(files, book.id)
        assert queue.checks.step()
        assert [request.url.path for request in remote.requests] == ["/api.php"]
        remote.requests.clear()
        http.apply(SourceSettings(background_comments=True), {})
        queue_check(files, book.id)
        assert queue.checks.step()
        assert [request.url.path for request in remote.requests] == ["/api.php", "/g/100/abcdef/"]
    finally:
        queue.close()


def test_imports_do_not_queue_source_checks(files, tmp_path):
    local_with_source(files, tmp_path)
    for index in range(3):
        path = make_zip(inbox(files) / f"scan-{index}.zip", [f"scan-{index}.png"])
        native_file(
            path, Metadata(source=Source(site="ehentai", gallery_id=300 + index, gallery_token="abc"))
        )
    scanner = Scanner(files)
    assert finish_scan(scanner, scanner.start()["id"])["counts"]["indexed"] == 3
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM book").fetchone()[0] == 4
        assert db.execute("SELECT count(*) FROM source_check").fetchone()[0] == 0


def test_a_download_records_its_source_check_for_the_periodic_refresh(queue):
    service, _ = queue
    done = finish_download(service, ready(service))
    assert done["state"] == "done"
    check = service.checks.get(done["book_id"])
    assert check and check["state"] == "done"


def test_too_fast_warning_cools_both_eh_sites_without_loosening_the_ban_match():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=TOO_FAST)

    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(SourceError, match="source_rate_limited") as caught:
            client.read("GET", "https://e-hentai.org/?f_search=x")
        assert caught.value.retry_after == TOO_FAST_SECONDS
        assert set(client.cooldowns()) == {"ehentai", "exhentai"}
        with pytest.raises(SourceError, match="source_rate_limited"):
            client.read("GET", "https://exhentai.org/g/1/ab/")
        assert len(calls) == 1
        # CDN pages are not E-Hentai's warning.
    finally:
        client.close()
    assert ban_seconds(TOO_FAST) is None


def test_eh_and_exhentai_requests_share_one_bucket(fake):
    http = budgeted(fake)
    try:
        http.read("GET", "https://e-hentai.org/g/1/abc/")
        http.read("GET", "https://exhentai.org/g/2/abc/")
        assert http.bucket.level == pytest.approx(2 * EH_COSTS["page"])
        # Archive hosts spend no source bucket budget.
        http.read("GET", "https://archive.example/file")
        assert http.bucket.level == pytest.approx(2 * EH_COSTS["page"])
    finally:
        http.close()


def test_concurrent_halts_persist_in_memory_order(files, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager

    http = SourceHTTP(SourceSettings(), {}, clock=Clock())
    http.persist(files.library)
    first_waiting, release_first, second_writing = (threading.Event() for _ in range(3))
    transaction = files.library.transaction

    @contextmanager
    def delayed_transaction():
        if not first_waiting.is_set():
            first_waiting.set()
            assert release_first.wait(5)
        else:
            second_writing.set()
        with transaction() as db:
            yield db

    monkeypatch.setattr(files.library, "transaction", delayed_transaction)
    try:
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(http._halt, PAGE_URL, "too_fast")
            assert first_waiting.wait(5)
            second = pool.submit(http._halt, PAGE_URL, "ban", 7200)
            try:
                # Persistence does not block configuration; both writers save the latest halt.
                assert second_writing.wait(1)
                assert http.configuration_lock.acquire(timeout=1)
                http.configuration_lock.release()
                # A job holding a database transaction can still read cooldowns.
                assert http.cooldowns()["ehentai"] > http.clock()
            finally:
                release_first.set()
            first.result(timeout=5)
            second.result(timeout=5)
        halt = http.halts["ehentai"]
        with files.library.connection() as db:
            saved = db.execute("SELECT * FROM source_halt").fetchone()
            events = db.execute("SELECT kind,reason,penalty FROM source_rate_event ORDER BY id").fetchall()
        assert (saved["until"], saved["reason"], saved["strikes"]) == (halt.until, "ban", 1)
        assert halt.until == http.clock() + 7200
        # A ban outlasting the warning's halt only extends it: no second penalty.
        assert [tuple(row) for row in events] == [("page", "too_fast", 0.67)]
        assert http.bucket.penalty == 0.67
    finally:
        release_first.set()
        http.close()


def test_responses_in_flight_together_count_as_one_rate_limit(files):
    """Two requests answered by the same ban or warning shrink the bucket once."""
    clock = Clock()
    http = SourceHTTP(SourceSettings(), {}, clock=clock)
    http.persist(files.library)
    try:
        for reason in ("too_fast", "too_fast", "ban", "http_429"):
            http._halt(PAGE_URL, reason, 60)
        halt = http.halts["ehentai"]
        assert (halt.reason, halt.strikes, halt.until) == ("too_fast", 1, clock.now + 600)
        assert http.bucket.penalty == 0.67
        with files.library.connection() as db:
            assert db.execute("SELECT count(*) FROM source_rate_event").fetchone()[0] == 1
        clock.now += 601  # the next warning after the halt ends is a new strike
        http._halt(PAGE_URL, "too_fast")
        assert http.halts["ehentai"].strikes == 2
        assert http.bucket.penalty == pytest.approx(0.67**2)
    finally:
        http.close()


def test_a_long_wait_for_the_bucket_requeues_downloads_and_checks_until_they_run(
    files, tmp_path, fake, monkeypatch
):
    """Deferred work needs nobody: it waits for its retry_at and then runs to the end by itself."""
    monkeypatch.setattr(downloads_module, "time", fake)
    monkeypatch.setattr(source_checks_module, "time", fake)
    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    http = SourceHTTP(ROUND, {}, transport=httpx.MockTransport(remote), clock=fake.time)
    service = Downloads(files, http=http)
    try:
        book = local_with_source(files, tmp_path)
        queue_check(files, book.id)
        http.bucket.level = 1100  # over the 1000-point capacity: minutes away from any room
        job = service.create(EH)
        assert download_step(service)
        assert not service.checks.step()
        waiting = service.get(job["id"])
        check = service.checks.get(book.id) or {}
        assert (waiting["state"], waiting["error_code"], waiting["attempts"]) == (
            "queued",
            "source_rate_limited",
            0,
        )
        assert (check["state"], check["error_code"]) == ("queued", "source_rate_limited")
        # The check is background work: it waits for the bucket to drain to 80%, later than the job.
        assert fake.wall + 600 < waiting["retry_at"] + 10 < check["retry_at"]
        assert remote.requests == []
        assert not download_step(service) and not service.checks.step()
        fake.advance(waiting["retry_at"] - fake.wall + 1)
        while download_step(service):
            pass
        assert service.get(job["id"])["state"] == "needs_approval"
        fake.advance(max(0, check["retry_at"] - fake.wall) + 1)
        while service.checks.step():
            pass
        assert (service.checks.get(book.id) or {})["state"] in {"done", "needs_review"}
        assert (service.checks.get(book.id) or {})["error_code"] is None
    finally:
        service.close()
