"""The ``mangacouch`` console entry point: ``init``, ``serve``, ``scan``, ``refresh-tags``,
``set-passcode``, ``nuke``, ``mock``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
import string
import sys
from pathlib import Path

from . import __version__
from . import config as config_mod
from .auth.crypto import load_or_create_keyfile
from .auth.security import generate_api_key, hash_api_key, hash_passcode
from .config import Config, load_config


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _ensure_db(config: Config) -> None:
    from .db import models  # noqa: F401 — register all tables on Base.metadata
    from .db.base import Base, get_engine, init_engine

    config.ensure_roots()
    init_engine(config.library_db_path)
    Base.metadata.create_all(get_engine())
    load_or_create_keyfile(config.secrets_keyfile_path)


def friendly_passcode(length: int = 10) -> str:
    """A typable default passcode guaranteed to mix digits, uppercase and lowercase letters."""
    alphabet = string.ascii_letters + string.digits
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.isdigit() for c in code)
            and any(c.isupper() for c in code)
            and any(c.islower() for c in code)
        ):
            return code


def _provision(config: Config, role: str, passcode: str | None, *, force: bool) -> dict | None:
    from sqlalchemy import select

    from .db.base import session_scope
    from .db.models import AuthCredential

    with session_scope() as session:
        cred = session.scalar(select(AuthCredential).where(AuthCredential.role == role))
        if cred is not None and cred.passcode_hash and not force:
            return None
        passcode = passcode or friendly_passcode()
        api_key = generate_api_key()
        if cred is None:
            cred = AuthCredential(role=role)
            session.add(cred)
        cred.passcode_hash = hash_passcode(passcode)
        cred.api_key_hash = hash_api_key(api_key)
        cred.enabled = True
    return {"role": role, "passcode": passcode, "api_key": api_key}


def cmd_init(args: argparse.Namespace) -> int:
    base = Path(args.base).resolve() if args.base else None
    config_mod.write_default_config(base)
    config = load_config(base)
    _ensure_db(config)

    print(f"MangaCouch {__version__} initialised at {config.base_dir}")
    print(f"  database: {config.database_root}")
    print(f"  cache:    {config.cache_root}")
    print(f"  manga:    {config.manga_root}")
    print()

    owner = _provision(config, "owner", args.passcode, force=args.force)
    if owner:
        print("== OWNER credentials (shown ONCE — store them now) ==")
        print(f"   passcode: {owner['passcode']}")
        print(f"   api key : {owner['api_key']}")
        print()
        # Open the browser first-run window: the UI offers to keep or regenerate this passcode
        # (for installs where this terminal output is never seen, e.g. Docker).
        _set_app_flag("first_run_pending", "true")
    elif not args.force:
        print("Credentials already provisioned (use --force to regenerate).")

    if not args.no_tags:
        try:
            _refresh_tags(config)
        except Exception as exc:  # noqa: BLE001
            print(f"  (skipped tag database download: {exc})")
    print("Done. Run `mangacouch serve` to start.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    config.ensure_roots()
    host = args.host or config.server.host
    port = args.port or config.server.port
    app = create_app(config)
    print(f"MangaCouch {__version__} serving on http://{host}:{port}  (UI + /api + /docs)")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    ctx = _headless_context(config)
    try:
        stats = ctx.ingestor.scan()
        ctx._rebuild_search_if_empty()
        print(f"scan complete: {stats}")
    finally:
        ctx.shutdown()
    return 0


def cmd_refresh_tags(args: argparse.Namespace) -> int:
    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    _ensure_db(config)
    count = _refresh_tags(config)
    print(f"tag database refreshed: {count} entries")
    return 0


def cmd_set_passcode(args: argparse.Namespace) -> int:
    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    _ensure_db(config)
    cred = _provision(config, "owner", args.passcode, force=True)
    assert cred is not None
    print("owner passcode set.")
    print(f"   passcode: {cred['passcode']}")
    print(f"   api key : {cred['api_key']}  (store it — only the hash is kept)")
    return 0


def _set_app_flag(key: str, value: str) -> None:
    from .db.base import session_scope
    from .db.models import AppConfig

    with session_scope() as session:
        row = session.get(AppConfig, key)
        if row is None:
            session.add(AppConfig(key=key, value=value))
        else:
            row.value = value


def cmd_nuke(args: argparse.Namespace) -> int:
    """Wipe everything MangaCouch generated — database, caches, search index, thumbnails, tag
    translations, credentials, config.toml — while leaving the manga folder untouched."""
    import shutil

    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    nuke_config = not args.keep_config

    targets = [config.database_root, config.cache_root]
    # Safety: never delete a root the manga folder lives inside.
    manga = config.manga_root.resolve()
    for t in targets:
        t = t.resolve()
        if manga == t or t in manga.parents:
            print(f"refusing to nuke {t}: the manga folder {manga} lives inside it")
            return 1

    print("This will permanently delete:")
    for t in targets:
        print(f"  - {t}")
    if nuke_config:
        print(f"  - {config.base_dir / config_mod.CONFIG_FILENAME}")
    print(f"The manga folder is kept: {config.manga_root}")
    if not args.yes:
        answer = input("Type 'nuke' to confirm: ").strip().lower()
        if answer != "nuke":
            print("aborted.")
            return 1

    for t in targets:
        if t.exists():
            shutil.rmtree(t, ignore_errors=True)
            print(f"removed {t}")
    if nuke_config:
        (config.base_dir / config_mod.CONFIG_FILENAME).unlink(missing_ok=True)
        print("removed config.toml")
    print("Done. Run `mangacouch init` to start fresh (your manga folder is intact).")
    return 0


def cmd_mock(args: argparse.Namespace) -> int:
    """Generate mock .cbz archives (with sidecar metadata) in the manga folder for UI testing."""
    from .testing.mockdata import generate_mock_library

    base = Path(args.base).resolve() if args.base else None
    config = load_config(base)
    _ensure_db(config)
    created = generate_mock_library(config.manga_root, count=args.count, seed=args.seed)
    print(f"created {created} mock archives under {config.manga_root}")
    ctx = _headless_context(config)
    try:
        stats = ctx.ingestor.scan()
        ctx._rebuild_search_if_empty()
        print(f"scan complete: {stats}")
    finally:
        ctx.shutdown()
    return 0


def _headless_context(config: Config):
    """Build a context without the web server, for one-off CLI tasks (no watcher/worker started)."""
    from .state import build_context

    ctx = build_context(config, use_process_pool=False)
    ctx.translator.load_safe()
    ctx.registry.discover(config.base_dir / "plugins")
    return ctx


def _refresh_tags(config: Config) -> int:
    import json
    from datetime import UTC, datetime

    import httpx

    from .db.base import session_scope
    from .db.models import AppConfig
    from .tags.translation import fetch_tagdb, ingest_tagdb

    async def _go() -> dict:
        async with httpx.AsyncClient(
            proxy=config.acquisition.proxy or None, follow_redirects=True
        ) as client:
            return await fetch_tagdb(client)

    _ensure_db(config)
    data = asyncio.run(_go())
    with session_scope() as session:
        count = ingest_tagdb(session, data)
        # Stamp the refresh time so the server's periodic refresher doesn't re-download on boot.
        stamp = json.dumps(datetime.now(UTC).isoformat())
        row = session.get(AppConfig, "tagdb_refreshed_at")
        if row is None:
            session.add(AppConfig(key="tagdb_refreshed_at", value=stamp))
        else:
            row.value = stamp
        return count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mangacouch", description="MangaCouch server + tools")
    parser.add_argument("--version", action="version", version=f"mangacouch {__version__}")
    parser.add_argument("--base", help="base directory holding config.toml + the data roots")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config + database and provision credentials")
    p_init.add_argument("--passcode", help="owner passcode (default: generated)")
    p_init.add_argument("--force", action="store_true", help="regenerate existing credentials")
    p_init.add_argument("--no-tags", action="store_true", help="skip the tag-database download")
    p_init.set_defaults(func=cmd_init)

    p_serve = sub.add_parser("serve", help="run the web server")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", type=int)
    p_serve.set_defaults(func=cmd_serve)

    p_scan = sub.add_parser("scan", help="scan + index the manga folder, then exit")
    p_scan.set_defaults(func=cmd_scan)

    p_tags = sub.add_parser("refresh-tags", help="download the EhTagTranslation database")
    p_tags.set_defaults(func=cmd_refresh_tags)

    p_pass = sub.add_parser("set-passcode", help="set or reset the owner passcode + API key")
    p_pass.add_argument("--passcode")
    p_pass.set_defaults(func=cmd_set_passcode)

    p_nuke = sub.add_parser(
        "nuke", help="delete the database, caches, tag translations and config.toml "
        "(manga folder is kept)"
    )
    p_nuke.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    p_nuke.add_argument("--keep-config", action="store_true", help="keep config.toml")
    p_nuke.set_defaults(func=cmd_nuke)

    p_mock = sub.add_parser("mock", help="generate mock archives in the manga folder (testing)")
    p_mock.add_argument("--count", type=int, default=100)
    p_mock.add_argument("--seed", type=int, default=42)
    p_mock.set_defaults(func=cmd_mock)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
