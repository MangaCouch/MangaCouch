"""Shared test fixtures: synthetic archives + a fully-wired (headless) app context."""
# pyright: reportAttributeAccessIssue=false, reportOptionalMemberAccess=false

from __future__ import annotations

import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import pyvips

from mangacouch.config import Config, ServerConfig, load_config
from mangacouch.state import AppContext, build_context


def make_image_bytes(color: tuple[int, int, int], size: int = 64, fmt: str = ".png") -> bytes:
    """A solid-colour test image encoded via pyvips (no Pillow)."""
    band = pyvips.Image.black(size, size)
    img = band.new_from_image(list(color)).copy(interpretation="srgb").cast("uchar")
    return img.write_to_buffer(fmt)


def make_zip(path: Path, pages: list[bytes], *, names: list[str] | None = None) -> Path:
    names = names or [f"{i + 1:03d}.png" for i in range(len(pages))]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in zip(names, pages, strict=True):
            zf.writestr(name, data)
    return path


def make_cbz(path: Path, pages: list[bytes]) -> Path:
    return make_zip(path, pages)


@pytest.fixture
def sample_pages() -> list[bytes]:
    return [
        make_image_bytes((220, 30, 30)),
        make_image_bytes((30, 220, 30)),
        make_image_bytes((30, 30, 220)),
    ]


@pytest.fixture
def roots(tmp_path: Path) -> Config:
    """A Config rooted under tmp_path with all four roots created."""
    from mangacouch import config as config_mod

    config_mod.write_default_config(tmp_path)
    config = load_config(tmp_path)
    config.server = ServerConfig(host="127.0.0.1", port=0)
    # No background network in tests: the periodic EhTagTranslation refresh would download
    # db.full.json from GitHub and race test DB writes.
    config.acquisition.tag_refresh_hours = 0
    config.ensure_roots()
    return config


@pytest.fixture
def context(roots: Config) -> Iterator[AppContext]:
    ctx = build_context(roots, use_process_pool=False)
    try:
        yield ctx
    finally:
        ctx.shutdown()


@pytest.fixture
def client(roots: Config) -> Iterator[object]:
    """A TestClient with the full app lifespan (workers + watcher started)."""
    from fastapi.testclient import TestClient

    from mangacouch.app import create_app

    app = create_app(roots)
    with TestClient(app) as c:
        yield c
