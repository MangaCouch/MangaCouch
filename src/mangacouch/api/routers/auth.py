"""Auth routes — passcode login → a session token; logout; whoami (§5.6)."""

from __future__ import annotations

import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ...auth.security import (
    Identity,
    Role,
    decode_bearer,
    generate_api_key,
    hash_api_key,
    hash_passcode,
    verify_passcode,
)
from ...db.models import AuthCredential, AuthSession
from ..deps import current_identity, get_db, require_owner, require_reader

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    passcode: str


class LoginResponse(BaseModel):
    api_key: str
    role: str


# Brute-force guard: passcodes can be short, so failed logins back off per client IP with an
# escalating lockout (also caps the CPU spent on argon2 verifies). In-memory is fine — a restart
# resetting the counters is acceptable.
_FAIL_LOCK = threading.Lock()
_FAILURES: dict[str, tuple[int, float]] = {}  # ip -> (consecutive failures, locked_until)
_FREE_ATTEMPTS = 5
_BASE_LOCKOUT_S = 30.0
_MAX_LOCKOUT_S = 900.0


def _throttle_check(ip: str) -> None:
    with _FAIL_LOCK:
        _count, until = _FAILURES.get(ip, (0, 0.0))
        if time.monotonic() < until:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                "too many failed attempts — try again later",
            )


def _throttle_failure(ip: str) -> None:
    with _FAIL_LOCK:
        count, _until = _FAILURES.get(ip, (0, 0.0))
        count += 1
        lock_for = 0.0
        if count >= _FREE_ATTEMPTS:
            lock_for = min(_BASE_LOCKOUT_S * 2 ** (count - _FREE_ATTEMPTS), _MAX_LOCKOUT_S)
        _FAILURES[ip] = (count, time.monotonic() + lock_for)


def _throttle_reset(ip: str) -> None:
    with _FAIL_LOCK:
        _FAILURES.pop(ip, None)


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> LoginResponse:
    ip = request.client.host if request.client else "unknown"
    _throttle_check(ip)
    # Owner is checked first so a shared owner/reader passcode resolves to the higher role.
    for role in (Role.OWNER, Role.READER):
        cred = db.get(AuthCredential, role.value)
        if cred and cred.enabled and verify_passcode(cred.passcode_hash, body.passcode):
            token = generate_api_key()
            db.add(AuthSession(token_hash=hash_api_key(token), role=role.value))
            _throttle_reset(ip)
            return LoginResponse(api_key=token, role=role.value)
    _throttle_failure(ip)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid passcode")


@router.post("/logout")
def logout(request: Request, _: Identity = Depends(require_reader), db: Session = Depends(get_db)):
    auth = request.headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.query_params.get("key")
    if token:
        raw = decode_bearer(token)
        if raw:
            db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_api_key(raw)))
    return {"ok": True}


class ChangePasscodeRequest(BaseModel):
    role: str = "owner"  # which credential to change: "owner" | "reader"
    new_passcode: str
    current_passcode: str | None = None  # required when changing the OWNER passcode


@router.post("/passcode")
def change_passcode(
    body: ChangePasscodeRequest,
    _: Identity = Depends(require_owner),
    db: Session = Depends(get_db),
) -> dict:
    """Owner-only: change the owner or reader passcode. The long-lived API key is unaffected."""
    role = body.role.lower()
    if role not in ("owner", "reader"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "role must be owner|reader")
    if len(body.new_passcode) < 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "passcode must be at least 4 characters")

    owner = db.get(AuthCredential, "owner")
    # Changing the owner passcode requires confirming the current one (defence in depth).
    if role == "owner" and not (
        owner and verify_passcode(owner.passcode_hash, body.current_passcode or "")
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "current owner passcode is incorrect")

    cred = db.get(AuthCredential, role)
    if cred is None:
        cred = AuthCredential(role=role, enabled=True)
        db.add(cred)
    cred.passcode_hash = hash_passcode(body.new_passcode)
    cred.enabled = True
    # Existing login sessions stay valid; only the passcode used for future logins changed.
    return {"ok": True, "role": role}


@router.get("/me")
def me(identity: Identity = Depends(current_identity)) -> dict:
    return {
        "authenticated": identity.is_authenticated,
        "role": identity.role.value if identity.role else None,
    }


@router.get("/status")
def auth_status(db: Session = Depends(get_db)) -> dict:
    """Whether credentials have been provisioned (so the UI can show a first-run hint)."""
    owner = db.scalar(select(AuthCredential).where(AuthCredential.role == "owner"))
    reader = db.scalar(select(AuthCredential).where(AuthCredential.role == "reader"))
    return {
        "owner_configured": bool(owner and owner.passcode_hash),
        "reader_configured": bool(reader and reader.passcode_hash),
    }
