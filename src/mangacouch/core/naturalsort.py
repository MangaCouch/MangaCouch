"""Natural, unicode-aware page ordering (§5.7).

Zero-padding mis-sorts ≥5-digit page numbers, so we use ``natsort`` (PATH + locale) instead. A
cover-first / credits-last heuristic nudges obvious front/back matter into place; the keyword lists
are configuration, not hard-coded.

ASCII keywords are matched against whole *tokens* of the file stem (split on non-alphanumerics) —
substring matching silently corrupted real page names ("legend" ⊃ "end", "backstage" ⊃ "back",
"10000" ⊃ "0000"). CJK keywords have no token boundaries and stay substring-matched.
"""

from __future__ import annotations

import re

from natsort import natsort_keygen, ns

# Front/back matter keyword hints (matched case-insensitively against the extension-less stem).
DEFAULT_COVER_KEYWORDS = ("cover", "front", "frontcover", "封面", "表紙")
DEFAULT_CREDITS_KEYWORDS = ("credit", "credits", "back", "backcover", "end", "裏表紙", "奥付",
                            "thanks")

_natkey = natsort_keygen(alg=ns.PATH | ns.IGNORECASE | ns.LOCALE)
_TOKEN_SPLIT = re.compile(r"[^0-9a-z]+")


def _bucket(name: str, cover_kw: tuple[str, ...], credits_kw: tuple[str, ...]) -> int:
    """0 = cover (first), 1 = body, 2 = credits (last)."""
    base = name.rsplit("/", 1)[-1].lower()
    stem = base.rsplit(".", 1)[0] if "." in base else base
    tokens = set(_TOKEN_SPLIT.split(stem)) - {""}

    def hit(keyword: str) -> bool:
        return keyword in tokens if keyword.isascii() else keyword in stem

    # Credits first so "back_cover" lands at the back, not the front.
    if any(hit(k) for k in credits_kw):
        return 2
    if any(hit(k) for k in cover_kw):
        return 0
    return 1


def natural_page_sort(
    names: list[str],
    *,
    cover_keywords: tuple[str, ...] = DEFAULT_COVER_KEYWORDS,
    credits_keywords: tuple[str, ...] = DEFAULT_CREDITS_KEYWORDS,
) -> list[str]:
    """Return ``names`` ordered cover → natural body → credits."""
    return sorted(
        names,
        key=lambda n: (_bucket(n, cover_keywords, credits_keywords), _natkey(n)),
    )
