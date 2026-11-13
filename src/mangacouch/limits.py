"""Resource limits and cancellation of archive work, kept apart from the image libraries so that
light commands (the container health check) can use them without loading libvips or PDFium."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


class ImportCancelled(ValueError):
    pass


@dataclass(frozen=True)
class Limits:
    max_archive_bytes: int | None = None
    max_page_bytes: int = 128 * 1024**2
    max_unpacked_bytes: int = 32 * 1024**3
    # Pillow's decompression-bomb error threshold (2 * Image.MAX_IMAGE_PIXELS), kept as a literal so
    # this module never imports an image library.
    max_pixels: int = 178_956_970
    max_dimension: int = 32_768
    max_render_pixels: int = 16_000_000
    max_pages: int = 20_000
    max_entries: int = 50_000
    max_directory_bytes: int = 32 * 1024**2

    def __post_init__(self) -> None:
        for value in vars(self).values():
            if value is not None and (type(value) is not int or value <= 0):
                raise ValueError(
                    "Resource limits must be positive integers; None means no archive byte limit"
                )


def check_cancel(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise ImportCancelled("Import cancelled before publication")
