from __future__ import annotations

import json
import threading
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, field_validator

from .database import Library, RevisionConflict
from .errors import InvalidInput
from .files import FileLock, atomic_write
from .models import Model, canonical_json
from .source_limits import EH_CAPACITY, EH_LEAK_PER_MIN, EH_MAX_CONCURRENCY

SECRET_NAMES = {"eh_cookie", "ex_cookie", "proxy"}
RATE_EVENTS_SHOWN = 5
"""Latest rate limits listed with the source settings."""
_KEY_LOCK = threading.Lock()


def _package_version() -> str:
    try:
        return version("mangacouch")
    except PackageNotFoundError:
        return "0"


DEFAULT_USER_AGENT = f"MangaCouch/{_package_version()}"


class SourceKeyLost(InvalidInput):
    """The stored source credentials cannot be read: their key file is missing, invalid or another
    library's. Online sources stay off until the credentials are reset."""

    code = "source_key_lost"


def try_lock(path: Path, message: str) -> FileLock:
    """Take an exclusive non-blocking lock on `path`; the caller closes it to release it."""
    try:
        return FileLock(path, blocking=False)
    except OSError:
        raise ValueError(message) from None


class SourceSettings(Model):
    online_paused: bool = False
    download_reserve_bytes: Annotated[int, Field(ge=0, le=1024**5)] = 10 * 1024**3
    download_limit_bytes: Annotated[int, Field(gt=0, le=1024**5)] = 32 * 1024**3
    comments: bool = True
    background_comments: bool = False
    """Background source checks fetch comments too (with `comments` on); otherwise they read only
    the gallery metadata, one gdata request per batch instead of an extra page per book."""
    eh_budget_capacity: Annotated[int, Field(ge=100, le=3000)] = EH_CAPACITY
    """Points the E(x)Hentai usage bucket holds (a gallery page is 1); see source_limits."""
    eh_budget_leak_per_min: Annotated[int, Field(ge=1, le=60)] = EH_LEAK_PER_MIN
    """Points the E(x)Hentai usage bucket drains per minute."""
    eh_max_concurrency: Annotated[int, Field(ge=1, le=8)] = EH_MAX_CONCURRENCY
    """Most E(x)Hentai requests in flight while the bucket is at most half full."""
    auto_free: bool = False
    default_quality: Literal["org", "res"] = "org"
    user_agent: Annotated[str, Field(min_length=1, max_length=512)] = DEFAULT_USER_AGENT
    allow_fake_ip: bool = False
    """Accept DNS answers in 198.18.0.0/15 (a TUN proxy's fake-IP mode) for source requests.

    Off by default: source hosts must resolve to public addresses. Turning it on lets connections
    reach that one benchmark range, which a fake-IP proxy intercepts; IP-literal hosts and every
    other private range stay rejected. An explicit proxy URL skips DNS checks altogether.
    """

    @field_validator("user_agent")
    @classmethod
    def valid_header(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) > 126 for character in value):
            raise ValueError("User agent must use printable ASCII")
        return value


def source_key_path(database: Path) -> Path:
    """The Fernet key file that belongs to a library database."""
    return database.with_name(database.name + ".sources.key")


