"""EHentai Download plugin (§5.4) — drives ``archiver.php`` to fetch a whole gallery as one ZIP."""

from __future__ import annotations

import re

from ...acquisition import ehentai
from ..base import (
    DownloadContext,
    DownloadPlugin,
    DownloadResult,
    PluginInfo,
    PluginType,
)

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = _SAFE.sub("_", name).strip().strip(".")
    cleaned = cleaned[:180]
    return cleaned or fallback


class EHentaiDownloadPlugin(DownloadPlugin):
    NAMESPACE = "ehentai_download"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="EHentai Archive Download",
            type=PluginType.DOWNLOAD,
            description="Fetch an e(x)hentai gallery as a single ZIP via Archive Download.",
            author="MangaCouch",
            cooldown=5.0,
            login_from="ehentai_login",
            url_regex=r"https?://e[-x]hentai\.org/g/\d+/[0-9a-f]+",
        )

    def matches(self, url: str) -> bool:
        try:
            ehentai.parse_gallery_url(url)
            return True
        except ehentai.EHentaiError:
            return False

    def download(self, ctx: DownloadContext) -> DownloadResult:
        ref = ehentai.parse_gallery_url(ctx.url)
        page = ehentai.fetch_archiver_page(ctx.session, ref)  # validates login; reads GP

        cost = page.original_cost if ctx.dltype == "org" else page.resample_cost
        # The rate limiter (server-side) has already gated us by the time we reach here (§5.3).
        hath_url = ehentai.request_archive(ctx.session, ref, dltype=ctx.dltype)

        filename = _safe_filename(f"{ref.gid}_{ref.token}", f"{ref.gid}") + ".zip"
        dest = ctx.dest_dir / filename
        ehentai.download_hath_zip(ctx.session, hath_url, dest, on_progress=ctx.on_progress)

        return DownloadResult(
            archive_path=dest,
            suggested_filename=filename,
            gp_cost=cost,
            gp_balance=page.current_gp,
            gallery_meta={
                "gid": ref.gid,
                "token": ref.token,
                "domain": ref.domain,
                "source_url": ref.gallery_url,
            },
        )
