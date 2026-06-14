"""FastAPI dependencies: auth resolution (role gating) and per-request DB sessions (§5.6, §6.1).

A caller is identified by ``Authorization: Bearer <base64(apiKey)>`` **or** a ``?key=`` query param
(media ``<img>`` tags can't set headers). The key may be a long-lived owner/reader API key or a
passcode-login session token. A dependency maps role → allowed verbs on every route.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.security import Identity, Role, decode_bearer, hash_api_key
from ..db.base import get_sessionmaker
from ..db.models import AuthCredential, AuthSession
from ..state import AppContext


def get_context(request: Request) -> AppContext:
    ctx = getattr(request.app.state, "context", None)
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "application not ready")
    return ctx


def get_db() -> Iterator[Session]:
    session = get_sessionmaker()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    key = request.query_params.get("key")
    return key.strip() if key else None


def current_identity(request: Request, db: Session = Depends(get_db)) -> Identity:
    token = _extract_token(request)
    if not token:
        return Identity(None)
    raw = decode_bearer(token)
    if not raw:
        return Identity(None)
    digest = hash_api_key(raw)

    cred = db.scalar(
        select(AuthCredential).where(
            AuthCredential.api_key_hash == digest, AuthCredential.enabled.is_(True)
        )
    )
    if cred is not None:
        return Identity(Role(cred.role))

    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    if sess is not None:
        return Identity(Role(sess.role))
    return Identity(None)


def require_reader(identity: Identity = Depends(current_identity)) -> Identity:
    """Any authenticated role (owner or reader)."""
    if not identity.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return identity


def require_owner(identity: Identity = Depends(current_identity)) -> Identity:
    if not identity.is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner privileges required")
    return identity
