"""EHentai Login plugin (§5.4) — cookies on both domains; the session is cached, not rebuilt."""

from __future__ import annotations

import httpx

from ...acquisition.client import build_client, build_cookies
from ..base import LoginContext, LoginPlugin, PluginInfo, PluginParam, PluginType


class EHentaiLoginPlugin(LoginPlugin):
    NAMESPACE = "ehentai_login"

    def plugin_info(self) -> PluginInfo:
        return PluginInfo(
            namespace=self.NAMESPACE,
            name="EHentai Login",
            type=PluginType.LOGIN,
            description="Authenticated e-hentai / exhentai session from your cookies.",
            author="MangaCouch",
            parameters=[
                PluginParam(name="ipb_member_id", type="string", description="Cookie: ipb_member_id"),
                PluginParam(
                    name="ipb_pass_hash", type="password", secret=True,
                    description="Cookie: ipb_pass_hash",
                ),
                PluginParam(
                    name="igneous", type="password", secret=True,
                    description="Cookie: igneous (required for exhentai)",
                ),
            ],
        )

    def do_login(self, ctx: LoginContext) -> httpx.Client:
        cookies = build_cookies(
            {
                "ipb_member_id": ctx.config.get("ipb_member_id", ""),
                "ipb_pass_hash": ctx.config.get("ipb_pass_hash", ""),
                "igneous": ctx.config.get("igneous", ""),
                "nw": "1",
            }
        )
        return build_client(
            proxy=ctx.proxy or None,
            proxy_scope=ctx.config.get("proxy_scope", "all"),
            user_agent=ctx.user_agent,
            cookies=cookies,
        )
