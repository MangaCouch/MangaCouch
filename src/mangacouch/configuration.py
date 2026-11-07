from __future__ import annotations

import ipaddress
import re
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from .archives import Limits
from .database import Library
from .models import Model


class HTTPSettings(Model):
    public_origin: Annotated[str, Field(max_length=2048)] = "http://127.0.0.1:8780"
    allowed_hosts: Annotated[list[str], Field(max_length=128)] = Field(default_factory=list)
    max_upload_bytes: Annotated[int, Field(gt=0, le=2**53 - 1)] = 512 * 1024**2

    @model_validator(mode="after")
    def check_origin(self) -> Self:
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
        return "__Host-mangacouch" if self.public_origin.startswith("https://") else "mangacouch_session"


def effective_upload_limit(settings: HTTPSettings, limits: Limits) -> int:
    """Largest accepted upload: the HTTP limit, capped by the archive limit when one is set."""
    if limits.max_archive_bytes is None:
        return settings.max_upload_bytes
    return min(settings.max_upload_bytes, limits.max_archive_bytes)


def _decode(row) -> HTTPSettings:
    return HTTPSettings.model_validate_json(row[0]) if row else HTTPSettings()


def save_http_settings(library: Library, settings: HTTPSettings) -> None:
    with library.transaction() as db:
        db.execute(
            "INSERT INTO http_config(singleton,settings_json) VALUES(1,?) "
            "ON CONFLICT(singleton) DO UPDATE SET settings_json=excluded.settings_json",
            (settings.model_dump_json(),),
        )


def load_http_settings(library: Library) -> HTTPSettings:
    with library.connection() as db:
        return _decode(db.execute("SELECT settings_json FROM http_config").fetchone())
