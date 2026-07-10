"""EHentai Metadata plugin (§5.4) — tags/title/rating from the gallery JSON API (Appendix A),
plus gallery comments scraped from the gallery page HTML (the API does not expose them)."""

from __future__ import annotations

import html as html_lib
import logging
import re
from datetime import UTC, datetime

import httpx

from ...acquisition import ehentai
from ..base import (
    MetadataComment,
    MetadataContext,
    MetadataPlugin,
    MetadataResult,
    PluginInfo,
    PluginParam,
    PluginType,
)

API_URL = "https://api.e-hentai.org/api.php"

log = logging.getLogger("mangacouch.plugins.ehentai")


def fetch_gdata(session: httpx.Client, gid: int, token: str) -> dict:
    """Call the ``gdata`` API for one gallery (up to 25 pairs/call; be polite between calls)."""
    payload = {"method": "gdata", "gidlist": [[gid, token]], "namespace": 1}
    resp = session.post(API_URL, json=payload, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    entries = data.get("gmetadata", [])
    if not entries:
        raise ehentai.EHentaiError("gdata returned no metadata for this gallery.")
    return entries[0]


def gdata_to_tags(entry: dict) -> list[str]:
    return [str(t) for t in entry.get("tags", [])]


def gdata_rating(entry: dict) -> float | None:
    try:
        value = float(str(entry.get("rating", "")))
    except (TypeError, ValueError):
        return None
    return value if 0.0 <= value <= 5.0 else None


# -- gallery-page comment scraping ---------------------------------------------------------------

_COMMENT_BLOCK_RE = re.compile(r'<div class="c1">(.*?)(?=<div class="c1">|<div id="chd")', re.S)
_C3_RE = re.compile(r'<div class="c3">Posted\s+on\s+([^<]+?)\s+by:.*?<a[^>]*>([^<]*)</a>', re.S)
_C6_RE = re.compile(r'<div class="c6"[^>]*>(.*?)</div>', re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"<br\s*/?>", re.I)


def _parse_comment_date(text: str) -> int | None:
    text = text.strip().rstrip(",")
    try:
        return int(datetime.strptime(text, "%d %B %Y, %H:%M").replace(tzinfo=UTC).timestamp())
    except ValueError:
        return None


def parse_gallery_comments(html: str) -> list[MetadataComment]:
    """Best-effort scrape of the ``#cdiv`` comment blocks on a gallery page."""
    out: list[MetadataComment] = []
    cdiv = html.split('id="cdiv"', 1)
    if len(cdiv) < 2:
        return out
    for block in _COMMENT_BLOCK_RE.findall(cdiv[1]):
        header = _C3_RE.search(block)
        body = _C6_RE.search(block)
        if not header or not body:
            continue
        content = _BR_RE.sub("\n", body.group(1))
        content = html_lib.unescape(_TAG_RE.sub("", content)).strip()
        if not content:
            continue
        out.append(
            MetadataComment(
                username=html_lib.unescape(header.group(2)).strip(),
                posted=_parse_comment_date(html_lib.unescape(header.group(1))),
                content=content,
            )
        )
    return out


def fetch_gallery_comments(session: httpx.Client, ref: ehentai.GalleryRef) -> list[MetadataComment]:
    resp = session.get(ref.gallery_url, params={"hc": 1}, timeout=30.0)
    resp.raise_for_status()
    return parse_gallery_comments(resp.text)


class EHentaiMetadataPlugin(MetadataPlugin):
    NAMESPACE = "ehentai_metadata"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="EHentai Metadata",
            type=PluginType.METADATA,
            description=(
                "Title, namespaced tags and rating from the e-hentai gallery JSON API; "
                "gallery comments from the gallery page."
            ),
            author="MangaCouch",
            cooldown=5.0,
            login_from="ehentai_login",
            parameters=[
                PluginParam(
                    name="fetch_comments", type="bool", default=True,
                    description="Also fetch gallery comments from the gallery page",
                ),
            ],
        )

    def get_tags(self, ctx: MetadataContext) -> MetadataResult:
        if not ctx.source_url or ctx.session is None:
            return MetadataResult(error="No source URL or session available.")
        try:
            ref = ehentai.parse_gallery_url(ctx.source_url)
            entry = fetch_gdata(ctx.session, ref.gid, ref.token)
        except ehentai.EHentaiError as exc:
            return MetadataResult(error=str(exc))
        except httpx.HTTPError as exc:
            return MetadataResult(error=f"network error: {exc}")

        comments: list[MetadataComment] = []
        if _truthy(ctx.config.get("fetch_comments", True)):
            try:
                comments = fetch_gallery_comments(ctx.session, ref)
            except (httpx.HTTPError, ehentai.EHentaiError):
                log.warning("could not fetch gallery comments for %s", ctx.source_url)

        title = html_lib.unescape(entry.get("title", "") or "")
        title_jpn = html_lib.unescape(entry.get("title_jpn", "") or "")
        summary = ""
        if entry.get("category"):
            summary = f"Category: {entry['category']}"
        return MetadataResult(
            tags=gdata_to_tags(entry),
            title=title or title_jpn or None,
            summary=summary,
            rating=gdata_rating(entry),
            comments=comments,
        )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)
