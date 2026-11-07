from __future__ import annotations

import hashlib
import re
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Literal

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from .database import Library
from .errors import InvalidInput, NotFound, RateLimited, RevisionConflict

Kind = Literal["management", "readonly", "downloads", "session"]
KeyKind = Literal["management", "readonly", "downloads"]
KEY_KINDS: tuple[KeyKind, ...] = ("management", "readonly", "downloads")
SESSION_SECONDS = 30 * 24 * 60 * 60
ROTATE_SECONDS = 15 * 60
GRACE_SECONDS = 60
CREDENTIAL_RETENTION = 30 * 24 * 60 * 60
PREFIX = {"management": "mcm_", "readonly": "mcr_", "downloads": "mcd_", "session": "mcs_"}
PASSWORD_HASHER = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)
LOGIN_WINDOW = 60
LOGIN_ATTEMPTS_PER_CLIENT = 10
LOGIN_ATTEMPTS_TOTAL = 30
GLOBAL_CLIENT = "*"
# Each Argon2 verification needs ~19 MiB; never run more than two at once.
_HASH_SLOTS = threading.BoundedSemaphore(2)


class Unauthorized(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    id: str
    kind: Kind
    label: str
    expires_at: int | None
    rotation_due: bool


def _digest(token: str) -> str:
    if not re.fullmatch(r"mc[mrds]_[A-Za-z0-9_-]{43}", token):
        raise Unauthorized("Invalid credentials")
    return hashlib.sha256(token.encode()).hexdigest()


def _passcode(value: str) -> None:
    if not 8 <= len(value) <= 256:
        raise InvalidInput("Passcode must contain 8 to 256 characters")


def _label(value: str) -> None:
    if not value.strip() or len(value) > 128:
        raise InvalidInput("Credential label must contain 1 to 128 characters")


class Auth:
    def __init__(self, library: Library, *, clock: Callable[[], float] = time.time):
        self.library = library
        self.clock = clock

    def configured(self) -> bool:
        with self.library.connection() as db:
            return db.execute("SELECT 1 FROM auth_settings").fetchone() is not None

    def initialize(self, passcode: str) -> None:
        _passcode(passcode)
        encoded = PASSWORD_HASHER.hash(passcode)
        with self.library.transaction() as db:
            if db.execute("SELECT 1 FROM auth_settings").fetchone():
                raise InvalidInput("Authentication is already initialized")
            db.execute("INSERT INTO auth_settings(singleton,passcode_hash) VALUES(1,?)", (encoded,))

    def _admit(self, client: str) -> None:
        """Count a login attempt per client and globally before any hashing happens."""
        now = int(self.clock())
        with self.library.transaction() as db:
            db.execute("DELETE FROM login_attempt WHERE window_start<=?", (now - LOGIN_WINDOW,))
            client = client[:256]
            for key, limit in ((client, LOGIN_ATTEMPTS_PER_CLIENT), (GLOBAL_CLIENT, LOGIN_ATTEMPTS_TOTAL)):
                row = db.execute(
                    "SELECT window_start,attempts FROM login_attempt WHERE client=?", (key,)
                ).fetchone()
                if row is not None and row["attempts"] >= limit:
                    raise RateLimited(
                        "Too many sign-in attempts; wait a minute and retry",
                        retry_after=row["window_start"] + LOGIN_WINDOW - now,
                    )
            for key in {client, GLOBAL_CLIENT}:
                db.execute(
                    "INSERT INTO login_attempt(client,window_start,attempts) VALUES(?,?,1) "
                    "ON CONFLICT(client) DO UPDATE SET attempts=attempts+1",
                    (key, now),
                )

    def _verify_passcode(self, passcode: str, client: str = "") -> int:
        with self.library.connection() as db:
            settings = db.execute("SELECT * FROM auth_settings").fetchone()
        if settings is None:
            raise Unauthorized("Authentication is not initialized")
        self._admit(client)
        valid = False
        if 1 <= len(passcode) <= 256:
            if not _HASH_SLOTS.acquire(timeout=10):
                raise RateLimited("Sign-in is busy; retry shortly", retry_after=5)
            try:
                with suppress(VerificationError):
                    valid = PASSWORD_HASHER.verify(settings["passcode_hash"], passcode)
            finally:
                _HASH_SLOTS.release()
        if not valid:
            raise Unauthorized("Invalid credentials")
        return settings["revision"]

    def _issue(self, db: sqlite3.Connection, kind: Kind, label: str) -> tuple[str, str]:
        _label(label)
        if kind not in PREFIX:
            raise InvalidInput("Invalid credential kind")
        now = int(self.clock())
        active = db.execute(
            "SELECT count(*) FROM credential WHERE revoked_at IS NULL "
            "AND (expires_at IS NULL OR expires_at>?)",
            (now,),
        ).fetchone()[0]
        if active >= 256:
            raise InvalidInput("Revoke unused credentials before creating more", code="too_many_credentials")
        token = PREFIX[kind] + secrets.token_urlsafe(32)
        credential_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO credential(id,kind,label,secret_hash,created_at,rotated_at,expires_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                credential_id,
                kind,
                label,
                _digest(token),
                now,
                now,
                now + SESSION_SECONDS if kind == "session" else None,
            ),
        )
        return credential_id, token

    def login(self, passcode: str, label: str, *, client: str = "") -> tuple[str, str]:
        revision = self._verify_passcode(passcode, client)
        with self.library.transaction() as db:
            if db.execute("SELECT revision FROM auth_settings").fetchone()[0] != revision:
                raise Unauthorized("Passcode changed; sign in again")
            return self._issue(db, "session", label)

    def create_key(self, kind: KeyKind, label: str) -> tuple[str, str]:
        if kind not in KEY_KINDS or not self.configured():
            raise InvalidInput("Initialize authentication and choose an API key kind")
        with self.library.transaction() as db:
            return self._issue(db, kind, label)

    def _lookup(self, db: sqlite3.Connection, token: str, session: bool) -> sqlite3.Row:
        digest, now = _digest(token), int(self.clock())
        row = db.execute(
            "SELECT * FROM credential WHERE secret_hash=? OR (previous_hash=? AND previous_until>?)",
            (digest, digest, now),
        ).fetchone()
        if (
            row is None
            or row["revoked_at"] is not None
            or (row["expires_at"] is not None and row["expires_at"] <= now)
            or (row["kind"] == "session") != session
            or not token.startswith(PREFIX[row["kind"]])
        ):
            raise Unauthorized("Invalid credentials")
        return row

    def authenticate(self, token: str, *, session: bool) -> Principal:
        with self.library.connection() as db:
            row = self._lookup(db, token, session)
        return Principal(
            row["id"],
            row["kind"],
            row["label"],
            row["expires_at"],
            row["kind"] == "session" and int(self.clock()) - row["rotated_at"] >= ROTATE_SECONDS,
        )

    def rotate(self, token: str) -> tuple[str, int] | None:
        """Replace a due session secret and slide its expiry forward."""
        now = int(self.clock())
        with self.library.transaction() as db:
            row = self._lookup(db, token, True)
            if row["secret_hash"] != _digest(token) or now - row["rotated_at"] < ROTATE_SECONDS:
                return None
            replacement = PREFIX["session"] + secrets.token_urlsafe(32)
            expires_at = now + SESSION_SECONDS
            db.execute(
                "UPDATE credential SET previous_hash=secret_hash,previous_until=?,secret_hash=?,rotated_at=?,"
                "expires_at=? WHERE id=?",
                (now + GRACE_SECONDS, _digest(replacement), now, expires_at, row["id"]),
            )
        return replacement, expires_at

    def list_credentials(self) -> list[dict]:
        cutoff = int(self.clock()) - CREDENTIAL_RETENTION
        with self.library.connection() as db:
            rows = db.execute(
                "SELECT id,kind,label,created_at,expires_at,revoked_at FROM credential "
                "WHERE coalesce(revoked_at,expires_at,?)>? ORDER BY created_at,id",
                (cutoff + 1, cutoff),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge(self) -> int:
        """Delete credentials revoked or expired more than the retention window ago."""
        cutoff = int(self.clock()) - CREDENTIAL_RETENTION
        with self.library.transaction() as db:
            return db.execute(
                "DELETE FROM credential WHERE coalesce(revoked_at,expires_at,?)<=?", (cutoff + 1, cutoff)
            ).rowcount

    def revoke(self, credential_id: str) -> None:
        with self.library.transaction() as db:
            result = db.execute(
                "UPDATE credential SET revoked_at=coalesce(revoked_at,?) WHERE id=?",
                (int(self.clock()), credential_id),
            )
            if result.rowcount != 1:
                raise NotFound("Credential not found")

    def change_passcode(self, new: str) -> None:
        _passcode(new)
        with self.library.connection() as db:
            row = db.execute("SELECT revision FROM auth_settings WHERE singleton=1").fetchone()
            if row is None:
                raise InvalidInput("Authentication is not initialized; run auth-init first")
            revision = row[0]
        encoded = PASSWORD_HASHER.hash(new)
        with self.library.transaction() as db:
            result = db.execute(
                "UPDATE auth_settings SET passcode_hash=?,revision=revision+1 WHERE revision=?",
                (encoded, revision),
            )
            if result.rowcount != 1:
                raise RevisionConflict("Passcode changed concurrently")
            db.execute(
                "UPDATE credential SET revoked_at=? WHERE kind='session' AND revoked_at IS NULL",
                (int(self.clock()),),
            )
