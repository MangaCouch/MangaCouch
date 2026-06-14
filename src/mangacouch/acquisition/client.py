"""The persistent HTTP client for acquisition (§5.3).

One client carries the cookie jar, a realistic User-Agent, and follows redirects. A single optional
proxy applies to **all** acquisition traffic by default — ``e-hentai.org``, ``exhentai.org`` and the
H@H download host (a third host that must be reachable). Use ``socks5h://`` so DNS resolves at the
proxy. An optional ``exhentai-only`` scope narrows it.
"""

from __future__ import annotations

import httpx

# The login + access cookies, set on BOTH domains (Appendix A).
COOKIE_KEYS = ("ipb_member_id", "ipb_pass_hash", "igneous", "nw", "star", "ipb_coppa")
_EH_DOMAINS = ("e-hentai.org", "exhentai.org")


def build_cookies(values: dict[str, str]) -> httpx.Cookies:
    """Build a cookie jar with the e-hentai cookies set on both domains; ``nw=1`` defaulted."""
    jar = httpx.Cookies()
    merged = dict(values)
    merged.setdefault("nw", "1")  # skip the offensive-content interstitial
    for domain in _EH_DOMAINS:
        for key in COOKIE_KEYS:
            if merged.get(key):
                jar.set(key, str(merged[key]), domain=domain, path="/")
    return jar


def build_client(
    *,
    proxy: str | None = None,
    proxy_scope: str = "all",
    user_agent: str,
    cookies: httpx.Cookies | None = None,
    timeout: float = 30.0,
) -> httpx.Client:
    """Construct the persistent acquisition client.

    ``proxy_scope='exhentai-only'`` routes only ``exhentai.org`` through the proxy (rarely the right
    default — where e-hentai is blocked it needs the proxy too, hence the recommended ``all``).
    """
    headers = {"User-Agent": user_agent, "Accept-Language": "en-US,en;q=0.9"}
    common = {
        "headers": headers,
        "cookies": cookies,
        "follow_redirects": True,
        "timeout": httpx.Timeout(timeout, read=120.0),
    }
    if proxy and proxy_scope == "exhentai-only":
        mounts = {
            "all://exhentai.org": httpx.HTTPTransport(proxy=proxy),
            "all://*.exhentai.org": httpx.HTTPTransport(proxy=proxy),
            "all://": httpx.HTTPTransport(),
        }
        return httpx.Client(mounts=mounts, **common)
    if proxy:
        return httpx.Client(proxy=proxy, **common)
    return httpx.Client(**common)
