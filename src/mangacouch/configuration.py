from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .database import Library
from .errors import InvalidInput
from .limits import Limits
from .models import Model

MAX_CONFIG_BYTES = 64 * 1024


class HTTPSettings(Model):
    public_origin: Annotated[str, Field(max_length=2048)] = "http://127.0.0.1:8780"
    allowed_hosts: Annotated[list[str], Field(max_length=128)] = Field(default_factory=list)
    max_upload_bytes: Annotated[int, Field(gt=0, le=2**53 - 1)] = 2 * 1024**3
    thumbnail_cache_bytes: Annotated[int, Field(ge=64 * 1024, le=2**53 - 1)] = 20 * 1024**3
    """Disk budget of the cover and page thumbnail cache; shrinking it below the file rebuilds it empty."""
    trusted_proxies: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)
    """Reverse proxy addresses or networks whose X-Forwarded-For names the client (sign-in limits)."""

    @model_validator(mode="after")
    def check_origin(self) -> Self:
        for proxy in self.trusted_proxies:
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError:
                raise ValueError("Trusted proxies must be IP addresses or networks") from None
        for host in self.allowed_hosts:
            if host in {"*", "0.0.0.0"}:
                continue
            candidate = host.removeprefix("*.")
            parsed_host = urlsplit("//" + candidate)
            if (
                not candidate
                or host != host.lower()
                or len(host) > 253
                or not re.fullmatch(r"[a-z0-9.\[\]:-]+", candidate)
                or not parsed_host.hostname
                or parsed_host.username is not None
                or parsed_host.path
                or parsed_host.query
                or parsed_host.fragment
            ):
                raise ValueError("Allowed hosts must be hostnames, IPs, *.domain, 0.0.0.0 or *")
            try:
                _ = parsed_host.port  # raises ValueError for a non-numeric or out-of-range port
            except ValueError:
                raise ValueError("Allowed host port must be a number between 0 and 65535") from None
        parsed = urlsplit(self.public_origin)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.netloc.isascii()
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.netloc != parsed.netloc.lower()
            or parsed.port in {0, 80 if parsed.scheme == "http" else 443}
            or parsed.netloc.endswith(":")
            or self.public_origin != f"{parsed.scheme}://{parsed.netloc}"
            or any(character.isspace() or ord(character) < 32 for character in self.public_origin)
            or any(character in parsed.netloc for character in '\\%?#<>"`{}|^')
        ):
            raise ValueError("Public origin must be an exact http(s) origin without a path")
        return self

    def accepts_host(self, authority: str) -> bool:
        if authority.lower() == urlsplit(self.public_origin).netloc:
            return True
        if not self.allowed_hosts:
            return False
        try:
            parsed = urlsplit("//" + authority.lower())
            host, port = parsed.hostname, parsed.port
            if (
                not host
                or parsed.username is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
                or not re.fullmatch(r"[a-z0-9.\[\]:-]+", authority.lower())
            ):
                return False
        except ValueError:
            return False
        for pattern in self.allowed_hosts:
            if pattern == "*":
                return True
            if pattern == "0.0.0.0":
                try:
                    ipaddress.ip_address(host)
                    return True
                except ValueError:
                    continue
            target = urlsplit("//" + pattern.removeprefix("*."))
            if target.port is not None and target.port != (
                port or (443 if self.public_origin.startswith("https:") else 80)
            ):
                continue
            if host == target.hostname and not pattern.startswith("*."):
                return True
            if pattern.startswith("*.") and host.endswith("." + str(target.hostname)):
                return True
        return False

    @property
    def cookie_name(self) -> str:
        public = urlsplit(self.public_origin)
        return self.cookie_for(public.netloc, public.scheme)

    def request_scheme(self, authority: str, scheme: str, origin: str | None = None) -> str:
        """The scheme the browser uses for this host.

        The server speaks plain HTTP, so behind a TLS proxy `scheme` is always http. The public
        origin's host uses its configured scheme. Another allowed host may be reached directly (a
        LAN address over http) or through a TLS proxy, so a request whose `Origin` is that same
        host names its scheme; without one, the connection's scheme is assumed."""
        public = urlsplit(self.public_origin)
        if authority.lower() == public.netloc.lower():
            return public.scheme
        for candidate in ("https", "http"):
            if origin == f"{candidate}://{authority}":
                return candidate
        return scheme

    def cookie_for(self, authority: str, scheme: str, origin: str | None = None) -> str:
        scheme = self.request_scheme(authority, scheme, origin)
        port = urlsplit(f"{scheme}://{authority}").port or (443 if scheme == "https" else 80)
        prefix = "__Host-mangacouch" if scheme == "https" else "mangacouch_session"
        return f"{prefix}_{port}"

    def session_cookie(
        self, authority: str, scheme: str, origin: str | None, cookies: Mapping[str, str]
    ) -> str:
        """The session cookie to read. On another allowed host a request without `Origin` does not
        tell which scheme the browser used, so the cookie of either scheme is accepted (a browser
        sends a __Host- cookie only over https), preferring the Secure cookie over a leftover HTTP one."""
        name = self.cookie_for(authority, scheme, origin)
        if origin is not None or authority.lower() == urlsplit(self.public_origin).netloc.lower():
            return name
        for candidate in ("https", "http"):
            other = self.cookie_for(authority, candidate, f"{candidate}://{authority}")
            if other in cookies:
                return other
        return name


