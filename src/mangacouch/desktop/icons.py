"""The MangaCouch logo (web/public/assets/icon.svg) drawn with Pillow for the tray and app icons."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from PIL.Image import Image

# Panels from icon.svg, in its 24..232 view box, clipped by a rounded window of radius 37.
WINDOW = (42, 38, 214, 218)
RADIUS = 37
PANELS = (
    ([(42, 38), (128, 38), (105, 139), (42, 139)], (0xDD, 0x76, 0x5C)),
    ([(142, 38), (214, 38), (214, 115), (121, 137)], (0xE8, 0xB7, 0x60)),
    ([(42, 154), (214, 130), (214, 218), (42, 218)], (0x71, 0x8C, 0x76)),
)
VIEW = (24, 232)
DOTS = {"failed": (0xD9, 0x3B, 0x3B), "starting": (0xE8, 0xB7, 0x60)}


def logo(size: int, *, monochrome: bool = False, padding: float = 0.0) -> Image:
    """The logo at `size` pixels; `monochrome` is black for a macOS template image."""
    from PIL import Image, ImageDraw

    scale = 4
    canvas = size * scale
    inset = padding * canvas
    factor = (canvas - 2 * inset) / (VIEW[1] - VIEW[0])

    def point(x: float, y: float) -> tuple[float, float]:
        return (inset + (x - VIEW[0]) * factor, inset + (y - VIEW[0]) * factor)

    art = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    draw = ImageDraw.Draw(art)
    for polygon, color in PANELS:
        draw.polygon([point(x, y) for x, y in polygon], fill=(0, 0, 0, 255) if monochrome else (*color, 255))
    mask = Image.new("L", (canvas, canvas), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (*point(WINDOW[0], WINDOW[1]), *point(WINDOW[2], WINDOW[3])), radius=RADIUS * factor, fill=255
    )
    clipped = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    clipped.paste(art, (0, 0), mask)
    return clipped.resize((size, size), Image.Resampling.LANCZOS)


def tray_icon(
    state: Literal["running", "starting", "stopped", "failed"], *, template: bool, size: int = 64
) -> Image:
    """Running shows the plain logo; other states are dimmed, with a coloured dot in full colour."""
    from PIL import ImageDraw

    image = logo(size, monochrome=template, padding=0.02 if template else 0.0)
    if state != "running":
        alpha = image.getchannel("A").point([value * 45 // 100 for value in range(256)])
        if not template:
            image = image.convert("LA").convert("RGBA")
        image.putalpha(alpha)
    if not template and state in DOTS:
        radius = size * 0.2
        center = size - radius - 1
        ImageDraw.Draw(image).ellipse(
            (center - radius, center - radius, center + radius, center + radius),
            fill=(*DOTS[state], 255),
            outline=(255, 255, 255, 255),
            width=max(1, size // 32),
        )
    return image
