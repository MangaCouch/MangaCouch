"""Auth routes — passcode login → a session token; logout; whoami (§5.6)."""

from __future__ import annotations

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
    verify_passcode,
)
from ...db.models import AuthCredential, AuthSession
from ..deps import current_identity, get_db, require_reader

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    passcode: str


class LoginResponse(BaseModel):
    api_key: str
    role: str


@router.post("/login", response_model=LoginResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> LoginResponse:
    # Owner is checked first so a shared owner/reader passcode resolves to the higher role.
    for role in (Role.OWNER, Role.READER):
        cred = db.get(AuthCredential, role.value)
        if cred and cred.enabled and verify_passcode(cred.passcode_hash, body.passcode):
            token = generate_api_key()
            db.add(AuthSession(token_hash=hash_api_key(token), role=role.value))
            return LoginResponse(api_key=token, role=role.value)
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
