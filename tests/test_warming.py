from __future__ import annotations

from fastapi.testclient import TestClient
from helpers import CSRF, ORIGIN, PASSCODE, login, make_zip

from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app
from mangacouch.image_cache import ImageCache
from mangacouch.warming import Warmer


def books(files, tmp_path, count):
    names = [[f"{index}-{n}.png" for n in range(2)] for index in range(count)]
    archives = [make_zip(tmp_path / f"book-{index}.zip", pages) for index, pages in enumerate(names)]
    return [files.import_file(archive).book for archive in archives]


def run(warmer: Warmer) -> int:
    steps = 0
    while warmer.step():
        steps += 1
    return steps


def test_covers_are_rendered_once_in_batches_with_progress(files, tmp_path, monkeypatch):
    imported = books(files, tmp_path, 6)
    cache = ImageCache(tmp_path / "cache")
    warmer = Warmer(files, cache)
    assert warmer.summary()["state"] == "idle"
    assert warmer.step()
    progress = warmer.summary()
    assert progress["state"] == "active" and progress["done"] == 4 and progress["total"] == 6
    assert run(warmer) == 1
    finished = warmer.summary()
    assert finished["state"] == "idle" and finished["done"] == finished["rendered"] == 6
    assert all(cache.has(files.cover_key(book)) for book in imported)
    # Nothing changed: the next step costs a count query, not a pass.
    assert not warmer.step()
    rendered = []
    original = files.cover_bytes
    monkeypatch.setattr(files, "cover_bytes", lambda *a, **k: rendered.append(a) or original(*a, **k))
    # An explicit request walks the library again, finding every cover cached already.
    warmer.request()
    run(warmer)
    assert rendered == [] and warmer.summary()["rendered"] == 0 and warmer.summary()["done"] == 6
    # A new book starts a pass that renders only that one.
    added = files.import_file(make_zip(tmp_path / "new.zip", ["new.png"])).book
    run(warmer)
    assert [call[0] for call in rendered] == [added.id]


def test_an_unreadable_book_is_counted_and_does_not_stall_the_pass(files, tmp_path):
    first, second = books(files, tmp_path, 2)
    (files.root.path / first.content.relative_path).write_bytes(b"gone")
    warmer = Warmer(files, ImageCache(tmp_path / "cache"))
    run(warmer)
    summary = warmer.summary()
    assert summary["done"] == 2 and summary["failed"] == 1 and summary["rendered"] == 1
    assert warmer.cache.has(files.cover_key(second))


def test_cover_routes_report_cache_budget_and_start_a_pass(files, tmp_path):
    books(files, tmp_path, 2)
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    settings = HTTPSettings(public_origin=ORIGIN, thumbnail_cache_bytes=8 * 1024**2)
    with TestClient(create_app(files, settings, auth=auth), base_url=ORIGIN) as client:
        login(client)
        status = client.get("/api/cache").json()
        assert status["cache"]["budget_bytes"] == 8 * 1024**2
        for kind in ("covers", "thumbnails"):
            assert set(status[kind]) >= {"state", "done", "total", "rendered", "failed", "full"}
            assert client.post(f"/api/cache/{kind}/warm").status_code == 403
            assert client.post(f"/api/cache/{kind}/warm", headers=CSRF).status_code == 200
        assert client.post("/api/cache/pages/warm", headers=CSRF).status_code == 422
        assert {"covers", "thumbnails"} <= set(client.get("/api/tasks").json())
        assert "cache" in client.get("/api/settings/summary").json()


def test_thumbnails_cover_every_page_across_steps(files, tmp_path):
    long = files.import_file(make_zip(tmp_path / "long.zip", [f"{n}.png" for n in range(7)])).book
    short = books(files, tmp_path, 1)[0]
    cache = ImageCache(tmp_path / "cache")
    warmer = Warmer(files, cache, "thumbnails")
    assert warmer.step()
    # Four renders per step: the long book is resumed mid-way instead of finished at once.
    assert warmer.summary()["done"] == 0 and warmer.summary()["rendered"] == 4
    run(warmer)
    summary = warmer.summary()
    assert summary["done"] == summary["total"] == 2 and summary["rendered"] == 9
    assert not summary["full"] and summary["failed"] == 0
    for book in (long, short):
        assert all(cache.has(files.cover_key(book, index)) for index in range(book.content.page_count))
    # Covers share the first page's entry, so a cover pass afterwards renders nothing.
    covers = Warmer(files, cache, "covers")
    run(covers)
    assert covers.summary()["rendered"] == 0 and covers.summary()["done"] == 2


def test_thumbnail_pass_stops_before_it_would_evict(files, tmp_path, monkeypatch):
    books(files, tmp_path, 3)
    cache = ImageCache(tmp_path / "cache")
    monkeypatch.setattr(cache, "stored_bytes", lambda: cache.data_bytes)
    warmer = Warmer(files, cache, "thumbnails")
    assert not warmer.step()
    summary = warmer.summary()
    assert summary["state"] == "idle" and summary["full"] and summary["rendered"] == 0
    # Until something changes it stays stopped rather than retrying every step.
    assert not warmer.step()
