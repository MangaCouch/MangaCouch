"""nHentai Metadata plugin (§5.4) — tags/title from the nhentai JSON API.

A fallback metadata source for when the e(x)hentai gallery is gone. The gallery is resolved, in
order, from: an explicit nhentai URL (``source_url``), a ``source:nhentai.net/g/<id>`` tag, a
``{id}`` marker in the filename, and finally an optional title search.
"""

from __future__ import annotations

import contextlib
import re
from urllib.parse import quote

import httpx

from ..base import (
    MetadataComment,
    MetadataContext,
    MetadataPlugin,
    MetadataResult,
    PluginInfo,
    PluginParam,
    PluginType,
)

GALLERY_API = "https://nhentai.net/api/v2/galleries/{gid}"
SEARCH_API = "https://nhentai.net/api/v2/search?query={query}"
COMMENTS_API = "https://nhentai.net/api/gallery/{gid}/comments"

_URL_RE = re.compile(r"nhentai\.net/g/(\d+)", re.IGNORECASE)
_SOURCE_TAG_RE = re.compile(r"^source:\s*(?:https?://)?nhentai\.net/g/(\d+)", re.IGNORECASE)
_BRACED_ID_RE = re.compile(r"\{(\d+)\}")


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
    m = _BRACED_ID_RE.search(stem)
    return int(m.group(1)) if m else None


def tags_from_gallery(data: dict) -> list[str]:
    """nhentai tag objects → "namespace:value" strings (plain ``tag`` type stays un-namespaced)."""
    out: list[str] = []
    for tag in data.get("tags", []):
        namespace = tag.get("type", "")
        name = str(tag.get("name", "")).strip()
        if not name:
            continue
        out.append(name if namespace == "tag" else f"{namespace}:{name}")
    return out


def title_from_gallery(data: dict) -> str | None:
    titles = data.get("title") or {}
    return titles.get("pretty") or titles.get("english") or titles.get("japanese") or None


class NHentaiMetadataPlugin(MetadataPlugin):
    NAMESPACE = "nhentai_metadata"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="nHentai Metadata",
            type=PluginType.METADATA,
            description=(
                "Title and tags from the nhentai API — useful when the e-hentai gallery was "
                "taken down. Resolves the gallery from a nhentai URL, a source: tag, a {id} "
                "filename marker, or a title search."
            ),
            author="MangaCouch",
            cooldown=4.0,
            parameters=[
                PluginParam(
                    name="cf_clearance", type="password", secret=True,
                    description="Cloudflare cf_clearance cookie (only needed if nhentai "
                                "challenges this server's IP)",
                ),
                PluginParam(
                    name="title_search", type="bool", default=True,
                    description="Fall back to searching nhentai by title when no ID is found",
                ),
                PluginParam(
                    name="add_timestamp", type="bool", default=False,
                    description="Add a timestamp: tag with the gallery's upload date",
                ),
                PluginParam(
                    name="fetch_comments", type="bool", default=True,
                    description="Also fetch gallery comments",
                ),
            ],
        )

    def get_tags(self, ctx: MetadataContext) -> MetadataResult:
        session = ctx.session or httpx.Client(follow_redirects=True, timeout=30.0)
        owns_session = ctx.session is None
        comments: list[MetadataComment] = []
        try:
            self._apply_cf_cookie(session, ctx.config)
            gid = self._resolve_gallery_id(ctx, session)
            if gid is None:
                return MetadataResult(error="No matching nHentai gallery found.")
            data = self._fetch_gallery(session, gid)
            if _truthy(ctx.config.get("fetch_comments", True)):
                # Comments are best-effort — never fail the tag fetch over them.
                with contextlib.suppress(httpx.HTTPError, ValueError):
                    comments = self._fetch_comments(session, gid)
        except httpx.HTTPStatusError as exc:
            return MetadataResult(error=f"nhentai returned HTTP {exc.response.status_code}")
        except (httpx.HTTPError, ValueError) as exc:
            return MetadataResult(error=f"network error: {exc}")
        finally:
            if owns_session:
                session.close()

        tags = tags_from_gallery(data)
        if not tags:
            return MetadataResult(error="Gallery has no tags.")
        if _truthy(ctx.config.get("add_timestamp")) and data.get("upload_date"):
            tags.append(f"timestamp:{data['upload_date']}")
        tags.append(f"source:nhentai.net/g/{gid}")
        return MetadataResult(tags=tags, title=title_from_gallery(data), comments=comments)

    # -- internals ------------------------------------------------------------------------------

    @staticmethod
    def _apply_cf_cookie(session: httpx.Client, config: dict) -> None:
        clearance = config.get("cf_clearance", "")
        if clearance:
            session.cookies.set("cf_clearance", str(clearance), domain="nhentai.net", path="/")

    def _resolve_gallery_id(self, ctx: MetadataContext, session: httpx.Client) -> int | None:
        gid = gallery_id_from_url(ctx.source_url) or gallery_id_from_tags(ctx.existing_tags)
        if gid is None and ctx.file_path is not None:
            gid = gallery_id_from_filename(ctx.file_path.stem)
        if gid is None and ctx.title and _truthy(ctx.config.get("title_search", True)):
            gid = self._search_by_title(session, ctx.title)
        return gid

    @staticmethod
    def _search_by_title(session: httpx.Client, title: str) -> int | None:
        resp = session.get(SEARCH_API.format(query=quote(title)))
        resp.raise_for_status()
        results = resp.json().get("result", [])
        return int(results[0]["id"]) if results else None

    @staticmethod
    def _fetch_gallery(session: httpx.Client, gid: int) -> dict:
        resp = session.get(GALLERY_API.format(gid=gid))
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _fetch_comments(session: httpx.Client, gid: int) -> list[MetadataComment]:
        resp = session.get(COMMENTS_API.format(gid=gid))
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        out: list[MetadataComment] = []
        for row in data:
            if not isinstance(row, dict):
                continue
            body = str(row.get("body", "")).strip()
            if not body:
                continue
            poster = row.get("poster") or {}
            posted = row.get("post_date")
            out.append(
                MetadataComment(
                    username=str(poster.get("username", "")),
                    posted=int(posted) if isinstance(posted, (int, float)) else None,
                    content=body,
                )
            )
        return out


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
