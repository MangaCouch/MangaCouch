"""Organize flow (flow 2) — ingest correctness and the hard rules R2/R3/R4."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from mangacouch.core.thumbnails import COVER_PAGE, VARIANT_COVER
from mangacouch.db.base import session_scope
from mangacouch.db.models import Archive
from mangacouch.state import AppContext

from .conftest import make_image_bytes, make_zip


def _ingest(ctx: AppContext, name: str, pages) -> str:
    path = make_zip(ctx.config.manga_root / name, pages)
    archive_id = ctx.ingestor.index_file(path)
    assert archive_id is not None
    return archive_id


def test_ingest_creates_relative_row_and_cover(context: AppContext, sample_pages):
    archive_id = _ingest(context, "Gallery One.zip", sample_pages)

    with session_scope() as session:
        arch = session.get(Archive, archive_id)
        assert arch is not None
        # R2: never store absolute paths.
        assert arch.rel_path == "Gallery One.zip"
        assert not Path(arch.rel_path).is_absolute()
        assert arch.page_count == len(sample_pages)
        assert arch.fingerprint is not None
        assert arch.cover_status == "ready"

    # Cover thumbnail eagerly generated into the blob store (§4).
    assert context.thumbs.get(archive_id, COVER_PAGE, VARIANT_COVER) is not None


def test_reingest_is_idempotent_and_no_symlinks(context: AppContext, sample_pages):
    """R4: unchanged files are not re-indexed as new; R3: no symlinks are created."""
    first = _ingest(context, "dup.zip", sample_pages)
    path = context.config.manga_root / "dup.zip"
    second = context.ingestor.index_file(path)
    assert first == second

    with session_scope() as session:
        count = session.scalar(select(Archive).where(Archive.id == first))
        assert count is not None

    # R3: nothing under any root is a symlink.
    for root in (context.config.manga_root, context.config.cache_root, context.config.database_root):
        for p in root.rglob("*"):
            assert not p.is_symlink()


def test_changed_content_gets_new_identity(context: AppContext, sample_pages):
    first = _ingest(context, "g.zip", sample_pages)
    # Overwrite with different content at the same path.
    make_zip(context.config.manga_root / "g.zip", [*sample_pages[:1], make_image_bytes((7, 7, 7))])
    second = context.ingestor.index_file(context.config.manga_root / "g.zip")
    assert second is not None
    assert second != first
    with session_scope() as session:
        # The stale row was reconciled away; only the new identity remains for this path.
        rows = session.scalars(select(Archive).where(Archive.rel_path == "g.zip")).all()
        assert len(rows) == 1
        assert rows[0].id == second


def test_unicode_filename_round_trip(context: AppContext, sample_pages):
    archive_id = _ingest(context, "こみっく 日本語.zip", sample_pages)
    with session_scope() as session:
        arch = session.get(Archive, archive_id)
        assert arch is not None
        assert "日本語" in arch.title
        assert arch.original_filename == "こみっく 日本語.zip"


def test_sidecar_written_and_reimport(context: AppContext, sample_pages):
    from mangacouch.core import sidecars

    archive_id = _ingest(context, "with-sidecar.zip", sample_pages)
    path = context.config.manga_root / "with-sidecar.zip"
    mc = sidecars.read_mc(path)
    assert mc is not None
    assert mc.archive_id == archive_id
    assert mc.page_count == len(sample_pages)
    assert mc.fingerprint is not None


def test_remove_path_deletes_everything(context: AppContext, sample_pages):
    archive_id = _ingest(context, "gone.zip", sample_pages)
    context.ingestor.remove_path("gone.zip")
    with session_scope() as session:
        assert session.get(Archive, archive_id) is None
    assert context.thumbs.get(archive_id, COVER_PAGE, VARIANT_COVER) is None
