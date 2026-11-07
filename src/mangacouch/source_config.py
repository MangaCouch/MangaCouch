from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import suppress
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated, BinaryIO, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from pydantic import Field, field_validator

from .database import Library, RevisionConflict
from .errors import InvalidInput
from .files import publish_file, sync_directory
from .models import Model, canonical_json

SECRET_NAMES = {"eh_cookie", "ex_cookie", "nh_cookie", "nh_key", "proxy"}
_KEY_LOCK = threading.Lock()


def _package_version() -> str:
    try:
        return version("mangacouch")
    except PackageNotFoundError:
        return "0"


DEFAULT_USER_AGENT = f"MangaCouch/{_package_version()}"


def try_lock(path: Path, message: str) -> BinaryIO:
    """Take an exclusive non-blocking lock on `path`; the caller closes the handle to release it."""
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.write(b"0")
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise ValueError(message) from None
    return handle


class SourceSettings(Model):
    download_limit_bytes: Annotated[int, Field(gt=0, le=1024**5)] = 32 * 1024**3
    comments: bool = True
    auto_free: bool = False
    default_quality: Literal["org", "res"] = "org"
    user_agent: Annotated[str, Field(min_length=1, max_length=512)] = DEFAULT_USER_AGENT

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
    """Source credentials (cookies, nHentai key, proxy) encrypted with a Fernet key file.

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
                key = Fernet.generate_key()
                with tempfile.NamedTemporaryFile(
                    prefix=".mangacouch-key-", dir=self.path.parent, delete=False
                ) as stream:
                    temporary = Path(stream.name)
                    stream.write(key)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    with suppress(FileExistsError):
                        publish_file(temporary, self.path)
                    sync_directory(self.path.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            if self.path.is_symlink():
                raise ValueError("Source key must be a regular file")
            try:
                return Fernet(self.path.read_bytes())
            except (OSError, ValueError):
                raise ValueError("Source key is missing or invalid; restore the matching key") from None

    def encrypt(self, value: dict) -> str:
        return self.cipher(create=True).encrypt(canonical_json(value).encode()).decode()

    def decrypt(self, value: str | None) -> dict:
        if not value:
            return {}
        try:
            return json.loads(self.cipher().decrypt(value.encode()))
        except (InvalidToken, ValueError):
            raise ValueError("Source credentials cannot be decrypted with this key") from None

    def reset_credentials(self) -> None:
        """Discard inaccessible remote credentials, keeping non-secret settings."""
        lock_path = self.library.path.with_name(self.library.path.name + ".downloads.lock")
        with _KEY_LOCK, try_lock(lock_path, "Stop serve before resetting source credentials"):
            with self.library.transaction() as db:
                db.execute("UPDATE source_config SET secrets='',revision=revision+1")
                db.execute(
                    "UPDATE download_job SET remote_secret=NULL,state='paused',revision=revision+1 "
                    "WHERE remote_secret IS NOT NULL AND state NOT IN ('done','cancelled')"
                )
                db.execute("UPDATE download_job SET remote_secret=NULL WHERE remote_secret IS NOT NULL")
            self.path.unlink(missing_ok=True)

    def load(self) -> tuple[SourceSettings, dict[str, str], int]:
        with self.library.connection() as db:
            row = db.execute("SELECT * FROM source_config").fetchone()
        if row is None:
            return SourceSettings(), {}, 0
        settings = json.loads(row["settings_json"])
        return (
            SourceSettings.model_validate(settings),
            {key: value for key, value in self.decrypt(row["secrets"]).items() if key in SECRET_NAMES},
            row["revision"],
        )

    def public(self) -> dict:
        settings, secrets, revision = self.load()
        return {
            "settings": settings.model_dump(),
            "configured": {key: bool(secrets.get(key)) for key in sorted(SECRET_NAMES)},
            "revision": revision,
        }

    @staticmethod
    def validate(changes: dict[str, str]) -> None:
        if not changes.keys() <= SECRET_NAMES:
            raise InvalidInput("Unknown source credential")
        if any(
            len(value) > 16384 or any(ord(c) < 32 or ord(c) > 126 for c in value)
            for value in changes.values()
        ):
            raise InvalidInput("Invalid credential characters or size")
        proxy = changes.get("proxy")
        if proxy:
            value = urlsplit(proxy)
            if (
                value.scheme not in {"http", "https", "socks5", "socks5h"}
                or not value.hostname
                or value.fragment
                or value.path not in {"", "/"}
            ):
                raise InvalidInput("Expected an HTTP or SOCKS proxy URL")

    def save(self, settings: SourceSettings, changes: dict[str, str], expected_revision: int) -> int:
        self.validate(changes)
        with self.library.transaction() as db:
            row = db.execute("SELECT * FROM source_config").fetchone()
            revision = row["revision"] if row else 0
            if revision != expected_revision:
                raise RevisionConflict("Source settings changed")
            secrets = self.decrypt(row["secrets"]) if row else {}
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
