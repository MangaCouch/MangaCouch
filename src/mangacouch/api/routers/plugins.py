"""Plugin routes (§5.4) — list plugins + their config; set per-plugin config (secrets encrypted);
run a metadata plugin against an archive (the "gallery was taken down" rescue flow)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ...acquisition.client import build_client
from ...plugins.base import MetadataContext, MetadataPlugin, PluginError
from ...state import AppContext
from ..deps import get_context, get_db, require_owner, require_reader

router = APIRouter(prefix="/api/plugins", tags=["plugins"])

log = logging.getLogger("mangacouch.plugins")

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


class RunMetadataBody(BaseModel):
    archive_id: str
    url: str | None = None  # oneshot gallery URL override (e.g. a nhentai/hitomi link)
    mode: Literal["merge", "replace"] = "merge"
    set_title: bool | None = None  # None = only fill an empty/filename-derived title


@router.post("/{namespace}/run")
def run_metadata_plugin(
    namespace: str,
    body: RunMetadataBody,
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    from .archives import _archive_path, _load, _replace_tags, _resync_sidecar

    plugin = ctx.registry.get(namespace)
    info = ctx.registry.info(namespace)
    if plugin is None or info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "plugin not found")
    if not isinstance(plugin, MetadataPlugin):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a metadata plugin")

    arch = _load(db, body.archive_id)
    existing = [f"{t.tag.namespace}:{t.tag.value}" if t.tag.namespace else t.tag.value
                for t in arch.tags]

    session, owns_session = _plugin_session(ctx, info.login_from)
    try:
        with ctx.rate_limiter.slot(namespace):
            result = plugin.get_tags(
                MetadataContext(
                    archive_id=arch.id,
                    title=arch.title,
                    source_url=body.url or arch.source_url,
                    config=ctx.plugin_config(namespace),
                    session=session,
                    file_path=_archive_path(ctx, arch),
                    existing_tags=existing,
                )
            )
    except Exception as exc:  # a plugin must never take the API down
        log.exception("metadata plugin %s crashed", namespace)
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"plugin crashed: {exc}") from exc
    finally:
        if owns_session:
            session.close()

    if result.error:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, result.error)

    new_tags = result.tags if body.mode == "replace" else [*existing, *result.tags]
    _replace_tags(db, arch, new_tags)

    title_applied = False
    if result.title and _should_set_title(body.set_title, arch):
        arch.title = result.title
        title_applied = True
    if result.summary and not arch.summary:
        arch.summary = result.summary

    db.flush()
    db.refresh(arch)
    tag_strings = [f"{t.tag.namespace}:{t.tag.value}" if t.tag.namespace else t.tag.value
                   for t in arch.tags]
    ctx.search.upsert(arch.id, arch.title, tag_strings)
    _resync_sidecar(ctx, arch, tag_strings)

    from ..serialization import serialize_archive

    return {
        "namespace": namespace,
        "title_applied": title_applied,
        "new_title": result.title,
        "added_tags": sorted(set(tag_strings) - set(existing)),
        "archive": serialize_archive(arch, ctx.translator, db=db, detail=True),
    }


def _plugin_session(ctx: AppContext, login_from: str | None):
    """The HTTP session for a manual plugin run: the cached login session when the plugin
    declares one, else a fresh client honouring the acquisition proxy/User-Agent settings.
    Returns ``(client, owns_session)`` — only close clients we created here."""
    if login_from:
        try:
            return ctx.download_worker.login_session(login_from), False
        except PluginError:
            pass  # not configured — fall through to an anonymous client
    acq = ctx.config.acquisition
    client = build_client(
        proxy=acq.proxy or None,
        proxy_scope=acq.proxy_scope,
        user_agent=acq.user_agent,
    )
    return client, True


def _should_set_title(set_title: bool | None, arch) -> bool:
    if set_title is not None:
        return set_title
    # Auto mode: only overwrite a title that is empty or just the filename.
    current = (arch.title or "").strip()
    from pathlib import Path

    stem = Path(arch.original_filename or arch.rel_path).stem
    return not current or current == stem
