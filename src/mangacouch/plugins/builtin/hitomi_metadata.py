"""Hitomi Metadata plugin (§5.4) — tags/title from hitomi.la's gallery JS.

A fallback metadata source for when the e(x)hentai gallery is gone. Hitomi has no JSON API; the
gallery info ships as a ``var galleryinfo = {...}`` JS file on the ltn CDN. The gallery is resolved
from a hitomi URL (``source_url``), a ``source:hitomi.la/...`` tag, or an ID in the filename.
"""

from __future__ import annotations

import json
import re

import httpx

from ..base import (
    MetadataContext,
    MetadataPlugin,
    MetadataResult,
    PluginInfo,
    PluginType,
)

# The gallery-info JS lives on hitomi's CDN domain (see LANraragi's Hitomi plugin).
GALLERY_JS = "https://ltn.gold-usergeneratedcontent.net/galleries/{gid}.js"

_URL_RE = re.compile(r"hitomi\.la/[^\s,]*?(\d+)\.html", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(
    r"^source:\s*(?:https?://)?hitomi\.la/[^\s,]*?(\d+)\.html", re.IGNORECASE
)
_FILENAME_ID_RE = re.compile(r"\{(\d+)\}|^(\d+)\b")
_GALLERYINFO_RE = re.compile(r"galleryinfo\s*=\s*(\{.*\})", re.DOTALL)


def gallery_id_from_url(url: str | None) -> int | None:
    if not url:
        return None
    m = _URL_RE.search(url)
    return int(m.group(1)) if m else None


def gallery_id_from_tags(tags: list[str]) -> int | None:
    for tag in tags:
        m = _SOURCE_TAG_RE.match(tag.strip())
        if m:
            return int(m.group(1))
    return None


def gallery_id_from_filename(stem: str) -> int | None:
    """Accept "{123456} Title" anywhere or a leading bare ID ("123456 Title")."""
    m = _FILENAME_ID_RE.search(stem)
    if m is None:
        return None
    return int(m.group(1) or m.group(2))


def parse_galleryinfo(js_text: str) -> dict:
    m = _GALLERYINFO_RE.search(js_text)
    if m is None:
        raise ValueError("no galleryinfo object in the hitomi JS")
    return json.loads(m.group(1))


def tags_from_galleryinfo(data: dict) -> list[str]:
    """Hitomi's galleryinfo → "namespace:value" strings (same mapping as LANraragi)."""
    out: list[str] = []
    for tag in data.get("tags") or []:
        name = str(tag.get("tag", "")).strip()
        if not name:
            continue
        if tag.get("male"):
            out.append(f"male:{name}")
        elif tag.get("female"):
            out.append(f"female:{name}")
        else:
            out.append(name)
    for arrayname, namespace in (
        ("parodys", "parody"),
        ("artists", "artist"),
        ("groups", "group"),
        ("characters", "character"),
    ):
        for entry in data.get(arrayname) or []:
            value = str(entry.get(namespace, "")).strip()
            if value:
                out.append(f"{namespace}:{value}")
    if data.get("type"):
        out.append(f"type:{data['type']}")
    if data.get("language"):
        out.append(f"language:{data['language']}")
    return out


class HitomiMetadataPlugin(MetadataPlugin):
    NAMESPACE = "hitomi_metadata"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="Hitomi Metadata",
            type=PluginType.METADATA,
            description=(
                "Title and tags from hitomi.la — useful when the e-hentai gallery was taken "
                "down. Resolves the gallery from a hitomi URL, a source: tag, or a {id} / "
                "leading-ID filename marker."
            ),
            author="MangaCouch",
            cooldown=4.0,
        )

    def get_tags(self, ctx: MetadataContext) -> MetadataResult:
        gid = gallery_id_from_url(ctx.source_url) or gallery_id_from_tags(ctx.existing_tags)
        if gid is None and ctx.file_path is not None:
            gid = gallery_id_from_filename(ctx.file_path.stem)
        if gid is None:
            return MetadataResult(error="No matching Hitomi gallery found.")

        session = ctx.session or httpx.Client(follow_redirects=True, timeout=30.0)
        owns_session = ctx.session is None
        try:
            resp = session.get(GALLERY_JS.format(gid=gid))
            resp.raise_for_status()
            data = parse_galleryinfo(resp.text)
        except httpx.HTTPStatusError as exc:
            return MetadataResult(error=f"hitomi returned HTTP {exc.response.status_code}")
        except httpx.HTTPError as exc:
            return MetadataResult(error=f"network error: {exc}")
        except (ValueError, json.JSONDecodeError) as exc:
            return MetadataResult(error=f"could not parse hitomi gallery info: {exc}")
        finally:
            if owns_session:
                session.close()

        tags = tags_from_galleryinfo(data)
        if not tags:
            return MetadataResult(error="Gallery has no tags.")
        tags.append(f"source:hitomi.la/galleries/{gid}.html")
        return MetadataResult(tags=tags, title=data.get("title") or None)
