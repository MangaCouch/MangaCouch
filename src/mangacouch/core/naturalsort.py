"""Natural, unicode-aware page ordering (§5.7).

Zero-padding mis-sorts ≥5-digit page numbers, so we use ``natsort`` (PATH + locale) instead. A
cover-first / credits-last heuristic nudges obvious front/back matter into place; the keyword lists
are configuration, not hard-coded, and are matched case-insensitively against the file stem.
"""

from __future__ import annotations

from natsort import natsort_keygen, ns

# Front/back matter keyword hints — configurable, unicode-aware (matched against the lowered stem).
DEFAULT_COVER_KEYWORDS = ("cover", "front", "封面", "表紙", "00000", "0000")
DEFAULT_CREDITS_KEYWORDS = ("credit", "credits", "back", "end", "裏表紙", "奥付", "thanks")

_natkey = natsort_keygen(alg=ns.PATH | ns.IGNORECASE | ns.LOCALE)


def _bucket(name: str, cover_kw: tuple[str, ...], credits_kw: tuple[str, ...]) -> int:
    """0 = cover (first), 1 = body, 2 = credits (last)."""
    stem = name.rsplit("/", 1)[-1].lower()
    if any(k in stem for k in cover_kw):
        return 0
    if any(k in stem for k in credits_kw):
        return 2
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
