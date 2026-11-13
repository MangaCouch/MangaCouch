import httpx
import pytest

from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceError, SourceHTTP, public_url


@pytest.mark.parametrize(
    "body,content_type",
    [
        (b'{"title":"Sad Panda Stories Opening Pages Too Fast"}', "application/json"),
        (b'{"title":"Sad Panda Stories"}', "text/plain"),
        (
            b'<div class="gm"><h1>Sad Panda Stories</h1><div id="cdiv">Opening Pages Too Fast</div>',
            "text/html",
        ),
    ],
)
def test_gallery_words_do_not_halt_sources(body, content_type):
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=body, headers={"Content-Type": content_type})
        ),
    )
    assert http.read("GET", "https://exhentai.org/g/1/abcdef/") == body
    assert not http.cooldowns()
    http.close()


@pytest.mark.parametrize("url", ["https://127.0.0.1/x", "https://[::1]/x", "https://localhost/x"])
def test_proxy_does_not_allow_private_literals(url):
    with pytest.raises(SourceError, match="source_private_address"):
        public_url(url, resolve=False)


@pytest.mark.parametrize("url", ["https://[bad/x", "https://example.org:abc/x"])
def test_malformed_urls_have_source_error(url):
    with pytest.raises(SourceError, match="unsafe_source_url"):
        public_url(url, resolve=False)


@pytest.mark.parametrize(
    "status,body,code",
    [
        (503, b"maintenance", "source_unavailable"),
        (403, b"forbidden", "source_login_required"),
        (503, b'<div id="cf-chl-test">', "verification_required"),
        (302, b"no location", "source_protocol"),
    ],
)
def test_only_actual_challenges_require_verification(status, body, code):
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, content=body, headers={"Server": "cloudflare"})
        ),
    )
    with pytest.raises(SourceError, match=code):
        http.read("GET", "https://e-hentai.org/g/1/abcdef/")
    http.close()


def test_admission_never_saves_inline(monkeypatch):
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok")),
    )
    monkeypatch.setattr(http, "_save", lambda **kw: pytest.fail("inline write"))
    http.read("GET", "https://e-hentai.org/g/1/abcdef/")
    assert http.unsaved == 1


def test_peer_must_match_checked_address(monkeypatch):
    import socket
    from types import SimpleNamespace

    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [(2, 1, 6, "", ("8.8.8.8", 443))])
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=b"ok",
                extensions={
                    "network_stream": SimpleNamespace(get_extra_info=lambda name: ("127.0.0.1", 443))
                },
            )
        ),
    )
    http.transport = None
    with pytest.raises(SourceError, match="source_private_address"):
        http.read("GET", "https://example.org/file")
    http.close()


def test_prefetch_keeps_unavailable_gallery_on_requested_site(queue):
    from mangacouch.models import Source
    from mangacouch.source_adapters import Sources

    service, remote = queue
    calls = []

    def respond(request):
        import json

        data = json.loads(request.content)
        calls.append((request.url.host, len(data["gidlist"])))
        if request.url.host != "exhentai.org":
            return httpx.Response(
                200, json={"gmetadata": [{"gid": gid, "error": "unavailable"} for gid, _ in data["gidlist"]]}
            )
        return remote(request)

    service.http.client.close()
    service.http.client = httpx.Client(transport=httpx.MockTransport(respond))
    sources = Sources(service.http)
    batch = [Source(site="ehentai", gallery_id=n, gallery_token="abcdef") for n in range(1, 26)]
    sources.prefetch(batch)
    for source in batch:
        with pytest.raises(SourceError, match="gallery_unavailable"):
            sources.preferred(source, comments=False)
    assert calls == [("api.e-hentai.org", 25)]


def test_fifo_waiter_times_out_even_when_not_at_head(monkeypatch):
    from collections import deque

    from test_source_halts import FakeTime

    from mangacouch import source_http

    clock = FakeTime()
    monkeypatch.setattr(source_http, "time", clock)
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    http.queue = deque([object()])
    with pytest.raises(SourceError, match="source_rate_limited"):
        http.read("GET", "https://e-hentai.org/g/1/abcdef/")
    assert 60 <= clock.mono - 1000 < 61
    assert len(http.queue) == 1
    http.close()


def test_penalty_floor_does_not_permanently_defer_search():
    from mangacouch.source_limits import MAX_WAIT, Bucket

    bucket = Bucket(level=269, updated=0, started=0)
    wait = bucket.admit(4.5, now=0, capacity=330, leak_per_min=3.9, max_concurrency=4)
    assert wait is not None and wait < MAX_WAIT
    assert bucket.admit(4.5, now=wait, capacity=330, leak_per_min=3.9, max_concurrency=4) is None
