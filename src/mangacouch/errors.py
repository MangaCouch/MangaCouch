"""Domain exceptions with stable API error codes.

Services raise these for expected, user-facing conditions; the HTTP layer maps
exactly these to 4xx responses with ``{"error": code, "message": text}``.
NotFound subclasses KeyError and the others ValueError so existing callers that
catch the builtin types keep working.
"""

from __future__ import annotations


class _Coded:
    code = "internal_error"
    message: str

    def _setup(self, message: str, code: str | None) -> None:
        self.message = message
        if code is not None:
            self.code = code

    def __str__(self) -> str:
        return self.message


class NotFound(_Coded, KeyError):
    code = "not_found"

    def __init__(self, message: str = "Not found", *, code: str | None = None):
        super().__init__(message)
        self._setup(message, code)


class InvalidInput(_Coded, ValueError):
    code = "invalid_input"

    def __init__(self, message: str = "Invalid input", *, code: str | None = None):
        super().__init__(message)
        self._setup(message, code)


class RevisionConflict(_Coded, ValueError):
    code = "revision_conflict"

    def __init__(self, message: str = "Changed elsewhere; reload and retry", *, code: str | None = None):
        super().__init__(message)
        self._setup(message, code)


class RateLimited(_Coded, ValueError):
    code = "rate_limited"

    def __init__(self, message: str = "Too many attempts", *, retry_after: int = 60):
        super().__init__(message)
        self._setup(message, None)
        self.retry_after = max(1, retry_after)
