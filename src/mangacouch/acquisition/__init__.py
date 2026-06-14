"""Acquisition layer — e(x)hentai Archive Download (§5.3, Appendix A)."""

from __future__ import annotations

from .client import build_client, build_cookies
from .ehentai import (
    ArchiverPage,
    EHentaiError,
    GalleryRef,
    InsufficientFundsError,
    NotLoggedInError,
    download_hath_zip,
    fetch_archiver_page,
    parse_gallery_url,
    request_archive,
)
from .ratelimit import RateLimiter

__all__ = [
    "ArchiverPage",
    "EHentaiError",
    "GalleryRef",
    "InsufficientFundsError",
    "NotLoggedInError",
    "RateLimiter",
    "build_client",
    "build_cookies",
    "download_hath_zip",
    "fetch_archiver_page",
    "parse_gallery_url",
    "request_archive",
]
