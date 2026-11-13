from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from .auth import Auth
from .configuration import HTTPSettings, RuntimeProfile, effective_upload_limit, load_profile
from .database import Library
from .diagnostics import library_status
from .errors import InvalidInput
from .files import ROOT_MARKER, LibraryRoot, atomic_write, publish_file, sync_directory
from .ingest import FileLibrary
from .limits import Limits
from .migrate import upgrade
from .source_config import SourceVault

WEB_DIR = Path(__file__).parent / "web"


def open_library(
    database: Path,
    root: Path | None = None,
    limits: Limits | None = None,
    *,
    cancelled: Callable[[], bool] = lambda: False,
    backup: Path | None = None,
) -> Library:
    """Open an existing library for this version: upgrade it to the current format first.

    `root` and `limits` default to the runtime configuration beside the database; `backup`
    names the pre-upgrade copy (see `migrate.upgrade`).
    """
    profile = load_profile(database)
    if profile is not None:
        root = root or Path(profile.library_root_hint)
        limits = limits or profile.limits
    upgrade(database, root=root, limits=limits, cancelled=cancelled, backup=backup)
    return Library(database)


def initialize_library(database: Path, root: Path, limits: Limits) -> Library:
    database, root = database.absolute(), root.absolute()
    profile_path = database.parent / "runtime.json"
    pending = database.with_name(database.name + ".init-pending")
    marker = root / ROOT_MARKER
    if not root.is_dir() or root.is_symlink():
        raise InvalidInput("Create an ordinary library directory before initialization")
    if database.parent.resolve().is_relative_to(root.resolve()):
        # Uploads are spooled next to the database, and files inside the root are only indexed.
        raise InvalidInput("Keep the database and state directory outside the library root")
    existing = (
        database,
        profile_path,
        pending,
        marker,
        database.with_name(database.name + ".restore-pending"),
    )
    for path in existing:
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Initialization will not replace existing state: {path}")
    profile = RuntimeProfile(schema="mangacouch.runtime/2", library_root_hint=str(root), limits=limits)
    owned: list[Path] = []
    bound: LibraryRoot | None = None
    with tempfile.TemporaryDirectory(prefix=".mangacouch-init-", dir=database.parent) as temporary:
        directory = Path(temporary)
        try:
            atomic_write(
                pending, b"Initialization incomplete; do not open this database.\n", mode=0o600, replace=False
            )
            owned.append(pending)
            sync_directory(database.parent)
            staged = Library.initialize(directory / "library.sqlite")
            os.chmod(staged.path, 0o600)
            bound = LibraryRoot.bind(staged, root)
            bound.check(writable=True)
            atomic_write(
                directory / "runtime.json",
                profile.model_dump_json(by_alias=True, exclude_defaults=True).encode(),
                mode=0o600,
            )
            for source, target in ((staged.path, database), (directory / "runtime.json", profile_path)):
                publish_file(source, target)
                owned.append(target)
            sync_directory(database.parent)
            pending.unlink()
            sync_directory(database.parent)
        except BaseException:
            for path in [
                *reversed(owned),
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
            ]:
                with suppress(OSError, ValueError):
                    path.unlink(missing_ok=True)
            with suppress(OSError, ValueError):
                if (
                    bound is not None
                    and marker.is_file()
                    and not marker.is_symlink()
                    and json.loads(marker.read_bytes())
                    == {"schema": "mangacouch.root/1", "id": bound.root_id}
                ):
                    marker.unlink()
                    sync_directory(root)
            raise
    return Library(database)


def startup_status(files: FileLibrary, settings: HTTPSettings, *, host: str, port: int) -> dict:
    if not host or any(character.isspace() for character in host) or not 1 <= port <= 65535:
        raise InvalidInput("Serve requires a host and a port between 1 and 65535")
    # A lost source key only turns online sources off; the settings page offers the reset.
    source_key_lost = SourceVault(files.library).key_lost()
    web = WEB_DIR
    assets: list[str] = []
    if (web / "index.html").is_file():
        index = (web / "index.html").read_text(encoding="utf-8")
        assets = re.findall(r'(?:src|href)="(/assets/[^"?]+)"', index)
    web_ready = bool(assets) and all((web / asset.lstrip("/")).is_file() for asset in assets)
    configured = Auth(files.library).configured()
    diagnostics = library_status(files)
    blockers: list[str] = []
    if not configured:
        blockers.append("Run auth-init to configure the library passcode")
    if not web_ready:
        blockers.append("Build web before starting the web application")
    return {
        "startup_ready": not blockers,
        "mode": "blocked" if blockers else "normal" if diagnostics["ready"] else "diagnostics_only",
        "blockers": blockers,
        "authentication_configured": configured,
        "web_assets_ready": web_ready,
        "source_key_lost": source_key_lost,
        "listen": {"host": host, "port": port},
        "http_settings": settings.model_dump(),
        "effective_max_upload_bytes": effective_upload_limit(settings, files.limits),
        "diagnostics": diagnostics,
    }
