"""Imaging via pyvips (R5) — the one image library. No Pillow.

pyvips' ``thumbnail_buffer`` is the fastest bulk-thumbnail path: it decodes, shrink-on-load, and
resizes in one native call. Raw (already-decoded) bitmaps — e.g. a rasterised PDF page out of
pdfium — are wrapped via ``new_from_memory`` after numpy normalises stride/byte-order.
"""
# pyvips builds its Image methods dynamically (via __getattr__), so static type checkers can't see
# thumbnail_image/flatten/write_to_buffer/etc. — suppress the resulting false positives here.
# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false, reportCallIssue=false
# pyright: reportOptionalCall=false, reportReturnType=false

from __future__ import annotations

import numpy as np
import pyvips

# Keep libvips from spamming stderr on slightly-malformed-but-recoverable images.
pyvips.voperation.cache_set_max(0)


def _suffix(fmt: str, quality: int) -> str:
    fmt = fmt.lower().lstrip(".")
    if fmt in ("jpg", "jpeg"):
        return f".jpg[Q={quality},optimize_coding=true]"
    if fmt == "webp":
        return f".webp[Q={quality}]"
    if fmt == "png":
        return ".png[compression=6]"
    return f".{fmt}"


def thumbnail_from_bytes(data: bytes, size: int, *, fmt: str = "webp", quality: int = 80) -> bytes:
    """Decode + shrink ``data`` so its long edge is ``size`` px, re-encode to ``fmt``.

    Recovers partial/corrupt images where libvips can (``fail_on=none``).
    """
    img = pyvips.Image.thumbnail_buffer(data, size, height=size, size="down")
    if img.hasalpha() and fmt.lower().lstrip(".") in ("jpg", "jpeg"):
        img = img.flatten(background=[255, 255, 255])
    return img.write_to_buffer(_suffix(fmt, quality))


def encode_rgba(buffer: bytes, width: int, height: int, bands: int, *, fmt: str = "png",
                quality: int = 90) -> bytes:
    """Encode a raw interleaved uchar bitmap (e.g. RGBA from pdfium) to ``fmt`` bytes."""
    img = pyvips.Image.new_from_memory(buffer, width, height, bands, "uchar")
    if bands == 4 and fmt.lower().lstrip(".") in ("jpg", "jpeg"):
        img = img.flatten(background=[255, 255, 255])
    return img.write_to_buffer(_suffix(fmt, quality))


def normalise_bitmap(array: np.ndarray) -> tuple[bytes, int, int, int]:
    """Return ``(contiguous_bytes, width, height, bands)`` for a pdfium ``to_numpy()`` array."""
    arr = np.ascontiguousarray(array)
    height, width = arr.shape[0], arr.shape[1]
    bands = arr.shape[2] if arr.ndim == 3 else 1
    return arr.tobytes(), width, height, bands


def thumbnail_from_rgba(buffer: bytes, width: int, height: int, bands: int, size: int, *,
                        fmt: str = "webp", quality: int = 80) -> bytes:
    img = pyvips.Image.new_from_memory(buffer, width, height, bands, "uchar")
    img = img.thumbnail_image(size, height=size, size="down")
    if img.hasalpha() and fmt.lower().lstrip(".") in ("jpg", "jpeg"):
        img = img.flatten(background=[255, 255, 255])
    return img.write_to_buffer(_suffix(fmt, quality))


def dimensions(data: bytes) -> tuple[int, int]:
    img = pyvips.Image.new_from_buffer(data, "")
    return img.width, img.height


def dhash(data: bytes, hash_size: int = 8) -> str:
    """Perceptual difference-hash of an (encoded) image — for near-duplicate cover detection (§4).

    Computed with pyvips + numpy (no extra image dependency). Returns hex of the bit string;
    compare two hashes by Hamming distance over the bits.
    """
    img = pyvips.Image.thumbnail_buffer(
        data, hash_size + 1, height=hash_size, size="force"
    ).colourspace("b-w")
    arr = np.ndarray(
        buffer=img.write_to_memory(), dtype=np.uint8, shape=(img.height, img.width)
    )
    diff = arr[:, 1:] > arr[:, :-1]
    bits = np.packbits(diff.flatten())
    return bits.tobytes().hex()


def hamming_distance(a: str, b: str) -> int:
    if len(a) != len(b):
        return max(len(a), len(b)) * 4
    return (int.from_bytes(bytes.fromhex(a), "big") ^ int.from_bytes(bytes.fromhex(b), "big")).bit_count()
