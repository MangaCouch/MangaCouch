"""Secrets at rest (§5.6).

Login cookies and any credential material in ``plugin_config`` are encrypted with a key from a
generated keyfile in ``database/`` (created ``0600`` on first run). The keyfile sits beside the DB
so a backup of ``database/`` is self-sufficient — and is therefore as sensitive as the DB itself.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


def load_or_create_keyfile(path: Path) -> bytes:
    """Return the Fernet key bytes, generating a ``0600`` keyfile on first run."""
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write with restrictive permissions from the start where the OS supports it.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    with contextlib.suppress(OSError):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # best-effort on Windows
    return key


class SecretBox:
    """Encrypt/decrypt short secret strings (cookies, API tokens) with the keyfile key."""

    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_keyfile(cls, path: Path) -> SecretBox:
        return cls(load_or_create_keyfile(path))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:  # wrong/rotated key, or corrupted value
            raise ValueError("could not decrypt secret (wrong keyfile?)") from exc
