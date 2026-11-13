from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import free_port

from mangacouch.archives import Limits
from mangacouch.auth import Auth
from mangacouch.lifecycle import initialize_library

REPOSITORY = Path(__file__).parents[1]


def environment(**overrides):
    return {**{k: v for k, v in os.environ.items() if not k.startswith("MANGACOUCH_")}, **overrides}


def cli(*args, env=None):
    return subprocess.run(
        [sys.executable, "-m", "mangacouch", *map(str, args)],
        env=env or environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    library = initialize_library(tmp_path / "library.sqlite", root, Limits())
    Auth(library).initialize("release-fixture-passcode")
    return library


def stop(process):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=10)
    else:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def test_deployment_environment_and_explicit_cli_precedence(library):
    configured = environment(
        MANGACOUCH_DB=str(library.path),
        MANGACOUCH_HOST="0.0.0.0",
        MANGACOUCH_PORT="8799",
        MANGACOUCH_ORIGIN="https://library.example",
        MANGACOUCH_ALLOWED_HOSTS="library.example, localhost",
        MANGACOUCH_UPLOAD_BYTES="12345",
        MANGACOUCH_MAX_BYTES="10000",
    )
    result = cli("serve", "--check", env=configured)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["listen"] == {"host": "0.0.0.0", "port": 8799}
    assert report["http_settings"]["allowed_hosts"] == ["library.example", "localhost"]
    assert report["http_settings"]["public_origin"] == "https://library.example"
    assert report["effective_max_upload_bytes"] == 10000
    result = cli(
        "--max-bytes",
        "50000",
        "serve",
        "--check",
        "--host",
        "127.0.0.1",
        "--port",
        "8798",
        "--origin",
        "http://localhost:8798",
        "--allowed-host",
        "localhost",
        "--upload-bytes",
        "20000",
        env=configured,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["listen"] == {"host": "127.0.0.1", "port": 8798}
    assert report["http_settings"]["allowed_hosts"] == ["localhost"]
    assert report["http_settings"]["public_origin"] == "http://localhost:8798"
    assert report["effective_max_upload_bytes"] == 20000


def test_invalid_deployment_number_rejected_before_starting(library):
    result = cli("serve", "--check", env=environment(MANGACOUCH_DB=str(library.path), MANGACOUCH_PORT="oops"))
    assert result.returncode == 2 and "invalid int value" in result.stderr


def test_health_uses_configured_host_and_checks_root(library):
    from helpers import start_test_server

    process, port, response = start_test_server(
        lambda port: [
            sys.executable,
            "-m",
            "mangacouch",
            "--db",
            str(library.path),
            "serve",
            "--port",
            str(port),
            "--origin",
            "https://library.example",
            "--allowed-host",
            "LOCALHOST",
        ],
        env=environment(),
        stdout=subprocess.PIPE,
        headers={"Host": "localhost"},
    )
    try:
        assert response.status_code == 401
        healthy = cli("--db", library.path, "health", "--port", port)
        assert healthy.returncode == 0 and json.loads(healthy.stdout) == {"ready": True}
        root = library.path.parent / "books"
        root.rename(root.with_name("books-offline"))
        failed = cli("--db", library.path, "health", "--port", port)
        assert failed.returncode == 1 and json.loads(failed.stdout)["error"] == "RootUnavailable"
    finally:
        stop(process)
    failed = cli(
        "--db", library.path, "--root", library.path.parent / "books-offline", "health", "--port", port
    )
    assert failed.returncode == 1 and "not reachable" in json.loads(failed.stdout)["detail"]


HEALTH_IMPORTS = """
import json, sys
from mangacouch.__main__ import main
sys.argv = ["mangacouch", *sys.argv[1:]]
code = main()
heavy = ["pyvips", "pypdfium2", "mangacouch.archives", "mangacouch.ingest"]
print(json.dumps({"code": code, "loaded": [name for name in heavy if name in sys.modules]}))
"""


def test_health_does_not_load_the_image_libraries(library):
    """The container runs `health` every 30 s; it reads only the database, runtime.json and the
    books folder marker, so it leaves libvips and PDFium unloaded."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            HEALTH_IMPORTS,
            "--db",
            str(library.path),
            "health",
            "--port",
            str(free_port()),
        ],
        env=environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )
    *reported, last = result.stdout.strip().splitlines()
    assert "not reachable" in "".join(reported), result.stdout + result.stderr
    assert json.loads(last) == {"code": 1, "loaded": []}


def test_serve_builds_http_settings_from_defaults_on_every_start(library, monkeypatch, capsys):
    import uvicorn

    from mangacouch import __main__ as main_module
    from mangacouch.configuration import HTTPSettings, load_http_settings

    for name in [name for name in os.environ if name.startswith("MANGACOUCH_")]:
        monkeypatch.delenv(name)
    import asyncio

    async def bound(server, sockets=None):
        assert server.config.limit_concurrency == 128
        server.started = True

    monkeypatch.setattr(uvicorn.Server, "startup", bound)
    monkeypatch.setattr(uvicorn.Server, "run", lambda server: asyncio.run(server.startup()))
    port = str(free_port())

    def serve(*arguments, **variables):
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr(
            sys, "argv", ["mangacouch", "--db", str(library.path), "serve", "--port", port, *arguments]
        )
        assert main_module.main() == 0
        capsys.readouterr()
        return load_http_settings(library)

    first = serve(
        "--allowed-host",
        "*",
        "--allowed-host",
        "NAS.Local",
        "--upload-bytes",
        "12345",
        MANGACOUCH_ORIGIN="https://library.example",
        MANGACOUCH_CACHE_BYTES="1048576",
        MANGACOUCH_TRUSTED_PROXIES="172.17.0.0/16",
    )
    assert first.allowed_hosts == ["*", "nas.local"]
    assert (first.max_upload_bytes, first.thumbnail_cache_bytes) == (12345, 1048576)
    assert first.trusted_proxies == ["172.17.0.0/16"]
    # Compose passes unset variables as empty strings: they mean the defaults, not the last values,
    # so `*` is revoked by leaving it out.
    second = serve(
        MANGACOUCH_ALLOWED_HOSTS="",
        MANGACOUCH_UPLOAD_BYTES="",
        MANGACOUCH_CACHE_BYTES="",
        MANGACOUCH_TRUSTED_PROXIES="",
    )
    assert second == HTTPSettings(public_origin="https://library.example")
    assert serve(MANGACOUCH_ALLOWED_HOSTS="Books.Example, localhost").allowed_hosts == [
        "books.example",
        "localhost",
    ]
    monkeypatch.delenv("MANGACOUCH_ORIGIN")
    monkeypatch.delenv("MANGACOUCH_ALLOWED_HOSTS")
    assert serve() == HTTPSettings()
