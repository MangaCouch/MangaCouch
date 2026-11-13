"""A varied, invented sample library for documentation screenshots (`ui_browser_server.py --showcase`).

Every title, name and tag here is made up; the art is generated geometry.
"""

from __future__ import annotations

import random
import time
import zipfile
from pathlib import Path

import httpx
import pyvips
from helpers import save_metadata
from source_fixtures import SourceFixture

from mangacouch.collections import Collections
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState, Source, SourceRating, Tag
from mangacouch.translations import Translations

DAY = 86400
TRANSLATIONS = {
    "other": {
        "seaside": "海边",
        "slice of life": "日常",
        "school life": "校园",
        "summer": "夏天",
        "snow": "雪景",
        "stargazing": "观星",
        "full color": "全彩",
        "food": "美食",
        "travel": "旅行",
        "rain": "雨天",
        "cats": "猫",
        "trains": "列车",
        "fantasy": "奇幻",
        "music": "音乐",
    },
    "parody": {"lighthouse chronicle": "灯塔纪事", "starlit academy": "星光学院"},
    "group": {"studio pebble": "石子工作室", "night owl press": "夜枭社"},
}
# title | original title | language | category | artist | group | tags | pages | cover motif | source rating
BOOKS_TABLE = """
山间来信 | Letters from the Hills | chinese | Manga | lin qiao | - | slice of life, travel, trains | 24 | mountains | 4.7
星降る夜の約束 | A Promise Under Falling Stars | japanese | Manga | kawase tomo | night owl press | stargazing, fantasy, school life | 14 | stars | 4.8
The Quiet Observatory | - | english | Artist CG | ellie marsh | - | stargazing, full color | 10 | stars | 4.4
海边的慢日子 | Slow Days by the Sea | chinese | Doujinshi | su wan | studio pebble | seaside, summer, slice of life | 12 | waves | 4.6
雨音カフェ | Raindrop Café | japanese | Doujinshi | mizuno haru | - | rain, food, music | 12 | rain | 4.2
The Clockwork Garden | - | english | Western | ellie marsh | - | fantasy, full color | 14 | garden | 4.5
雨夜书店 | The Rainy Night Bookshop | chinese | Manga | lin qiao | - | rain, slice of life, cats | 16 | city | 4.3
春風ステーション | Spring Breeze Station | japanese | Manga | kawase tomo | - | trains, school life | 12 | mountains | 4.1
Paper Lanterns | - | english | Doujinshi | noah fenwick | night owl press | summer, music | 10 | garden | 3.9
纸飞机与夏天 | Paper Planes and Summer | chinese | Doujinshi | su wan | studio pebble | summer, school life, seaside | 12 | waves | 4.6
図書室の魔女 | The Library Witch | japanese | Manga | mizuno haru | - | fantasy, school life | 14 | stars | 4.7
Northbound Train | - | english | Manga | noah fenwick | - | trains, snow, travel | 12 | snow | 4.0
月台上的猫 | The Cat on the Platform | chinese | Artist CG | lin qiao | - | cats, trains, full color | 8 | city | 4.5
夕焼け商店街 | Sunset Shopping Street | japanese | Doujinshi | aoi senna | studio pebble | food, slice of life | 10 | city | 3.8
Salt & Honey | - | english | Doujinshi | ruth calder | - | food, seaside | 10 | waves | 4.2
灯塔守夜人 | The Lighthouse Keeper | chinese | Doujinshi | su wan | studio pebble | seaside, fantasy | 12 | waves | 4.9
硝子の金魚 | Glass Goldfish | japanese | Artist CG | aoi senna | - | summer, full color | 8 | garden | 4.4
Midnight Bakery | - | english | Western | ruth calder | - | food, cats | 12 | city | 4.1
青石巷物语 | Tales of Bluestone Lane | chinese | Manga | zhou yi | - | slice of life, rain | 14 | rain | 4.0
雪見だいふく日和 | A Day for Snow-Viewing Sweets | japanese | Manga | kawase tomo | - | snow, food | 10 | snow | 4.3
Tidepool Diaries | - | english | Artist CG | ellie marsh | - | seaside, full color, summer | 8 | waves | 4.6
风筝季节 | Kite Season | chinese | Doujinshi | zhou yi | night owl press | summer, travel | 10 | mountains | 3.7
月曜日の灯台 | Monday Lighthouse | japanese | Doujinshi | mizuno haru | night owl press | seaside, music | 12 | waves | 4.2
A Map of Small Rooms | - | english | Manga | noah fenwick | - | slice of life, cats | 12 | city | 4.4
旧城邮差 | The Old Town Postman | chinese | Manga | lin qiao | - | travel, snow | 14 | snow | 4.5
星河便利店 | Galaxy Convenience Store | chinese | Doujinshi | su wan | - | stargazing, food | 10 | stars | 4.0
Copper Skies | - | english | Western | ruth calder | - | fantasy, travel | 12 | mountains | 3.9
午后茶馆 | Afternoon Teahouse | chinese | Artist CG | zhou yi | - | food, full color | 8 | garden | 4.3
白鹭湾 | Egret Bay | chinese | Manga | lin qiao | - | seaside, travel | 12 | waves | 4.6
ひだまり手帖 | Sunny Spot Notebook | japanese | Manga | aoi senna | - | slice of life, cats, school life | 12 | garden | 4.5
The Last Snow Train | - | english | Doujinshi | noah fenwick | night owl press | snow, trains | 10 | snow | 4.2
晚风合唱团 | Evening Breeze Choir | chinese | Doujinshi | zhou yi | studio pebble | music, school life | 12 | mountains | 4.4
"""  # noqa: E501
BOOKS = [
    [None if cell == "-" else cell for cell in (part.strip() for part in line.split("|"))]
    for line in BOOKS_TABLE.strip().splitlines()
]
PARODY = {1: "starlit academy", 10: "starlit academy", 15: "lighthouse chronicle", 22: "lighthouse chronicle"}
PALETTES = [
    ("#e9d8c4", "#c98f6b", "#5f4b5c", "#f6efe3"),
    ("#cfe0e3", "#6f9aa8", "#2f4858", "#f3f6f4"),
    ("#e4dcef", "#9b88b8", "#4b3f66", "#f6f2fa"),
    ("#dfe8d5", "#8aa77b", "#3e5240", "#f4f7ef"),
    ("#f2dccf", "#d98e73", "#7a3f3a", "#fbf3ec"),
    ("#d6e1ef", "#7d98c4", "#2e3f63", "#f1f4fa"),
    ("#efe5c8", "#c9a656", "#5b4a2a", "#faf6ea"),
    ("#d9ece7", "#6fb3a2", "#285a52", "#f0f8f5"),
]


