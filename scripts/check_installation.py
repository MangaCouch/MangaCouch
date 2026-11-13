"""Smoke-check an installed MangaCouch wheel: temporary library, loopback server, web shell, auth boundary."""

from __future__ import annotations

import json
import platform
import re
import socket
import tempfile
import threading
import time
from importlib.metadata import version
from pathlib import Path

import httpx
import uvicorn

from mangacouch.archives import Limits
from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.lifecycle import initialize_library


def check() -> None:
    with tempfile.TemporaryDirectory(prefix="mangacouch-smoke-") as temporary:
        path = Path(temporary)
        root = path / "books"
        root.mkdir()
        library = initialize_library(path / "library.sqlite", root, Limits())
        files = FileLibrary(library, root)
        auth = Auth(library)
        auth.initialize("installation-fixture-only")
        _, key = auth.create_key("management", "smoke")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
            app = create_app(files, HTTPSettings(public_origin=origin))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
            thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
            thread.start()
            try:
                for _ in range(200):
                    if server.started or not thread.is_alive():
                        break
                    time.sleep(0.05)
                if not server.started:
                    raise RuntimeError("HTTP server did not start")
                with httpx.Client(base_url=origin, timeout=20, trust_env=False) as client:
                    if client.get("/").status_code != 200:
                        raise RuntimeError("bundled web shell is not served")
                    response = client.get("/api/auth/me")
                    if response.status_code != 401 or response.json().get("error") != "invalid_credentials":
                        raise RuntimeError("authentication boundary is not enforced")
                    response = client.get("/api/diagnostics", headers={"Authorization": f"Bearer {key}"})
                    if response.status_code != 200 or response.json().get("ready") is not True:
                        raise RuntimeError("diagnostics endpoint is not ready")
                    response = client.get("/api/settings/summary", headers={"Authorization": f"Bearer {key}"})
                    if response.status_code != 200 or not re.fullmatch(
                        r"(?:main|v\d+\.\d+\.\d+)-[0-9a-f]{7}", response.json().get("version", "")
                    ):
                        raise RuntimeError("installed package has no valid build identity")
            finally:
                server.should_exit = True
                thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("HTTP server did not shut down")


def main() -> None:
    result = {"python": platform.python_version(), "platform": platform.platform()}
    try:
        result["package"] = version("mangacouch")
        check()
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}", **result}))
        raise SystemExit(1) from None
    print(json.dumps({"passed": True, **result}))


if __name__ == "__main__":
    main()
