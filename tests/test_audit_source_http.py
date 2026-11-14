"""Unsent stale credentials and unexpected compressed source responses."""

import gzip
import threading
import time

import httpx
import pytest

from mangacouch.downloads import Downloads
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import NotSent, SourceError, SourceHTTP

URL = "https://e-hentai.org/g/100/abcdef/"


@pytest.mark.parametrize("reset", [True, False])
def test_queued_request_does_not_send_old_configuration(files, reset):
    sent, errors = [], []
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(200, json={"ok": True})
        ),
    )
    queue = Downloads(files, http=http)
    queue.save_settings(queue.settings, {"eh_cookie": "fixture=old"}, 0)
    with http.lock:
        http.bucket.in_flight = http.settings.eh_max_concurrency

    def request():
        try:
            http.read("GET", URL)
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=request)
    try:
        thread.start()
        deadline = time.monotonic() + 5
        while http.waiting != 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert http.waiting == 1
        if reset:
            result = queue.reset_credentials()
            assert result["settings"]["online_paused"] and not result["configured"]["eh_cookie"]
        else:
            http.apply(http.settings, {"eh_cookie": "fixture=new"})
        # A stale waiter exits even before any budget becomes free.
        thread.join(5)
        assert not thread.is_alive() and not sent and len(errors) == 1
        if reset:
            assert isinstance(errors[0], SourceError) and errors[0].unsent
        else:
            assert isinstance(errors[0], NotSent)
    finally:
        with http.lock:
            http.bucket.in_flight = 0
        thread.join(5)
        queue.close()


@pytest.mark.parametrize("status", [200, 500])
def test_compressed_responses_are_rejected_before_reading(status):
    class Compressed(httpx.SyncByteStream):
        read = False

        def __iter__(self):
            self.read = True
            yield gzip.compress(b"x" * 32 * 1024**2)

    body = Compressed()
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, headers={"Content-Encoding": "gzip"}, stream=body)
        ),
    )
    try:
        with pytest.raises(SourceError, match="source_protocol"):
            http.read("GET", URL, limit=1024)
        assert not body.read
    finally:
        http.close()


def test_reset_after_admission_and_before_send_is_unsent(monkeypatch):
    sent = []
    http = SourceHTTP(
        SourceSettings(),
        {},
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(200)),
    )
    admit = http._admit

    def reset_before_send(url, cancelled):
        release = admit(url, cancelled)
        http.apply(SourceSettings(online_paused=True), {})
        return release

    monkeypatch.setattr(http, "_admit", reset_before_send)
    try:
        with pytest.raises(SourceError) as error:
            http.read("GET", URL)
        assert error.value.unsent and not sent
        assert http.bucket.in_flight == 0
    finally:
        http.close()


def test_configuration_change_during_redirect_stops_next_hop():
    sent = []

    class Redirect(httpx.SyncByteStream):
        def __iter__(self):
            yield b""

        def close(self):
            http.apply(SourceSettings(online_paused=True), {})

    def respond(request):
        sent.append(request)
        return httpx.Response(302, headers={"Location": URL + "?next=1"}, stream=Redirect())

    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(SourceError):
            http.read("GET", URL)
        assert len(sent) == 1
    finally:
        http.close()
