"""Sign-in, sessions, credentials and passcode routes."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr

from .auth import KeyKind, client_address
from .models import Model
from .services import Services, require_manager, require_session

LOGGER = logging.getLogger(__name__)


class LoginInput(Model):
    passcode: SecretStr
    label: Annotated[str, Field(min_length=1, max_length=128)] = "Web device"


class KeyInput(Model):
    kind: KeyKind
    label: Annotated[str, Field(min_length=1, max_length=128)]


class PasscodeInput(Model):
    replacement: SecretStr
    revoke_keys: bool = False


def auth_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/auth")
    auth = services.auth
    proxies = services.settings.trusted_proxies
    unlisted_proxies: set[str] = set()

    def client(request: Request) -> str:
        peer = request.client.host if request.client else ""
        forwarded = request.headers.getlist("x-forwarded-for")
        if forwarded and not proxies and peer not in unlisted_proxies and len(unlisted_proxies) < 16:
            # Once per peer: behind an unlisted reverse proxy every client shares the proxy's limit.
            unlisted_proxies.add(peer)
            LOGGER.warning(
                "Sign-in through %s carries X-Forwarded-For; list the proxy in MANGACOUCH_TRUSTED_PROXIES "
                "so attempts count per client",
                peer,
            )
        return client_address(peer, forwarded, proxies)

    @router.post("/login")
    def login(request: Request, data: LoginInput, response: Response) -> dict:
        identifier, token = auth.login(data.passcode.get_secret_value(), data.label, client=client(request))
        with auth.library.connection() as db:
            expires_at = db.execute("SELECT expires_at FROM credential WHERE id=?", (identifier,)).fetchone()[
                0
            ]
        services.set_cookie(response, token, expires_at, request)
        return {"id": identifier, "kind": "session", "expires_at": expires_at}

    @router.get("/me")
    def me(request: Request) -> dict:
        return asdict(request.state.principal)

    @router.post("/rotate")
    def rotate(request: Request, response: Response) -> dict:
        require_session(request)
        rotated = auth.rotate(
            request.cookies.get(
                services.settings.session_cookie(
                    request.url.netloc, request.url.scheme, request.headers.get("origin"), request.cookies
                ),
                "",
            )
        )
        if rotated:
            services.set_cookie(response, *rotated, request)
        return {"rotated": rotated is not None, "expires_at": rotated[1] if rotated else None}

    @router.post("/logout", status_code=204)
    def logout(request: Request, response: Response) -> None:
        auth.revoke(require_session(request).id)
        services.clear_cookie(response, request)

    @router.get("/credentials")
    def credentials(request: Request) -> dict:
        require_manager(request)
        return {"items": auth.list_credentials()}

    @router.post("/keys")
    def create_key(request: Request, data: KeyInput) -> dict:
        require_session(request)
        identifier, token = auth.create_key(data.kind, data.label)
        return {"id": identifier, "key": token, "kind": data.kind}

    @router.delete("/credentials/{credential_id}", status_code=204)
    def revoke(credential_id: str) -> None:
        auth.revoke(credential_id)

    @router.post("/passcode", status_code=204)
    def change_passcode(request: Request, data: PasscodeInput, response: Response) -> None:
        require_session(request)
        auth.change_passcode(data.replacement.get_secret_value(), revoke_keys=data.revoke_keys)
        services.clear_cookie(response, request)

    return router
