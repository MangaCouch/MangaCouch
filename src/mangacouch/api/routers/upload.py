"""Upload flow (flow 3): accept zip / pdf / cbz, save into manga/, parse and ingest (§2)."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from ...core.archives import detect_format, is_supported
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

    dest = ctx.config.manga_root / filename
    dest = _dedupe(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with tmp.open("wb") as fh:
            while chunk := await file.read(1 << 20):
                written += len(chunk)
                if written > _MAX_BYTES:
                    raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "file too large")
                fh.write(chunk)
    finally:
        await file.close()
    if written == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "empty upload")
    tmp.replace(dest)

    try:
        detect_format(dest)
        archive_id = ctx.ingestor.index_file(dest)
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