class SourceVault:
    """Source credentials (cookies, proxy) encrypted with a Fernet key file.

    Threat model: the key lives next to the database as <library>.sources.key (mode 0600), so the
    encryption only protects copies of the database taken without that file, for example a
    database-only backup or a shared bug report. Anyone who can read the library directory can
    decrypt the credentials. Snapshots include the key so a restore keeps working; keep them private.
    """

    def __init__(self, library: Library):
        self.library = library
        self.path = source_key_path(library.path)

    def cipher(self, *, create: bool = False) -> Fernet:
        with _KEY_LOCK:
            if create and not self.path.exists():
                with suppress(FileExistsError):
                    atomic_write(self.path, Fernet.generate_key(), mode=0o600, replace=False)
            if self.path.is_symlink():
                raise SourceKeyLost("Source key must be a regular file")
            try:
                return Fernet(self.path.read_bytes())
            except (OSError, ValueError):
                raise SourceKeyLost("Source key is missing or invalid; restore the matching key") from None

    def encrypt(self, value: dict) -> str:
        return self.cipher(create=True).encrypt(canonical_json(value).encode()).decode()

    def decrypt(self, value: str | None) -> dict:
        if not value:
            return {}
        try:
            return json.loads(self.cipher().decrypt(value.encode()))
        except (InvalidToken, ValueError):
            raise SourceKeyLost("Source credentials cannot be decrypted with this key") from None

    def reset_credentials(self) -> None:
        """Discard inaccessible remote credentials, keeping non-secret settings (CLI, server stopped)."""
        lock_path = self.library.path.with_name(self.library.path.name + ".downloads.lock")
        with try_lock(lock_path, "Stop serve before resetting source credentials"):
            self.discard_credentials()

    def discard_credentials(self, working: tuple[str, ...] = ()) -> None:
        """Drop the stored credentials and saved remote download links, then start a new key.

        The caller holds the download worker lock: the running server resets from the web page.
        Unfinished downloads that held a remote link are paused; settings and books stay.
        """
        with _KEY_LOCK:
            with self.library.transaction() as db:
                db.execute(
                    "INSERT INTO source_config VALUES(1,json_object('online_paused',json('true')),'',1) "
                    "ON CONFLICT(singleton) DO UPDATE SET secrets='',revision=revision+1,"
                    "settings_json=json_set(settings_json,'$.online_paused',json('true'))"
                )
                db.execute(
                    "UPDATE download_job SET remote_secret=NULL,state=CASE WHEN id IN "
                    "(SELECT value FROM json_each(?)) THEN 'pausing' ELSE 'paused' END,revision=revision+1 "
                    "WHERE remote_secret IS NOT NULL AND state NOT IN ('done','cancelled','cancelling')",
                    (json.dumps(working),),
                )
                db.execute(
                    "UPDATE download_job SET state='pausing',revision=revision+1 WHERE id IN (SELECT "
                    "value FROM json_each(?)) AND state NOT IN ('done','cancelled','cancelling')",
                    (json.dumps(working),),
                )
                db.execute("UPDATE download_job SET remote_secret=NULL WHERE remote_secret IS NOT NULL")
            self.path.unlink(missing_ok=True)
            atomic_write(self.path, Fernet.generate_key(), mode=0o600, replace=False)

    def load(self) -> tuple[SourceSettings, dict[str, str], int]:
        """Settings, credentials and revision; SourceKeyLost when the credentials cannot be read."""
        settings, revision, secrets = self._read()
        with self.library.connection() as db:
            link = db.execute(
                "SELECT remote_secret FROM download_job WHERE remote_secret IS NOT NULL "
                "AND state NOT IN ('done','cancelled') LIMIT 1"
            ).fetchone()
        if link:
            self.decrypt(link[0])
        decrypted = self.decrypt(secrets)
        current = {key: value for key, value in decrypted.items() if key in SECRET_NAMES}
        if current != decrypted:
            with self.library.transaction() as db:
                changed = db.execute(
                    "UPDATE source_config SET secrets=?,revision=revision+1 WHERE secrets=?",
                    (self.encrypt(current), secrets),
                ).rowcount
            revision += bool(changed)
        return (
            settings,
            current,
            revision,
        )

    def settings(self) -> tuple[SourceSettings, int]:
        """The settings and revision alone, readable without the key."""
        settings, revision, _ = self._read()
        return settings, revision

    def key_lost(self) -> bool:
        try:
            self.load()
        except SourceKeyLost:
            return True
        return False

    def _read(self) -> tuple[SourceSettings, int, str]:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM source_config").fetchone()
        if row is None:
            return SourceSettings(), 0, ""
        settings = SourceSettings.model_validate(json.loads(row["settings_json"]))
        return settings, row["revision"], row["secrets"]

    def public(self) -> dict:
        """The settings page view; `key_lost` with nothing configured when the key is gone."""
        try:
            settings, secrets, revision = self.load()
            key_lost = False
        except SourceKeyLost:
            (settings, revision), secrets, key_lost = self.settings(), {}, True
        with self.library.connection() as db:
            events = db.execute(
                "SELECT at,site,kind,reason,until,level,remaining,penalty FROM source_rate_event "
                "ORDER BY id DESC LIMIT ?",
                (RATE_EVENTS_SHOWN,),
            ).fetchall()
        return {
            "settings": settings.model_dump(),
            "configured": {key: bool(secrets.get(key)) for key in sorted(SECRET_NAMES)},
            "revision": revision,
            "key_lost": key_lost,
            "rate_events": [dict(event) for event in events],
        }

    @staticmethod
    def validate(changes: dict[str, str]) -> None:
        if not changes.keys() <= SECRET_NAMES:
            raise InvalidInput("Unknown source credential")
        if any(
            len(value) > 16384
            or (value != "" and not value.strip())
            or any(ord(c) < 32 or ord(c) > 126 for c in value)
            for value in changes.values()
        ):
            raise InvalidInput("Invalid credential characters or size", code="invalid_credential")
        proxy = changes.get("proxy")
        if proxy:
            try:
                value = urlsplit(proxy)
                _ = value.port
            except ValueError as exc:
                raise InvalidInput("Expected an HTTP or SOCKS proxy URL", code="invalid_proxy") from exc
            if (
                value.scheme not in {"http", "https", "socks5", "socks5h"}
                or not value.hostname
                or value.fragment
                or value.path not in {"", "/"}
            ):
                raise InvalidInput("Expected an HTTP or SOCKS proxy URL", code="invalid_proxy")

    def save(self, settings: SourceSettings, changes: dict[str, str], expected_revision: int) -> int:
        self.validate(changes)
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM source_config").fetchone()
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RevisionConflict("Source settings changed")
            secrets = (
                {key: value for key, value in self.decrypt(row["secrets"]).items() if key in SECRET_NAMES}
                if row
                else {}
            )
            secrets.update({key: value.strip() for key, value in changes.items()})
            db.execute(
                "INSERT INTO source_config VALUES(1,?,?,?) ON CONFLICT(singleton) DO UPDATE SET "
                "settings_json=excluded.settings_json,secrets=excluded.secrets,revision=excluded.revision",
                (
                    settings.model_dump_json(),
                    self.encrypt(secrets) if any(secrets.values()) else "",
                    revision + 1,
                ),
            )
        return revision + 1