def el(tag: str, **attributes) -> str:
    """One SVG element; numbers are rounded and `stroke_width` becomes `stroke-width`."""
    values = " ".join(
        f'{name.replace("_", "-")}="{round(value) if isinstance(value, float) else value}"'
        for name, value in attributes.items()
    )
    return f"<{tag} {values}/>"


def motif_svg(motif: str, palette, rng: random.Random, w: int, h: int) -> str:
    light, mid, dark, paper = palette
    parts = [
        el("rect", width=w, height=h, fill=dark if motif == "stars" else mid if motif == "rain" else light)
    ]
    if motif == "mountains":
        parts += [
            el("circle", cx=w * 0.7, cy=h * 0.28, r=w * 0.16, fill=paper),
            el(
                "path",
                d=f"M0 {h * 0.72:.0f} L{w * 0.3:.0f} {h * 0.38:.0f} L{w * 0.62:.0f} {h * 0.75:.0f}Z",
                fill=mid,
            ),
            el(
                "path",
                d=f"M{w * 0.35:.0f} {h} L{w * 0.72:.0f} {h * 0.45:.0f} L{w} {h * 0.68:.0f} L{w} {h}Z",
                fill=dark,
            ),
            el("rect", y=h * 0.74, width=w, height=h * 0.26, fill=dark, opacity="0.8"),
        ]
    elif motif == "waves":
        parts.append(el("circle", cx=w * 0.3, cy=h * 0.3, r=w * 0.13, fill=paper))
        for row in range(5):
            y = h * (0.55 + row * 0.1)
            crest = " ".join(
                f"Q{x + w / 12:.0f} {y - 22:.0f} {x + w / 6:.0f} {y:.0f}" for x in range(0, w, w // 6)
            )
            parts.append(el("path", d=f"M0 {y:.0f} {crest} L{w} {h} L0 {h}Z", fill=[mid, dark][row % 2]))
    elif motif == "city":
        parts.append(el("circle", cx=w * 0.75, cy=h * 0.22, r=w * 0.1, fill=paper))
        x = 0.0
        while x < w:
            width, height = rng.uniform(0.1, 0.2) * w, rng.uniform(0.3, 0.6) * h
            parts.append(el("rect", x=x, y=h - height, width=width + 2, height=height, fill=dark))
            for wy in range(int(h - height) + 24, h - 30, 46):
                for wx in range(int(x) + 12, int(x + width) - 14, 28):
                    if rng.random() < 0.45:
                        parts.append(el("rect", x=wx, y=wy, width=12, height=18, fill=light))
            x += width
    elif motif == "stars":
        for _ in range(70):
            parts.append(
                el(
                    "circle",
                    cx=rng.uniform(0, w),
                    cy=rng.uniform(0, h * 0.7),
                    r=rng.uniform(1, 3.5),
                    fill=paper,
                )
            )
        parts += [
            el("circle", cx=w * 0.68, cy=h * 0.25, r=w * 0.12, fill=light),
            el("circle", cx=w * 0.74, cy=h * 0.22, r=w * 0.11, fill=dark),
            el(
                "path",
                d=f"M0 {h * 0.8:.0f} Q{w / 2:.0f} {h * 0.68:.0f} {w} {h * 0.8:.0f} L{w} {h} L0 {h}Z",
                fill=mid,
            ),
        ]
    elif motif == "rain":
        for _ in range(60):
            x, y = rng.uniform(0, w), rng.uniform(0, h)
            parts.append(el("line", x1=x, y1=y, x2=x - 8, y2=y + 34, stroke=light, stroke_width=2))
        parts += [
            el(
                "path",
                d=f"M{w * 0.25:.0f} {h * 0.55:.0f} Q{w / 2:.0f} {h * 0.32:.0f} "
                f"{w * 0.75:.0f} {h * 0.55:.0f}Z",
                fill=dark,
            ),
            el("rect", x=w / 2 - 3, y=h * 0.55, width=6, height=h * 0.22, fill=dark),
            el("rect", y=h * 0.82, width=w, height=h * 0.18, fill=dark, opacity="0.7"),
        ]
    elif motif == "snow":
        parts += [
            el(
                "path",
                d=f"M0 {h * 0.66:.0f} L{w * 0.4:.0f} {h * 0.36:.0f} L{w * 0.8:.0f} {h * 0.66:.0f}Z",
                fill=mid,
            ),
            el(
                "path",
                d=f"M{w * 0.28:.0f} {h * 0.45:.0f} L{w * 0.4:.0f} {h * 0.36:.0f} "
                f"L{w * 0.52:.0f} {h * 0.45:.0f}Z",
                fill=paper,
            ),
            el("rect", y=h * 0.66, width=w, height=h * 0.34, fill=paper),
            el("rect", y=h * 0.74, width=w, height=8, fill=dark),
        ]
        for _ in range(45):
            parts.append(
                el(
                    "circle",
                    cx=rng.uniform(0, w),
                    cy=rng.uniform(0, h * 0.66),
                    r=rng.uniform(2, 5),
                    fill=paper,
                )
            )
    else:  # garden
        parts.append(el("rect", y=h * 0.7, width=w, height=h * 0.3, fill=dark))
        for _ in range(16):
            x, y, r = rng.uniform(0, w), rng.uniform(h * 0.2, h * 0.8), rng.uniform(18, 60)
            parts.append(el("circle", cx=x, cy=y, r=r, fill=rng.choice([mid, paper]), opacity="0.9"))
            parts.append(el("circle", cx=x, cy=y, r=r / 3, fill=dark, opacity="0.6"))
    return "".join(parts)


MOTIFS = ["mountains", "waves", "city", "stars", "rain", "snow", "garden"]
LAYOUTS = [
    [(0, 0, 1, 0.38), (0, 0.38, 0.5, 0.31), (0.5, 0.38, 0.5, 0.31), (0, 0.69, 1, 0.31)],
    [(0, 0, 0.6, 0.5), (0.6, 0, 0.4, 0.5), (0, 0.5, 1, 0.5)],
    [(0, 0, 1, 0.3), (0, 0.3, 1, 0.35), (0, 0.65, 0.45, 0.35), (0.45, 0.65, 0.55, 0.35)],
]


def page_png(index: int, page: int, motif: str, palette) -> bytes:
    """The cover (page 0) is one scene; later pages are framed panels with a page number."""
    width, height = 600, 850
    rng = random.Random(index * 1000 + page)
    if page == 0:
        body = motif_svg(motif, palette, rng, width, height)
    else:
        margin, gap = 34, 12
        inner_w, inner_h = width - 2 * margin, height - 2 * margin - 26
        body = el("rect", width=width, height=height, fill="#ffffff")
        for number, (px, py, pw, ph) in enumerate(LAYOUTS[page % 3]):
            x, y = margin + px * inner_w + gap / 2, margin + py * inner_h + gap / 2
            w, h = round(pw * inner_w - gap), round(ph * inner_h - gap)
            scene = motif_svg(motif if number == 0 else rng.choice(MOTIFS), palette, rng, w, h)
            if rng.random() < 0.6:
                # A speech balloon with a few lines standing in for text.
                bx, by = rng.uniform(0.2, 0.7) * w, rng.uniform(0.25, 0.4) * h
                scene += el(
                    "ellipse", cx=bx, cy=by, rx=62, ry=40, fill="#ffffff", stroke="#1d1d1f", stroke_width=2
                )
                for line in range(3):
                    scene += el(
                        "rect",
                        x=bx - 38,
                        y=by - 17 + line * 13,
                        width=76 - line * 18,
                        height=5,
                        fill="#8a8a8f",
                    )
            body += f'<svg x="{x:.0f}" y="{y:.0f}" width="{w}" height="{h}">{scene}</svg>'
            body += el("rect", x=x, y=y, width=w, height=h, fill="none", stroke="#1d1d1f", stroke_width=3)
        body += (
            f'<text x="{width // 2}" y="{height - 22}" font-size="16" text-anchor="middle" '
            f'font-family="Helvetica, Arial, sans-serif" fill="#55555a">{page + 1}</text>'
        )
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">{body}</svg>'
    return pyvips.Image.svgload_buffer(svg.encode()).write_to_buffer(".png", compression=9)


class ShowcaseSource(SourceFixture):
    """Source responses with invented titles and generated covers per gallery."""

    TITLES = {101: "Lanterns Over the River", 200: "Harbor Lights"}
    COVER_HOSTS = {"covers.example"}

    def __call__(self, request: httpx.Request):
        if request.url.host in self.COVER_HOSTS:
            cover = page_png(100, 0, "waves", PALETTES[1])
            return httpx.Response(200, content=cover, headers={"content-type": "image/png"})
        response = super().__call__(request)
        if request.url.path == "/api.php":
            data = response.json()
            for entry in data["gmetadata"]:
                entry["title"] = self.TITLES.get(entry["gid"], entry.get("title"))
                entry["title_jpn"] = "港の灯り" if entry["gid"] == 200 else ""
            return httpx.Response(200, json=data)
        return response


def seed(directory: Path, library: Library, files: FileLibrary) -> ShowcaseSource:
    Translations(library).import_data(
        {
            "version": 7,
            "head": {"sha": "synthetic-showcase"},
            "data": [
                {"namespace": namespace, "data": {key: {"name": name} for key, name in values.items()}}
                for namespace, values in TRANSLATIONS.items()
            ],
        }
    )
    now = int(time.time())
    books = []
    for index, (title, original, language, category, artist, group, tags, pages, motif, score) in enumerate(
        BOOKS
    ):
        assert title and language and category and artist and tags and pages and motif and score
        tags, pages, score = tags.split(", "), int(pages), float(score)
        palette = PALETTES[index % len(PALETTES)]
        archive = directory / f"showcase-{index + 1:02}.cbz"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zipped:
            for page in range(pages):
                zipped.writestr(f"{page + 1:03}.png", page_png(index, page, motif, palette))
        book = files.import_file(archive).book
        metadata = Metadata(
            title=title,
            original_title=original,
            language=language,
            work_type=category,
            tags=[
                Tag(namespace="artist", value=artist),
                *([Tag(namespace="group", value=group)] if group else []),
                *([Tag(namespace="parody", value=PARODY[index])] if index in PARODY else []),
                *(Tag(namespace="other", value=tag) for tag in tags),
            ],
            source_rating=SourceRating(average=score, count=40 + index * 7),
            source=Source(site="ehentai", gallery_id=2000 + index, gallery_token="0a1b2c3d4e")
            if index % 3 != 2
            else None,
            newer_versions=[Source(site="ehentai", gallery_id=2900, gallery_token="5f6e7d8c9b")]
            if index == 0
            else [],
            uploader="sample uploader",
            uploaded_at=f"2026-0{1 + index % 9}-1{index % 10}T12:00:00+00:00",
        )
        books.append(save_metadata(library, book.id, metadata, expected_revision=book.metadata_revision))
    with library.transaction() as db:
        # Two books stay in the review queue; the rest look like a tidy, confirmed library.
        db.executemany(
            "UPDATE book SET review_state='confirmed' WHERE id=?", [(book.id,) for book in books[:-2]]
        )

    # Reading history over the last four weeks: (book, page reached, days ago, finished).
    # fmt: off
    history = [
        (11, 4, 27, False), (11, 11, 26, True), (5, 6, 24, False), (5, 13, 23, True),
        (16, 7, 21, True), (8, 3, 20, False), (19, 5, 18, False), (19, 9, 16, True),
        (3, 4, 15, False), (24, 8, 13, False), (24, 13, 12, True), (12, 7, 11, True),
        (6, 5, 9, False), (10, 6, 8, False), (2, 3, 7, False), (3, 9, 6, False), (27, 7, 5, True),
        (6, 10, 4, False), (10, 9, 3, False), (1, 5, 2, False), (4, 3, 2, False), (0, 4, 1, False),
        (1, 8, 1, False), (0, 6, 0, False), (2, 6, 0, False),
    ]
    # fmt: on
    revisions = {book.id: book.personal_revision for book in books}
    for order, (index, page, days_ago, finished) in enumerate(history):
        book = books[index]
        saved = library.save_personal(
            book.id,
            PersonalState(
                resume_page=min(page, book.content.page_count - 1),
                completed=finished,
                rating=None,
            ),
            expected_revision=revisions[book.id],
        )
        revisions[book.id] = saved.personal_revision
        when = now - days_ago * DAY - (len(history) - order) * 1800
        with library.transaction() as db:
            db.execute(
                "UPDATE reading_event SET created_at=? WHERE id=(SELECT max(id) FROM reading_event)", (when,)
            )
            db.execute("UPDATE reading_activity SET updated_at=? WHERE book_id=?", (when, book.id))
    for index, rating in {
        0: 5.0,
        1: 4.5,
        3: 5.0,
        5: 4.0,
        10: 4.5,
        11: 3.5,
        15: 5.0,
        24: 4.0,
        29: 4.5,
    }.items():
        book = library.get_book(books[index].id)
        library.save_personal(
            book.id,
            book.personal.model_copy(update={"rating": rating}),
            expected_revision=book.personal_revision,
        )

    collections = Collections(library)
    later = collections.create("稍后阅读")
    for book in (books[6], books[13], books[20]):
        later = collections.edit_item(later["id"], book.id, later["revision"])
    collections.create("中文漫画", {"query": "", "options": {"language": "Chinese"}})
    collections.create("海边的故事", {"query": "other:seaside", "options": {}})

    remote = ShowcaseSource((directory / "showcase-31.cbz").read_bytes())
    remote.title = "Harbor Lights"
    remote.tags = ["language:english", "artist:ruth calder", "other:seaside"]
    return remote
