"""Upload flow (flow 3): accept zip / pdf / cbz, save into manga/, parse and ingest (§2)."""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from starlette.concurrency import run_in_threadpool

from ...core.archives import is_supported
from ...state import AppContext
from ..deps import get_context, require_owner

router = APIRouter(prefix="/api", tags=["upload"])

_SAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_MAX_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB guard


def _safe_name(name: str) -> str:
    cleaned = _SAFE.sub("_", Path(name).name).strip().strip(".")
    return cleaned[:200] or "upload"


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    _: object = Depends(require_owner),
    ctx: AppContext = Depends(get_context),
) -> dict:
    filename = _safe_name(file.filename or "upload")
    if not is_supported(Path(filename)):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "unsupported format (zip / pdf / cbz only; zip is encouraged over cbz)",
        )

    root = ctx.config.manga_root
    root.mkdir(parents=True, exist_ok=True)

    # Unique temp name: concurrent uploads of the same filename must not share a .part file.
    # The leading dot + .part suffix keep it invisible to the watcher/scanner.
    tmp = root / f".upload-{uuid.uuid4().hex}.part"
    written = 0
    try:
        with tmp.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > _MAX_BYTES:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
                fh.write(chunk)
        if written == 0:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")

        # Claim a final name atomically (dedupe + exclusive create beats a check-then-rename race).
        dest = ctx.config.manga_root / filename
        while True:
            dest = _dedupe(dest)
            try:
                dest.touch(exist_ok=False)
                break
            except FileExistsError:
                continue
        tmp.replace(dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        await file.close()

    try:
        # Hashing/fingerprinting/thumbnailing a multi-GiB file must not block the event loop.
        archive_id = await run_in_threadpool(ctx.ingestor.index_file, dest)
    except Exception as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"could not parse: {exc}") from exc
    if archive_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "archive could not be indexed")
    return {"archive_id": archive_id, "filename": dest.name, "size": written}


def _dedupe(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, i = path.stem, path.suffix, 1
    while True:
        candidate = path.with_name(f"{stem} ({i}){suffix}")
        if not candidate.exists():
            return candidate
        i += 1
