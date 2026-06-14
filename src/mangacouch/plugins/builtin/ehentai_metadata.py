"""EHentai Metadata plugin (§5.4) — tags/title from the gallery JSON API (Appendix A)."""

from __future__ import annotations

import html as html_lib

import httpx

from ...acquisition import ehentai
from ..base import (
    MetadataContext,
    MetadataPlugin,
    MetadataResult,
    PluginInfo,
    PluginType,
)

API_URL = "https://api.e-hentai.org/api.php"


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


class EHentaiMetadataPlugin(MetadataPlugin):
    NAMESPACE = "ehentai_metadata"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="EHentai Metadata",
            type=PluginType.METADATA,
            description="Title and namespaced tags from the e-hentai gallery JSON API.",
            author="MangaCouch",
            cooldown=5.0,
            login_from="ehentai_login",
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

        title = html_lib.unescape(entry.get("title", "") or "")
        title_jpn = html_lib.unescape(entry.get("title_jpn", "") or "")
        summary = ""
        if entry.get("category"):
            summary = f"Category: {entry['category']}"
        return MetadataResult(
            tags=gdata_to_tags(entry),
            title=title or title_jpn or None,
            summary=summary,
        )
