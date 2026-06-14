"""Plugin routes — list plugins + their config; set per-plugin config (secrets encrypted) (§5.4)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ...state import AppContext
from ..deps import get_context, require_owner, require_reader

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

_SECRET_MASK = "••••••••"


@router.get("")
def list_plugins(
    _: object = Depends(require_reader), ctx: AppContext = Depends(get_context)
) -> dict[str, Any]:
    out = []
    for info in ctx.registry.all_info():
        stored = ctx.plugin_config(info.namespace)
        secret_keys = {p.name for p in info.parameters if p.secret}
        config = {
            k: (_SECRET_MASK if (k in secret_keys and v) else v) for k, v in stored.items()
        }
        out.append(
            {
                "namespace": info.namespace,
                "name": info.name,
                "type": info.type.value,
                "version": info.version,
                "description": info.description,
                "author": info.author,
                "cooldown": info.cooldown,
                "login_from": info.login_from,
                "parameters": [p.model_dump() for p in info.parameters],
                "config": config,
            }
        )
    return {"plugins": out}


class PluginConfigBody(BaseModel):
    values: dict[str, Any]


@router.post("/{namespace}/config")
def set_config(
    namespace: str,
    body: PluginConfigBody,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
) -> dict[str, Any]:
    info = ctx.registry.info(namespace)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    secret_keys = {p.name for p in info.parameters if p.secret}
    # Ignore masked secret values (means "unchanged").
    values = {k: v for k, v in body.values.items() if not (k in secret_keys and v == _SECRET_MASK)}
    ctx.set_plugin_config(namespace, values, secret_keys)
    return {"namespace": namespace, "saved": sorted(values.keys())}
