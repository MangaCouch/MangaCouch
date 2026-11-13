from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path
from tempfile import SpooledTemporaryFile

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import (
    ORIGIN,
    PASSCODE,
    make_zip,
)
from source_fixtures import SourceFixture
from starlette import formparsers as forms

from mangacouch import api_scans
from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings, save_http_settings
from mangacouch.database import Library
from mangacouch.downloads import Downloads
from mangacouch.files import LibraryRoot
from mangacouch.http import create_app
from mangacouch.image_cache import ImageCache
from mangacouch.ingest import FileLibrary
from mangacouch.maintenance import Maintenance
from mangacouch.sidecars import SidecarSync
from mangacouch.source_adapters import Sources
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceHTTP


@pytest.fixture(autouse=True)
def sources_stay_offline(monkeypatch):
    initialize = SourceHTTP.__init__

    def offline(self, settings, secrets, *, transport=None, **options):
        if transport is None:
            transport = httpx.MockTransport(lambda request: httpx.Response(503))
        initialize(self, settings, secrets, transport=transport, **options)

    monkeypatch.setattr(SourceHTTP, "__init__", offline)


@pytest.fixture(autouse=True)
def image_caches_are_closed(monkeypatch):
    """Close every ImageCache a test opened (reopened caches included) so no SQLite handle leaks."""
    opened: list[ImageCache] = []
    initialize = ImageCache.__init__

    def tracked(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        opened.append(self)

    monkeypatch.setattr(ImageCache, "__init__", tracked)
    yield
    for cache in opened:
        with suppress(Exception):
            cache.close()


@pytest.fixture(autouse=True)
def windows_symlink_privilege(monkeypatch):
    import os
    from pathlib import Path

    if os.name != "nt":
        return
    original = Path.symlink_to

    def symlink(self, *args, **kwargs):
        try:
            return original(self, *args, **kwargs)
        except OSError as error:
            if error.winerror == 1314:
                pytest.skip("Creating symlinks requires Windows Developer Mode or elevated privileges")
            raise

    monkeypatch.setattr(Path, "symlink_to", symlink)


@pytest.fixture
def files(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    library = Library.initialize(tmp_path / "library.sqlite")
    # As in a running server: without a retained connection, every close checkpoints and removes
    # the WAL, which makes tests that open many connections very slow on Windows.
    library.keep_alive()
    LibraryRoot.bind(library, root)
    service = FileLibrary(library, root)
    yield service
    service.page_cache.close()
    library.close_keep_alive()


@pytest.fixture
def auth(files: FileLibrary) -> Auth:
    service = Auth(files.library)
    service.initialize(PASSCODE)
    return service


@pytest.fixture
def without_maintenance(monkeypatch):
    """Keep background indexing from changing state during read-only endpoint assertions."""
    monkeypatch.setattr(Maintenance, "start", lambda self: None)


@pytest.fixture
def after_maintenance(monkeypatch):
    """wait(condition): block until a background maintenance pass leaves `condition()` true."""
    passes = threading.Condition()
    run_steps = Maintenance.run_steps

    def observed(self):
        try:
            return run_steps(self)
        finally:
            with passes:
                passes.notify_all()

    monkeypatch.setattr(Maintenance, "run_steps", observed)

    def wait(condition, timeout: float = 10) -> None:
        with passes:
            assert passes.wait_for(condition, timeout), "background maintenance did not finish the work"

    return wait


@pytest.fixture
def client(files: FileLibrary, auth: Auth):
    settings = HTTPSettings(public_origin=ORIGIN)
    save_http_settings(files.library, settings)
    with TestClient(create_app(files, auth=auth), base_url=ORIGIN) as connection:
        yield connection


@pytest.fixture
def temporary_cleanup(monkeypatch):
    handles, directories = [], []
    spool, temporary = SpooledTemporaryFile, api_scans.tempfile.mkdtemp

    def record_spool(*args, **kwargs):
        result = spool(*args, **kwargs)
        handles.append(result)
        return result

    def record_directory(*args, **kwargs):
        result = temporary(*args, **kwargs)
        if str(kwargs.get("prefix", "")).startswith("mangacouch-upload-"):
            directories.append(Path(result))
        return result

    monkeypatch.setattr(forms, "SpooledTemporaryFile", record_spool)
    monkeypatch.setattr(api_scans.tempfile, "mkdtemp", record_directory)
    yield handles
    assert all(handle.closed for handle in handles)
    assert all(not path.exists() for path in directories)


@pytest.fixture
def api(files, auth, queue):
    _, key = auth.create_key("management", "release checks")
    client = TestClient(
        create_app(files, HTTPSettings(public_origin=ORIGIN), auth=auth, downloads=queue[0]),
        base_url=ORIGIN,
        headers={"Authorization": f"Bearer {key}"},
    )
    yield client
    client.close()


@pytest.fixture
def sync(files) -> SidecarSync:
    return SidecarSync(files)


@pytest.fixture
def library(tmp_path: Path) -> Iterator[Library]:
    library = Library.initialize(tmp_path / "library.sqlite")
    library.keep_alive()
    yield library
    library.close_keep_alive()


@pytest.fixture
def queue(files, tmp_path):
    remote = SourceFixture(make_zip(tmp_path / "remote.zip").read_bytes())
    client = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote))
    queue = Downloads(files, http=client)
    yield queue, remote
    queue.close()


@pytest.fixture
def remote(tmp_path):
    return SourceFixture(make_zip(tmp_path / "source.zip").read_bytes())


@pytest.fixture
def sources(remote):
    http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote))
    yield Sources(http)
    http.close()
