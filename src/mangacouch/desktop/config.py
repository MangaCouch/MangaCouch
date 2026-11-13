"""Desktop launcher settings: where the library lives and how the tray starts `serve`.

The settings are per user and kept outside the library, in the platform's application data folder.
They are the source of truth for the server options the tray passes on every start.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import sys
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, ValidationError, model_validator

from ..configuration import HTTPSettings
from ..files import atomic_write
from ..models import Model

GIB = 1024**3
DATABASE_NAME = "library.sqlite"
SAVED, CANCELLED = 0, 3


def app_directory() -> Path:
    """Per-user folder for the launcher settings, its lock and the server log."""
    override = os.environ.get("MANGACOUCH_DESKTOP_HOME")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "MangaCouch"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "MangaCouch"
    config = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config) if os.path.isabs(config) else Path.home() / ".config"
    return base / "mangacouch"


def default_library_directory() -> Path:
    return Path.home() / "MangaCouch"


class DesktopConfig(Model):
    state_dir: Annotated[str, Field(max_length=4096)] = ""
    """Folder holding library.sqlite and runtime.json; empty until the first setup."""
    port: Annotated[int, Field(ge=1024, le=65535)] = 8780
    lan: bool = False
    """Listen on every interface so other devices on the network can connect."""
    public_origin: Annotated[str, Field(max_length=2048)] = ""
    """Exact URL clients use (for example behind an HTTPS proxy); empty derives it from the port."""
    extra_hosts: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    trusted_proxies: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    cache_gib: Annotated[int, Field(ge=1, le=1024 * 1024)] = 20
    upload_gib: Annotated[int, Field(ge=1, le=1024 * 1024)] = 2
    launch_at_login: bool = False
    open_browser: bool = True
    """Open the web app once the server is ready after the tray starts."""

    @model_validator(mode="after")
    def check_server_options(self) -> Self:
        # Reject what `serve` would refuse, so a bad value fails in the settings window, not at start.
        self.http_settings(lan_address=None)
        return self

    @property
    def database(self) -> Path | None:
        return Path(self.state_dir) / DATABASE_NAME if self.state_dir else None

    @property
    def host(self) -> str:
        return "0.0.0.0" if self.lan else "127.0.0.1"

    @property
    def local_url(self) -> str:
        """What this computer opens: the configured origin, else loopback (always an allowed host)."""
        return self.public_origin or f"http://127.0.0.1:{self.port}"

    def origin(self, lan_address: str | None) -> str:
        if self.public_origin:
            return self.public_origin
        if self.lan and lan_address:
            return f"http://{lan_address}:{self.port}"
        return f"http://localhost:{self.port}"

    def allowed_hosts(self) -> list[str]:
        hosts = ["localhost", "127.0.0.1"]
        if self.lan:
            # "0.0.0.0" admits any IP address, so the server stays reachable when DHCP changes it.
            hosts.append("0.0.0.0")
            name = local_hostname()
            if name:
                hosts.append(name)
        return list(dict.fromkeys([*hosts, *self.extra_hosts]))

    def http_settings(self, lan_address: str | None) -> HTTPSettings:
        return HTTPSettings(
            public_origin=self.origin(lan_address),
            allowed_hosts=self.allowed_hosts(),
            trusted_proxies=self.trusted_proxies,
            max_upload_bytes=self.upload_gib * GIB,
            thumbnail_cache_bytes=self.cache_gib * GIB,
        )

    def serve_arguments(self, lan_address: str | None) -> list[str]:
        """CLI arguments (after the executable) that start this configuration's server."""
        if self.database is None:
            raise ValueError("The library folder is not set")
        settings = self.http_settings(lan_address)
        arguments = [
            "--db",
            str(self.database),
            "serve",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--origin",
            settings.public_origin,
            "--upload-bytes",
            str(settings.max_upload_bytes),
            "--cache-bytes",
            str(settings.thumbnail_cache_bytes),
            "--stop-on-stdin-eof",
        ]
        for host in settings.allowed_hosts:
            arguments += ["--allowed-host", host]
        for proxy in settings.trusted_proxies:
            arguments += ["--trusted-proxy", proxy]
        return arguments


def local_hostname() -> str | None:
    """The mDNS name (`name.local`) other devices may use, when the host name is a valid label."""
    name = socket.gethostname().split(".")[0].lower()
    candidate = f"{name}.local"
    try:
        HTTPSettings(allowed_hosts=[candidate])
    except ValidationError:
        return None
    return candidate if name else None


def lan_address() -> str | None:
    """The address of the interface that routes to the network; no packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.connect(("192.0.2.1", 9))  # TEST-NET-1: never routed, only selects an interface
            address = probe.getsockname()[0]
        except OSError:
            return None
    return None if address.startswith(("127.", "0.")) else address


def config_path() -> Path:
    return app_directory() / "desktop.json"


def read_config() -> tuple[DesktopConfig, str | None]:
    """The saved settings, or the defaults plus the reason the file could not be used. A file that
    does not parse is moved to `desktop.json.invalid`, so saving the defaults does not destroy it."""
    path = config_path()
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return DesktopConfig(), None
    except OSError as error:
        return DesktopConfig(), str(error)
    try:
        return DesktopConfig.model_validate_json(data), None
    except ValueError as error:
        with contextlib.suppress(OSError):
            os.replace(path, path.with_name(path.name + ".invalid"))
        return DesktopConfig(), str(error).splitlines()[0]


def load_config() -> DesktopConfig:
    """The saved settings; missing or unreadable settings start from the defaults."""
    return read_config()[0]


def save_config(config: DesktopConfig) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(config.model_dump(), ensure_ascii=False, indent=2).encode("utf-8"))


def find_state_directory(folder: Path) -> Path:
    """Accept either the state folder itself or its parent holding a `state/` library."""
    if not (folder / DATABASE_NAME).is_file() and (folder / "state" / DATABASE_NAME).is_file():
        return folder / "state"
    return folder


def library_root(state_dir: Path) -> Path | None:
    """The books folder of an existing library, from its runtime.json."""
    from ..lifecycle import load_profile

    try:
        profile = load_profile(state_dir / DATABASE_NAME)
    except (OSError, ValueError):
        return None
    return Path(profile.library_root_hint) if profile else None
