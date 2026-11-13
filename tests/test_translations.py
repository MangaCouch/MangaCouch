"""Updating the EhTagTranslation database over HTTP, without reaching the network."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest

from mangacouch import translations
from mangacouch.source_config import SourceSettings, SourceVault

DATABASE = {
    "version": 7,
    "head": {"sha": "fixture-revision"},
    "data": [
        {"namespace": "rows", "data": {}},
        {"namespace": "parody", "data": {"blue archive": {"name": "碧蓝档案"}}},
        {"namespace": "artist", "data": {"example": {"name": "示例"}}},
    ],
}


class Release:
    """The release download, answered by `handler`; records how each download was made."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]):
        self.calls: list[dict] = []
        self.requests: list[httpx.Request] = []

        def recorded(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return handler(request)

        @contextmanager
        def stream(
            method: str, url: str, *, proxy=None, trust_env=True, **options
        ) -> Iterator[httpx.Response]:
            self.calls.append({"method": method, "url": url, "proxy": proxy, "trust_env": trust_env})
            with (
                httpx.Client(transport=httpx.MockTransport(recorded)) as client,
                client.stream(method, url, **options) as response,
            ):
                yield response

        monkeypatch.setattr(translations.httpx, "stream", stream)


class Chunks(httpx.SyncByteStream):
    """A response body sent in 1 MiB chunks; `sent` counts the chunks the reader asked for."""

    def __init__(self, count: int):
        self.count, self.sent = count, 0

    def __iter__(self):
        for _ in range(self.count):
            self.sent += 1
            yield b" " * 1024**2


@pytest.fixture
def manager(client, auth) -> dict[str, str]:
    _, key = auth.create_key("management", "translations")
    return {"Authorization": f"Bearer {key}"}


def test_update_imports_the_release_and_reports_its_status(client, files, manager, monkeypatch):
    release = Release(monkeypatch, lambda request: httpx.Response(200, json=DATABASE))
    assert client.get("/api/translations", headers=manager).json() == {
        "updated_at": None,
        "revision": None,
        "count": 0,
    }
    response = client.post("/api/translations/update", headers=manager)
    assert response.status_code == 200, response.text
    status = response.json()
    assert status["revision"] == "fixture-revision" and status["count"] == 2 and status["updated_at"]
    assert client.get("/api/translations", headers=manager).json() == status
    assert release.calls == [
        {"method": "GET", "url": translations.DATABASE_URL, "proxy": None, "trust_env": False}
    ]
    with files.library.connection() as db:
        rows = db.execute("SELECT namespace,value,translation FROM tag_translation ORDER BY 1").fetchall()
    assert [tuple(row) for row in rows] == [
        ("artist", "example", "示例"),
        ("parody", "blue archive", "碧蓝档案"),
    ]


def test_update_goes_through_the_configured_source_proxy(client, files, manager, monkeypatch):
    SourceVault(files.library).save(SourceSettings(), {"proxy": "socks5h://proxy.example:1080"}, 0)
    release = Release(monkeypatch, lambda request: httpx.Response(200, json=DATABASE))
    assert client.post("/api/translations/update", headers=manager).status_code == 200
    assert [call["proxy"] for call in release.calls] == ["socks5h://proxy.example:1080"]
    assert release.calls[0]["trust_env"] is False


@pytest.mark.parametrize("body", [b"{not json", b"\xff\xfe", json.dumps(["version", 7]).encode()])
def test_update_rejects_a_release_that_is_not_a_translation_database(
    client, files, manager, monkeypatch, body
):
    Release(monkeypatch, lambda request: httpx.Response(200, content=body))
    response = client.post("/api/translations/update", headers=manager)
    assert response.status_code == 422 and response.json()["error"] == "translation_unsupported"
    assert client.get("/api/translations", headers=manager).json()["count"] == 0


def test_update_stops_reading_a_release_over_16_mib(client, manager, monkeypatch):
    body = Chunks(64)
    Release(monkeypatch, lambda request: httpx.Response(200, stream=body))
    response = client.post("/api/translations/update", headers=manager)
    assert response.status_code == 422 and response.json()["error"] == "translation_unsupported"
    assert response.json()["message"] == "Translation database is too large"
    # Reading stops at the first chunk past the cap instead of buffering the whole release.
    assert body.sent == 17


def test_update_that_cannot_download_the_release_reports_the_source_unavailable(client, manager, monkeypatch):
    Release(monkeypatch, lambda request: httpx.Response(404))
    response = client.post("/api/translations/update", headers=manager)
    assert response.status_code == 502 and response.json()["error"] == "source_unavailable"
    assert client.get("/api/translations", headers=manager).json()["count"] == 0
