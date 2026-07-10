"""Generate a mock library — many small .cbz archives with sidecar metadata — for UI testing
(infinite scroll, search, badges, progress bars, Chinese tag search, …).

Deterministic for a given seed, so repeated runs produce the same titles and skip existing files.
"""

# pyvips has no type stubs (see core/imaging.py) — suppress its dynamic-call noise file-locally.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false

from __future__ import annotations

import random
import zipfile
from pathlib import Path

from ..core import sidecars

_ADJECTIVES = [
    "Midnight", "Scarlet", "Silent", "Eternal", "Wandering", "Golden", "Hidden", "Lucky",
    "Rainy", "Neon", "Gentle", "Secret", "Summer", "Winter", "Lonely", "Radiant",
]
_NOUNS = [
    "Garden", "Voyage", "Melody", "Classroom", "Cafe", "Library", "Festival", "Constellation",
    "Aquarium", "Journey", "Letter", "Promise", "Season", "Holiday", "Memory", "Story",
]
_CJK_TITLES = [
    "星空下的约定", "夏日祭典", "雨后的图书馆", "深夜咖啡店", "旅行日记", "青春物语",
    "海边的回忆", "樱花之约", "月光奏鸣曲", "秘密花园",
]
_ARTISTS = ["yamada", "suzuki", "tanaka", "sato", "kobayashi", "watanabe", "ito", "kimura"]
_GROUPS = ["circle alpha", "studio beta", "team gamma"]
_PARODIES = ["original", "touhou project", "fate grand order", "genshin impact", "vocaloid"]
_LANGUAGES = ["english", "japanese", "chinese", "korean"]
_FEMALE_TAGS = ["ponytail", "twintails", "glasses", "long hair", "short hair", "kimono"]
_MALE_TAGS = ["glasses", "short hair"]
_OTHER_TAGS = ["full color", "story arc", "multi-work series", "artbook"]
_CATEGORIES = ["Doujinshi", "Manga", "Artist CG", "Non-H", "Western"]

_PALETTE = [
    (233, 84, 84), (84, 160, 233), (108, 203, 130), (240, 180, 90), (170, 120, 220),
    (90, 200, 200), (230, 140, 180), (150, 150, 150), (250, 210, 120), (120, 140, 230),
]


def _page_bytes(
    color: tuple[int, int, int], index: int, salt: int, width: int = 400, height: int = 600
) -> bytes:
    """A small solid-colour JPEG page (pyvips only, matching the app's image stack). A thin
    darker band whose position encodes the page index makes pages visually distinguishable;
    ``salt`` (the archive number) keeps every archive's bytes unique so the content-hash
    dedup never collapses two mock archives into one."""
    import pyvips

    base = pyvips.Image.black(width, height)
    img = base.new_from_image(list(color)).copy(interpretation="srgb").cast("uchar")
    band_h = 40
    y = ((index + salt) * 53) % max(1, height - band_h)
    dark = tuple(max(0, c - 90 - (salt % 31)) for c in color)
    stripe = pyvips.Image.black(width, band_h).new_from_image(list(dark)).cast("uchar")
    img = img.insert(stripe, 0, y)
    return img.write_to_buffer(".jpg", Q=70)


def _make_cbz(path: Path, page_count: int, salt: int, rng: random.Random) -> None:
    color = rng.choice(_PALETTE)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for i in range(page_count):
            zf.writestr(f"{i + 1:03d}.jpg", _page_bytes(color, i, salt))


def _mock_title(rng: random.Random, index: int) -> str:
    if rng.random() < 0.25:
        return f"{rng.choice(_CJK_TITLES)} 第{index + 1}话"
    return f"{rng.choice(_ADJECTIVES)} {rng.choice(_NOUNS)} Vol.{index + 1}"


def _mock_tags(rng: random.Random) -> list[str]:
    tags = [
        f"artist:{rng.choice(_ARTISTS)}",
        f"language:{rng.choice(_LANGUAGES)}",
        f"parody:{rng.choice(_PARODIES)}",
    ]
    if rng.random() < 0.5:
        tags.append(f"group:{rng.choice(_GROUPS)}")
    tags += [f"female:{t}" for t in rng.sample(_FEMALE_TAGS, k=rng.randint(1, 3))]
    if rng.random() < 0.3:
        tags.append(f"male:{rng.choice(_MALE_TAGS)}")
    tags += [f"other:{t}" for t in rng.sample(_OTHER_TAGS, k=rng.randint(0, 2))]
    return tags


def generate_mock_library(manga_root: Path, *, count: int = 100, seed: int = 42) -> int:
    """Create ``count`` mock .cbz files (with .mc.json sidecars) under ``manga_root/mock/``.

    Roughly a third get a saved reading position so read badges / progress bars / "new only"
    filtering are all exercised. Returns the number of archives created (existing ones are kept).
    """
    rng = random.Random(seed)
    dest = manga_root / "mock"
    dest.mkdir(parents=True, exist_ok=True)
    created = 0
    for i in range(count):
        page_count = rng.randint(6, 32)
        title = _mock_title(rng, i)
        path = dest / f"mock-{i + 1:03d}.cbz"
        # Consume the same random draws whether or not the file exists, for stable output.
        tags = _mock_tags(rng)
        rating = round(rng.uniform(2.0, 5.0), 2) if rng.random() < 0.8 else None
        progress_roll = rng.random()
        if path.exists():
            continue
        _make_cbz(path, page_count, i, rng)
        progress = 0
        if progress_roll < 0.2:
            progress = page_count  # finished → read badge
        elif progress_roll < 0.4:
            progress = rng.randint(1, max(1, page_count - 2))  # partial → progress bar
        sidecars.write_mc(
            path,
            sidecars.McSidecar(
                archive_id="",
                fingerprint=None,
                format="cbz",
                page_count=page_count,
                original_filename=path.name,
                title=title,
                summary=f"Mock archive #{i + 1} for UI testing.",
                rating=rating,
                language=next((t.split(":", 1)[1] for t in tags if t.startswith("language:")), None),
                category=rng.choice(_CATEGORIES),
                tags=tags,
                progress_page=progress,
                ingest={"via": "mock"},
            ),
        )
        created += 1
    return created
