"""Smoke-check a standalone MangaCouch executable as a black box, using only the standard library.

Usage: python scripts/check_standalone.py TARGET

TARGET is the executable itself or a release archive (mangacouch-<version>-<platform>.tar.gz or .zip).

It runs the CLI against a temporary library: init, auth-init, a CBZ and a PDF import, a page from each
(PDF pages exercise PDFium and the libvips WebP encoder bundled in the executable), then serves over
loopback and checks the web shell, the authentication boundary, diagnostics and `health`.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from pathlib import Path

PASSCODE = "standalone-fixture-only"


def png(width: int = 12, height: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes((120, 60, 20)) * width for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def pdf() -> bytes:
    content = b"0.2 0.5 0.8 rg 10 20 40 65 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 150] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    output = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    output += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return bytes(output)


class Checker:
    def __init__(self, executable: Path, directory: Path):
        self.executable = executable
        self.database = directory / "state" / "library.sqlite"
        self.root = directory / "books"
        self.environment = {k: v for k, v in os.environ.items() if not k.startswith("MANGACOUCH_")}

    def run(self, *args: str | Path, stdin: str | None = None) -> dict:
        command = [str(self.executable), "--db", str(self.database), *map(str, args)]
        result = subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.environment,
            timeout=120,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{' '.join(command[1:])} failed: {result.stdout}{result.stderr}")
        return json.loads(result.stdout)

    def serve(self, port: int, key: str) -> None:
        origin = f"http://127.0.0.1:{port}"
        process = subprocess.Popen(
            [
                str(self.executable),
                "--db",
                str(self.database),
                "serve",
                "--port",
                str(port),
                "--origin",
                origin,
            ],
            env=self.environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            for _ in range(300):
                if process.poll() is not None:
                    raise RuntimeError(f"serve exited: {process.communicate()[1].decode(errors='replace')}")
                try:
                    status, _ = fetch(f"{origin}/api/auth/me")
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("serve did not start listening")
            if status != 401:
                raise RuntimeError("authentication boundary is not enforced")
            status, body = fetch(f"{origin}/")
            if status != 200 or b"<html" not in body.lower():
                raise RuntimeError("bundled web shell is not served")
            status, body = fetch(f"{origin}/api/diagnostics", {"Authorization": f"Bearer {key}"})
            if status != 200 or json.loads(body).get("ready") is not True:
                raise RuntimeError(f"diagnostics endpoint is not ready: {body[:500]!r}")
            if self.run("health", "--port", str(port)) != {"ready": True}:
                raise RuntimeError("health command did not report ready")
        finally:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=30
                )
            else:
                process.terminate()
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=30)
            if process.stderr is not None:
                process.stderr.close()


def fetch(url: str, headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=20) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def check(executable: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="mangacouch-standalone-") as temporary:
        directory = Path(temporary)
        checker = Checker(executable, directory)
        checker.root.mkdir()
        checker.database.parent.mkdir()
        checker.run("--root", checker.root, "init")
        checker.run("auth-init", stdin=f"{PASSCODE}\n{PASSCODE}\n")
        sources = directory / "sources"
        sources.mkdir()
        with zipfile.ZipFile(sources / "comic.cbz", "w") as archive:
            archive.writestr("1.png", png())
            archive.writestr("2.png", png(16, 10))
        (sources / "document.pdf").write_bytes(pdf())
        formats = {}
        for name, expected in (("comic.cbz", "png"), ("document.pdf", "webp")):
            book = checker.run("import", sources / name)["book"]
            output = directory / f"{name}.page"
            page = checker.run("page", book["id"], "0", output)
            if page["format"] != expected or page["bytes"] <= 0:
                raise RuntimeError(f"{name} page 0 rendered as {page}")
            formats[name] = page["format"]
        key = checker.run("key-create", "--kind", "management", "--label", "smoke")["key"]
        checker.serve(free_port(), key)
        return formats


def check_target(target: Path) -> dict:
    if not (target.suffix == ".zip" or target.name.endswith(".tar.gz")):
        return check(target)
    # A release archive: unpack it the way a user would and run the executable inside.
    with tempfile.TemporaryDirectory(prefix="mangacouch-archive-") as temporary:
        if target.suffix == ".zip":
            with zipfile.ZipFile(target) as archive:
                archive.extractall(temporary)
        else:
            with tarfile.open(target) as archive:
                archive.extractall(temporary, filter="tar")
        name = "mangacouch.exe" if os.name == "nt" else "mangacouch"
        return check(Path(temporary) / "mangacouch" / name)


def main() -> None:
    target = Path(sys.argv[1]).resolve()
    try:
        pages = check_target(target)
    except Exception as error:
        print(json.dumps({"passed": False, "error": f"{type(error).__name__}: {error}"}))
        raise SystemExit(1) from None
    print(json.dumps({"passed": True, "target": target.name, "pages": pages}))


if __name__ == "__main__":
    main()
