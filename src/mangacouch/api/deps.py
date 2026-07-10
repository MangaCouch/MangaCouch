"""FastAPI dependencies: auth resolution (role gating) and per-request DB sessions (§5.6, §6.1).

A caller is identified by ``Authorization: Bearer <base64(apiKey)>``. Media routes (page /
thumbnail / OPDS — ``<img>`` tags and OPDS readers can't set headers) additionally accept a
``?key=`` query param via the ``*_media`` dependencies; ordinary API routes do **not**, so keys
don't leak into proxy logs and browser history. The key may be the long-lived owner API key
or a passcode-login session token (idle-expired server-side).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.security import Identity, Role, decode_bearer, hash_api_key
from ..db.base import get_sessionmaker
from ..db.models import AppConfig, AuthCredential, AuthSession
from ..state import AppContext

# Passcode-login session tokens expire after this much inactivity; last_seen is written at most
# once an hour so a busy reader doesn't turn every GET into a DB write.
SESSION_IDLE_MAX = timedelta(days=30)
_LAST_SEEN_WRITE_INTERVAL = timedelta(hours=1)


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


def _extract_token(request: Request, *, allow_query: bool) -> str | None:
    auth = request.headers.get("Authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if allow_query:
        key = request.query_params.get("key")
        return key.strip() if key else None
    return None


def _identity_for_token(token: str | None, db: Session, *, allow_media_token: bool = False) -> Identity:
    if not token:
        return Identity(None)
    raw = decode_bearer(token)
    if not raw:
        return Identity(None)
    digest = hash_api_key(raw)

    if allow_media_token:
        # The stable media token authenticates media routes only (thumbnails/pages/OPDS),
        # so the rotating session tokens never end up pinned inside cached image URLs.
        row = db.get(AppConfig, "media_token_hash")
        if row is not None and row.value == digest:
            return Identity(Role.OWNER)

    cred = db.scalar(
        select(AuthCredential).where(
            AuthCredential.api_key_hash == digest, AuthCredential.enabled.is_(True)
        )
    )
    if cred is not None and cred.role == Role.OWNER.value:
        return Identity(Role.OWNER)

    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest))
    if sess is not None and sess.role == Role.OWNER.value:
        now = datetime.now(UTC)
        last = sess.last_seen if sess.last_seen.tzinfo else sess.last_seen.replace(tzinfo=UTC)
        if now - last > SESSION_IDLE_MAX:
            db.delete(sess)  # idle-expired — a leaked token is not valid forever
            return Identity(None)
        if now - last > _LAST_SEEN_WRITE_INTERVAL:
            sess.last_seen = now
        return Identity(Role.OWNER)
    return Identity(None)


def current_identity(request: Request, db: Session = Depends(get_db)) -> Identity:
    return _identity_for_token(_extract_token(request, allow_query=False), db)


def current_identity_media(request: Request, db: Session = Depends(get_db)) -> Identity:
    """Media/OPDS variant: also accepts ``?key=`` (image tags/readers can't set headers)
    and the long-lived media token."""
    return _identity_for_token(
        _extract_token(request, allow_query=True), db, allow_media_token=True
    )


def require_auth(identity: Identity = Depends(current_identity)) -> Identity:
    """Any authenticated caller (single-user model: authenticated == owner)."""
    if not identity.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return identity


def require_auth_media(identity: Identity = Depends(current_identity_media)) -> Identity:
    if not identity.is_authenticated:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required")
    return identity


def require_owner(identity: Identity = Depends(current_identity)) -> Identity:
    if not identity.is_owner:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "owner privileges required")
    return identity
