"""Two-tier auth (owner/reader) and secrets-at-rest (§5.6)."""

from __future__ import annotations

from .crypto import SecretBox, load_or_create_keyfile
from .security import (
    Identity,
    Role,
    generate_api_key,
    hash_api_key,
    hash_passcode,
    verify_passcode,
)

__all__ = [
    "Identity",
    "Role",
    "SecretBox",
    "generate_api_key",
    "hash_api_key",
    "hash_passcode",
    "load_or_create_keyfile",
    "verify_passcode",
]