class RuntimeProfile(Model):
    """runtime.json beside the database: where the books are and the archive limits."""

    schema_name: Literal["mangacouch.runtime/2"] = Field(alias="schema")
    library_root_hint: Annotated[str, Field(max_length=4096)]
    limits: Limits

    @model_validator(mode="before")
    @classmethod
    def upgrade_limits(cls, value):
        if isinstance(value, dict) and value.get("schema") == "mangacouch.runtime/1":
            defaults = {**vars(Limits()), "max_pixels": 100_000_000}
            value = {
                **value,
                "schema": "mangacouch.runtime/2",
                "limits": {
                    key: limit for key, limit in value.get("limits", {}).items() if limit != defaults.get(key)
                },
            }
        return value


def load_profile(database: Path) -> RuntimeProfile | None:
    path = database.absolute().parent / "runtime.json"
    if path.is_symlink():
        raise InvalidInput("Runtime configuration must be an ordinary file")
    if not path.exists():
        return None
    with path.open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise InvalidInput("Runtime configuration exceeds the supported size")
    profile = RuntimeProfile.model_validate_json(data)
    if not Path(profile.library_root_hint).is_absolute():
        raise InvalidInput("Runtime library root must be an absolute path")
    return profile


def effective_upload_limit(settings: HTTPSettings, limits: Limits) -> int:
    """Largest accepted upload: the HTTP limit, capped by the archive limit when one is set."""
    if limits.max_archive_bytes is None:
        return settings.max_upload_bytes
    return min(settings.max_upload_bytes, limits.max_archive_bytes)


def _decode(row) -> HTTPSettings:
    """Stored settings exactly as saved; defaults fill only what was never chosen."""
    if not row:
        return HTTPSettings()
    return HTTPSettings.model_validate(json.loads(row[0]))


def save_http_settings(library: Library, settings: HTTPSettings) -> None:
    """Record the settings `serve` runs with, for `health` and snapshots; `serve` never reads them back."""
    with library.transaction() as db:
        db.execute(
            "INSERT INTO http_config(singleton,settings_json) VALUES(1,?) "
            "ON CONFLICT(singleton) DO UPDATE SET settings_json=excluded.settings_json",
            (settings.model_dump_json(),),
        )


def load_http_settings(library: Library) -> HTTPSettings:
    with library.connection() as db:
        return _decode(db.execute("SELECT settings_json FROM http_config").fetchone())
