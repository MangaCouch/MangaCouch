from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .archives import Limits
from .auth import Auth
from .configuration import HTTPSettings, effective_upload_limit
from .database import Library
from .diagnostics import library_status
from .errors import InvalidInput
from .files import ROOT_MARKER, LibraryRoot, publish_file, sync_directory
from .ingest import FileLibrary
from .snapshots import MAX_CONFIG_BYTES, RuntimeProfile, _write_durable
from .source_config import SourceVault

WEB_DIR = Path(__file__).parent / "web"


def load_profile(database: Path) -> RuntimeProfile | None:
    path = database.absolute().parent / "runtime.json"
    if path.is_symlink():
        raise InvalidInput("Runtime configuration must be an ordinary file")
    if not path.exists():
        return None
    with path.open("rb") as stream:
        data = stream.read(MAX_CONFIG_BYTES + 1)
    if len(data) > MAX_CONFIG_BYTES:
        raise InvalidInput("Runtime configuration exceeds the supported size")
    profile = RuntimeProfile.model_validate_json(data)
    if not Path(profile.library_root_hint).is_absolute():
        raise InvalidInput("Runtime library root must be an absolute path")
    return profile


def initialize_library(database: Path, root: Path, limits: Limits) -> Library:
    database, root = database.absolute(), root.absolute()
    profile_path = database.parent / "runtime.json"
    pending = database.with_name(database.name + ".init-pending")
    marker = root / ROOT_MARKER
    if not root.is_dir() or root.is_symlink():
        raise InvalidInput("Create an ordinary library directory before initialization")
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
    profile = RuntimeProfile(schema="mangacouch.runtime/1", library_root_hint=str(root), limits=limits)
    owned: list[Path] = []
    bound: LibraryRoot | None = None
    with tempfile.TemporaryDirectory(prefix=".mangacouch-init-", dir=database.parent) as temporary:
        directory = Path(temporary)
        try:
            _write_durable(pending, b"Initialization incomplete; do not open this database.\n")
            owned.append(pending)
            sync_directory(database.parent)
            staged = Library.initialize(directory / "library.sqlite")
            os.chmod(staged.path, 0o600)
            bound = LibraryRoot.bind(staged, root)
            bound.check(writable=True)
            _write_durable(directory / "runtime.json", profile.model_dump_json(by_alias=True).encode())
            for source, target in ((staged.path, database), (directory / "runtime.json", profile_path)):
                publish_file(source, target)
                owned.append(target)
            sync_directory(database.parent)
            pending.unlink()
            sync_directory(database.parent)
        except Exception:
            for path in reversed(owned):
                path.unlink(missing_ok=True)
            if (
                bound is not None
                and marker.is_file()
                and not marker.is_symlink()
                and json.loads(marker.read_bytes()) == {"schema": "mangacouch.root/1", "id": bound.root_id}
            ):
                marker.unlink()
                sync_directory(root)
            raise
    return Library(database)


def startup_status(files: FileLibrary, settings: HTTPSettings, *, host: str, port: int) -> dict:
    if not host or any(character.isspace() for character in host) or not 1 <= port <= 65535:
        raise InvalidInput("Serve requires a host and a port between 1 and 65535")
    source_error = None
    try:
        SourceVault(files.library).load()
    except ValueError as error:
        source_error = str(error)
    web = WEB_DIR
    assets: list[str] = []
    if (web / "index.html").is_file():
        assets = re.findall(r'(?:src|href)="(/assets/[^"?]+)"', (web / "index.html").read_text())
    web_ready = bool(assets) and all((web / asset.lstrip("/")).is_file() for asset in assets)
    configured = Auth(files.library).configured()
    diagnostics = library_status(files)
    blockers = [source_error] if source_error else []
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
        "listen": {"host": host, "port": port},
        "http_settings": settings.model_dump(),
        "effective_max_upload_bytes": effective_upload_limit(settings, files.limits),
        "diagnostics": diagnostics,
    }
