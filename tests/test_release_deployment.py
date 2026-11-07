from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from mangacouch.archives import Limits
from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings, save_http_settings
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


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_http(process, port, host):
    with httpx.Client(trust_env=False, timeout=1) as client:
        for _ in range(150):
            if process.poll() is not None:
                raise AssertionError(process.communicate(timeout=10))
            try:
                response = client.get(f"http://127.0.0.1:{port}/api/auth/me", headers={"Host": host})
                assert response.status_code == 401
                return
            except httpx.TransportError:
                time.sleep(0.05)
    raise AssertionError("Temporary HTTP server did not start")


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
    port = free_port()
    save_http_settings(
        library, HTTPSettings(public_origin="https://library.example", allowed_hosts=["localhost"])
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "mangacouch", "--db", str(library.path), "serve", "--port", str(port)],
        env=environment(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_http(process, port, "localhost")
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
