"""Sign-in, sessions, credentials and passcode routes."""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Request, Response
from pydantic import Field, SecretStr

from .auth import KeyKind
from .models import Model
from .services import Services, require_manager, require_session


class LoginInput(Model):
    passcode: SecretStr
    label: Annotated[str, Field(min_length=1, max_length=128)] = "Web device"


class KeyInput(Model):
    kind: KeyKind
    label: Annotated[str, Field(min_length=1, max_length=128)]


class PasscodeInput(Model):
    replacement: SecretStr


def auth_router(services: Services) -> APIRouter:
    router = APIRouter(prefix="/api/auth")
    auth = services.auth

    @router.post("/login")
    def login(request: Request, data: LoginInput, response: Response) -> dict:
        client = request.client.host if request.client else ""
        identifier, token = auth.login(data.passcode.get_secret_value(), data.label, client=client)
        expires_at = auth.authenticate(token, session=True).expires_at or 0
        services.set_cookie(response, token, expires_at)
        return {"id": identifier, "kind": "session", "expires_at": expires_at}

    @router.get("/me")
    def me(request: Request) -> dict:
        return asdict(request.state.principal)

    @router.post("/rotate")
    def rotate(request: Request, response: Response) -> dict:
        require_session(request)
        rotated = auth.rotate(request.cookies.get(services.settings.cookie_name, ""))
        if rotated:
            services.set_cookie(response, *rotated)
        return {"rotated": rotated is not None, "expires_at": rotated[1] if rotated else None}

    @router.post("/logout")
    def logout(request: Request, response: Response) -> dict:
        auth.revoke(require_session(request).id)
        services.clear_cookie(response)
        return {"locked": True}

    @router.get("/credentials")
    def credentials(request: Request) -> dict:
        require_manager(request)
        return {"credentials": auth.list_credentials()}

    @router.post("/keys")
    def create_key(data: KeyInput) -> dict:
        identifier, token = auth.create_key(data.kind, data.label)
        return {"id": identifier, "key": token, "kind": data.kind}

    @router.delete("/credentials/{credential_id}")
    def revoke(credential_id: str) -> dict:
        auth.revoke(credential_id)
        return {"revoked": True}

    @router.post("/passcode")
    def change_passcode(request: Request, data: PasscodeInput, response: Response) -> dict:
        require_session(request)
        auth.change_passcode(data.replacement.get_secret_value())
        services.clear_cookie(response)
        return {"sessions_revoked": True, "api_keys_unchanged": True}

    return router
