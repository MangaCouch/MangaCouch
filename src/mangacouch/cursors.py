"""Opaque keyset cursors for paged JSON responses.

A page returns its continuation as `after` (null on the last page); the client sends it back
unchanged as the `after` query parameter (or body field). Clients never build or read one.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from .errors import InvalidInput
from .models import canonical_json

MAX_CURSOR = 2048


def encode_cursor(*values: str | int) -> str:
    return base64.urlsafe_b64encode(canonical_json(list(values)).encode()).decode().rstrip("=")


def decode_cursor(value: str, *types: type, signed: bool = False) -> tuple[Any, ...]:
    """The values of a cursor made by `encode_cursor`, checked against `types` (str or int)."""
    try:
        if len(value) <= MAX_CURSOR:
            values = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))
            if (
                isinstance(values, list)
                and len(values) == len(types)
                and all(
                    type(item) is kind and (kind is not int or (-(2**63) if signed else 0) <= item < 2**63)
                    for item, kind in zip(values, types, strict=True)
                )
            ):
                return tuple(values)
    except (ValueError, TypeError, binascii.Error):
        pass
    raise InvalidInput("Invalid page cursor")
