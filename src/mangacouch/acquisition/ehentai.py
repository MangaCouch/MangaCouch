"""The e(x)hentai Archive Download protocol (Appendix A) — the v1 acquisition path.

Flow: parse the gallery URL → ``GET archiver.php`` (validate + read GP cost/balance) →
``POST archiver.php`` (``dltype=org``/``res``) → parse ``document.location`` → the H@H archive URL
→ ``GET <url>?start=1`` to stream the ZIP, retrying with backoff while H@H prepares it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

_URL_RE = re.compile(r"https?://(?P<host>e[-x]hentai\.org)/g/(?P<gid>\d+)/(?P<token>[0-9a-f]+)")
_REDIRECT_RE = re.compile(r"""document\.location\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HATH_HREF_RE = re.compile(r'href=["\']([^"\']*?/archive/[^"\']+)["\']', re.IGNORECASE)
_GP_RE = re.compile(r"([\d,]+)\s*GP", re.IGNORECASE)
_CREDITS_RE = re.compile(r"([\d,]+)\s*Credits", re.IGNORECASE)


class EHentaiError(Exception):
    """A protocol-level failure with a user-facing message."""


class NotLoggedInError(EHentaiError):
    pass


class InsufficientFundsError(EHentaiError):
    pass


@dataclass(slots=True)
class GalleryRef:
    domain: str  # "e-hentai" | "exhentai"
    gid: int
    token: str

    @property
    def host(self) -> str:
        return "exhentai.org" if self.domain == "exhentai" else "e-hentai.org"

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def gallery_url(self) -> str:
        return f"{self.base_url}/g/{self.gid}/{self.token}/"

    @property
    def archiver_url(self) -> str:
        return f"{self.base_url}/archiver.php?gid={self.gid}&token={self.token}"


@dataclass(slots=True)
class ArchiverPage:
    current_gp: int | None
    credits: int | None
    original_cost: int | None
    resample_cost: int | None
    raw_html: str = ""


def parse_gallery_url(url: str) -> GalleryRef:
    m = _URL_RE.search(url.strip())
    if not m:
        raise EHentaiError(f"not an e(x)hentai gallery URL: {url!r}")
    host = m.group("host")
    domain = "exhentai" if host.startswith("exhentai") else "e-hentai"
    return GalleryRef(domain=domain, gid=int(m.group("gid")), token=m.group("token"))


def _to_int(text: str | None) -> int | None:
    if not text:
        return None
    try:
        return int(text.replace(",", ""))
    except ValueError:
        return None


def parse_archiver_gp(html: str) -> ArchiverPage:
    """Best-effort parse of the GP cost (Original/Resample) and the account's current balance.

    Exact pricing is account/policy-dependent (Appendix A), so we read live values and degrade to
    ``None`` for anything we can't find rather than guessing.
    """
    current_gp = _to_int(m.group(1)) if (m := _GP_RE.search(html)) else None
    credits = _to_int(m.group(1)) if (m := _CREDITS_RE.search(html)) else None

    def cost_after(keyword: str) -> int | None:
        idx = html.lower().find(keyword.lower())
        if idx < 0:
            return None
        window = html[idx : idx + 400]
        m = _GP_RE.search(window)
        return _to_int(m.group(1)) if m else None

    original_cost = cost_after("Download Original Archive") or cost_after("Original Archive")
    resample_cost = cost_after("Download Resample Archive") or cost_after("Resample Archive")
    return ArchiverPage(
        current_gp=current_gp,
        credits=credits,
        original_cost=original_cost,
        resample_cost=resample_cost,
        raw_html=html,
    )


def fetch_archiver_page(session: httpx.Client, ref: GalleryRef) -> ArchiverPage:
    """``GET archiver.php`` — validate the gallery/login and read GP cost + balance."""
    resp = session.get(ref.archiver_url)
    resp.raise_for_status()
    html = resp.text
    if "Invalid archiver key" in html:
        raise EHentaiError("Invalid archiver key (bad gid/token).")
    if "This page requires you to log on" in html or "requires you to log on" in html:
        raise NotLoggedInError("Not logged in — set your e-hentai cookies in the Login plugin.")
    return parse_archiver_gp(html)


def request_archive(session: httpx.Client, ref: GalleryRef, dltype: str = "org") -> str:
    """``POST archiver.php`` and return the H@H archive URL from the JS redirect."""
    if dltype == "res":
        form = {"dltype": "res", "dlcheck": "Download Resample Archive"}
    else:
        form = {"dltype": "org", "dlcheck": "Download Original Archive"}
    resp = session.post(ref.archiver_url, data=form)
    resp.raise_for_status()
    html = resp.text
    if "Insufficient funds" in html:
        raise InsufficientFundsError("Insufficient funds (out of GP).")

    m = _REDIRECT_RE.search(html)
    if m:
        url = m.group(1).strip()
    else:
        href = _HATH_HREF_RE.search(html)
        if not href:
            raise EHentaiError("Could not find the H@H archive URL in the archiver response.")
        url = href.group(1).strip()
    if url.startswith("//"):
        url = "https:" + url
    return url


def download_hath_zip(
    session: httpx.Client,
    hath_url: str,
    dest: Path,
    *,
    on_progress: Callable[[float], None] | None = None,
    max_retries: int = 8,
    base_backoff: float = 3.0,
) -> Path:
    """Stream the prepared ZIP from H@H, retrying while it is still being prepared.

    ``?start=1`` is the programmatic equivalent of the "Click Here To Start Downloading" button.
    Large archives are prepared asynchronously, so the first response may be a "being prepared"
    page rather than the ZIP body — we retry with exponential backoff.
    """
    sep = "&" if "?" in hath_url else "?"
    start_url = f"{hath_url}{sep}start=1"
    dest.parent.mkdir(parents=True, exist_ok=True)

    for attempt in range(max_retries):
        with session.stream("GET", start_url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "").lower()
            is_zip = (
                "zip" in ctype
                or "octet-stream" in ctype
                or "application/x-download" in ctype
                or "content-disposition" in resp.headers
            )
            if not is_zip:
                body = resp.read()
                text = body.decode("utf-8", "ignore").lower()
                if "being prepared" in text or "try again" in text or "moment" in text:
                    time.sleep(base_backoff * (1.6**attempt))
                    continue
                # Unknown non-zip body: treat short HTML as transient, otherwise fail.
                if len(body) < 4096:
                    time.sleep(base_backoff * (1.6**attempt))
                    continue
                raise EHentaiError("H@H returned an unexpected non-archive response.")

            total = int(resp.headers.get("content-length", 0) or 0)
            written = 0
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    fh.write(chunk)
                    written += len(chunk)
                    if on_progress and total:
                        on_progress(min(1.0, written / total))
            if written == 0:
                tmp.unlink(missing_ok=True)
                raise EHentaiError("H@H returned an empty archive.")
            tmp.replace(dest)
            if on_progress:
                on_progress(1.0)
            return dest

    raise EHentaiError("H@H archive was not ready after several retries (still preparing).")
