"""Run `mangacouch serve` as a child process and track whether it answers."""

from __future__ import annotations

import contextlib
import enum
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from urllib.parse import urlsplit

import httpx

from .config import DesktopConfig, app_directory, lan_address

# serve gives open requests 15 s, then up to 60 s to persist a paid archive link (GRACEFUL_SHUTDOWN_SECONDS).
STOP_TIMEOUT = 90
START_RETRY_SECONDS = 1
START_RETRY_TIMEOUT = 90
LOG_TAIL_BYTES = 16 * 1024
# server.log is rotated at each start (its automatic retries append) and whenever it would pass
# LOG_MAX_BYTES, keeping LOG_BACKUPS older files (server.log.1 is the newest).
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
LOG_LINE_BYTES = 64 * 1024
"""Longest piece of output copied at once; a longer line is split, a shorter one never is."""
LOG_DRAIN_TIMEOUT = 10
RETRY_MARKER = "--- MangaCouch launcher: retrying the start ---"
"""Written to the log before each retried start; `failure_detail` reads only what follows it."""


class State(enum.StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"
    LIBRARY_MISSING = "library_missing"


@dataclass(frozen=True)
class Status:
    state: State
    detail: str = ""
    lan_url: str | None = None


def cli_command() -> list[str]:
    """How to run the MangaCouch CLI: the frozen executable itself, or this interpreter."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    executable = Path(sys.executable)
    if sys.platform == "win32" and executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            executable = console
    return [str(executable), "-m", "mangacouch"]


def log_path() -> Path:
    return app_directory() / "logs" / "server.log"


class RotatingLog:
    """The server log, written only by the launcher, which copies the server's output into it.

    Being the only writer, the launcher can close the file before renaming it, which Windows
    requires. If another program holds a file open and the rename fails anyway, writing continues
    in the current file and rotation is retried after another quarter of `max_bytes`. A failed
    write (a full disk) drops that output rather than stopping the copy, which would stall the server.
    """

    def __init__(
        self, path: Path, max_bytes: int = LOG_MAX_BYTES, backups: int = LOG_BACKUPS, *, fresh: bool = True
    ):
        """Start a new file (`fresh`), or append to the current one when retrying a start, so a
        burst of retries cannot push the previous run's log out of the kept backups."""
        self.path = path
        self.max_bytes = max_bytes
        self.backups = max(1, backups)
        self._lock = threading.Lock()
        self._stream: IO[bytes] | None = None
        self._size = 0
        self._closed = False
        self._limit = max_bytes
        path.parent.mkdir(parents=True, exist_ok=True)
        if fresh:
            with self._lock:
                self._rotate()
        else:
            self.write(f"{RETRY_MARKER}\n".encode())

    def _backup(self, index: int) -> Path:
        return self.path.with_name(f"{self.path.name}.{index}")

    def _rotate(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError):
                self._stream.close()
            self._stream = None
        rotated = True
        try:
            if self.path.exists():
                for index in range(self.backups - 1, 0, -1):
                    if self._backup(index).exists():
                        os.replace(self._backup(index), self._backup(index + 1))
                os.replace(self.path, self._backup(1))
        except OSError:
            rotated = False
        self._stream = self.path.open("wb" if rotated else "ab")
        self._size = self._stream.tell()
        self._limit = self.max_bytes if rotated else self._size + max(1, self.max_bytes // 4)

    def write(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                if self._stream is None:
                    self._stream = self.path.open("ab")
                    self._size = self._stream.tell()
                    self._limit = max(self.max_bytes, self._size + self.max_bytes // 4)
                if self._size and self._size + len(data) > self._limit:
                    self._rotate()
                self._stream.write(data)
                self._stream.flush()
                self._size += len(data)
            except OSError:
                pass

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._stream is not None:
                with contextlib.suppress(OSError):
                    self._stream.close()
                self._stream = None


def copy_output(stream: IO[bytes], log: RotatingLog) -> None:
    """Copy the server's output into the log line by line until the server closes it."""
    with contextlib.suppress(OSError, ValueError), stream:
        for line in iter(lambda: stream.readline(LOG_LINE_BYTES), b""):
            log.write(line)


def port_in_use(host: str, port: int) -> bool:
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.2)
                if probe.connect_ex((address, port)) == 0:
                    return True
        except OSError:
            continue
    return False


# The last line of a Python traceback, "module.ErrorName: message".
EXCEPTION_LINE = re.compile(
    r"(?:[A-Za-z_]\w*\.)*[A-Za-z_]\w*(?:Error|Exception|Unavailable|Input|Exit)\b(?:: (.*))?"
)


def failure_detail(log: Path) -> str:
    """The reason a server exited: its JSON error line, else the exception of the last traceback
    (uvicorn's closing "Application startup failed" line says nothing), else the last line."""
    try:
        with log.open("rb") as stream:
            stream.seek(max(0, log.stat().st_size - LOG_TAIL_BYTES))
            lines = stream.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    lines = [line.strip() for line in lines if line.strip()]
    if RETRY_MARKER in lines:
        # An earlier attempt's error must not be reported as this attempt's.
        lines = lines[len(lines) - lines[::-1].index(RETRY_MARKER) :]
    for line in reversed(lines):
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "error" in value:
                return str(value.get("detail") or value["error"])
    for line in reversed(lines):
        match = EXCEPTION_LINE.fullmatch(line)
        if match:
            return match.group(1) or line
    return lines[-1] if lines else ""


class Service:
    """Owns one server process at a time. Methods may be called from any thread."""

    def __init__(self, on_change: Callable[[Status], None] = lambda status: None):
        self._on_change = on_change
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log: RotatingLog | None = None
        self._copier: threading.Thread | None = None
        self._status = Status(State.STOPPED)
        self._generation = 0
        self._retry_timer: threading.Timer | None = None

    @property
    def status(self) -> Status:
        return self._status

    def _set(self, status: Status) -> None:
        if status != self._status:
            self._status = status
            self._on_change(status)

    def _cancel_retry(self) -> None:
        if self._retry_timer is not None:
            self._retry_timer.cancel()
            self._retry_timer = None

    def _retry(self, config: DesktopConfig, deadline: float, detail: str) -> None:
        if time.monotonic() >= deadline:
            self._set(Status(State.FAILED, detail))
            return
        self._set(Status(State.STARTING, detail))
        generation = self._generation

        def retry() -> None:
            with self._lock:
                if generation == self._generation:
                    self._retry_timer = None
                    self.start(config, _deadline=deadline)

        self._retry_timer = threading.Timer(START_RETRY_SECONDS, retry)
        self._retry_timer.daemon = True
        self._retry_timer.start()

    def start(self, config: DesktopConfig, *, _deadline: float | None = None) -> None:
        with self._lock:
            if self._process is not None:
                return
            self._cancel_retry()
            # Only the first attempt of a start (not its retries) begins a new log file.
            fresh = _deadline is None
            if _deadline is None:
                self._generation += 1
                _deadline = time.monotonic() + START_RETRY_TIMEOUT
            if config.database is None or not config.database.is_file():
                self._set(Status(State.LIBRARY_MISSING, str(config.database or "")))
                return
            if port_in_use(config.host, config.port):
                self._retry(config, _deadline, f"port_in_use:{config.port}")
                return
            address = lan_address() if config.lan else None
            try:
                self._log = RotatingLog(log_path(), LOG_MAX_BYTES, LOG_BACKUPS, fresh=fresh)
            except OSError as error:
                self._set(Status(State.FAILED, str(error)))
                return
            flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            environment = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            # CLI options override inherited server settings. Keep the log level and trusted
            # proxies fallback (used only when the launcher has no explicit proxy list).
            for name in [name for name in environment if name.startswith("MANGACOUCH_")]:
                if name not in {"MANGACOUCH_LOG_LEVEL", "MANGACOUCH_TRUSTED_PROXIES"}:
                    del environment[name]
            try:
                self._process = subprocess.Popen(
                    [*cli_command(), *config.serve_arguments(address)],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    creationflags=flags,
                    env=environment,
                )
            except OSError as error:
                self._log.close()
                self._log = None
                self._set(Status(State.FAILED, str(error)))
                return
            if self._process.stdout is not None:
                self._copier = threading.Thread(
                    target=copy_output,
                    args=(self._process.stdout, self._log),
                    name="mangacouch-server-log",
                    daemon=True,
                )
                self._copier.start()
            self._generation += 1
            lan_url = f"http://{address}:{config.port}" if address and not config.public_origin else None
            self._set(Status(State.STARTING, lan_url=lan_url))
            threading.Thread(
                target=self._watch,
                args=(
                    self._process,
                    self._generation,
                    config,
                    urlsplit(config.origin(address)).netloc,
                    _deadline,
                ),
                name="mangacouch-service",
                daemon=True,
            ).start()

    def _watch(
        self,
        process: subprocess.Popen[bytes],
        generation: int,
        config: DesktopConfig,
        authority: str,
        deadline: float,
    ) -> None:
        """Poll the authentication boundary until the process exits or a newer start replaces it."""
        with httpx.Client(timeout=3, trust_env=False) as client:
            while process.poll() is None:
                healthy = False
                try:
                    response = client.get(
                        f"http://127.0.0.1:{config.port}/api/auth/me", headers={"Host": authority}
                    )
                    healthy = response.status_code == 401
                except httpx.HTTPError:
                    pass
                with self._lock:
                    if generation != self._generation or self._status.state == State.STOPPING:
                        break
                    if healthy:
                        self._set(Status(State.RUNNING, lan_url=self._status.lan_url))
                    elif self._status.state == State.RUNNING:
                        self._set(Status(State.STARTING, "not_responding", self._status.lan_url))
                time.sleep(5 if healthy else 0.5)
        code = process.wait()
        with self._lock:
            if generation != self._generation:
                return
            self._release()
            if self._status.state == State.STOPPING or code == 0:
                self._set(Status(State.STOPPED))
            else:
                detail = failure_detail(log_path()) or f"exit code {code}"
                if (
                    detail
                    in {
                        "Another download worker is using this library",
                        "Stop the other MangaCouch server before upgrading this library",
                    }
                    or "address already in use" in detail.lower()
                ):
                    self._retry(config, deadline, detail)
                else:
                    self._set(Status(State.FAILED, detail))

    def _release(self) -> None:
        if self._process is not None and self._process.stdin is not None:
            with contextlib.suppress(OSError):
                self._process.stdin.close()
        self._process = None
        # The server has exited: let the copy reach the end of its output before closing the log.
        if self._copier is not None:
            self._copier.join(LOG_DRAIN_TIMEOUT)
            self._copier = None
        if self._log is not None:
            self._log.close()
            self._log = None

    def stop(self) -> None:
        """Stop gracefully (close stdin), killing the server only if it does not exit in time."""
        with self._lock:
            self._cancel_retry()
            self._generation += 1
            process = self._process
            if process is None:
                self._set(Status(State.STOPPED))
                return
            self._set(Status(State.STOPPING))
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        try:
            process.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        with self._lock:
            if self._process is process:
                self._release()
            self._set(Status(State.STOPPED))

    def restart(self, config: DesktopConfig) -> None:
        self.stop()
        self.start(config)
