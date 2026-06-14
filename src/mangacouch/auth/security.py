"""Passcodes, API keys, and the owner/reader role model (§5.6).

Passcodes are hashed with argon2id. API keys are random ``secrets`` tokens, stored only as a SHA-256
hash (they are high-entropy, so a fast hash is appropriate and lets us look a key up by its hash).
"""

from __future__ import annotations

import base64
import binascii
import enum
import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id with library defaults


class Role(enum.StrEnum):
    OWNER = "owner"
    READER = "reader"

    @property
    def can_write(self) -> bool:
        return self is Role.OWNER


class Identity:
    """The resolved caller: a role, or anonymous."""

    __slots__ = ("role",)

    def __init__(self, role: Role | None) -> None:
        self.role = role

    @property
    def is_authenticated(self) -> bool:
        return self.role is not None

    @property
    def is_owner(self) -> bool:
        return self.role is Role.OWNER


# --- passcodes ------------------------------------------------------------------------------


def hash_passcode(passcode: str) -> str:
    return _hasher.hash(passcode)


def verify_passcode(stored_hash: str | None, passcode: str) -> bool:
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, passcode)
    except (VerifyMismatchError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    return _hasher.check_needs_rehash(stored_hash)


# --- API keys -------------------------------------------------------------------------------


def generate_api_key() -> str:
    """A URL-safe, high-entropy API key the client base64-encodes into the Bearer header."""
    return secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def verify_api_key(stored_hash: str | None, api_key: str) -> bool:
    if not stored_hash:
        return False
    return hmac.compare_digest(stored_hash, hash_api_key(api_key))


def decode_bearer(token: str) -> str | None:
    """Decode the ``base64(apiKey)`` Bearer token the clients send. Returns the raw API key.

    Tolerates a raw (non-base64) key too, so a hand-set header still works.
    """
    token = token.strip()
    try:
        decoded = base64.b64decode(token, validate=True).decode("utf-8")
        if decoded:
            return decoded
    except (binascii.Error, ValueError, UnicodeDecodeError):
        pass
    return token or None
