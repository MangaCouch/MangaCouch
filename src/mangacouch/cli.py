"""The ``mangacouch`` console entry point: ``init``, ``serve``, ``scan``, ``refresh-tags``,
``set-passcode``."""

from __future__ import annotations

import argparse
import asyncio
import logging
import secrets
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


def _friendly_passcode() -> str:
    return secrets.token_hex(4)  # 8 typable hex chars


def _provision(config: Config, role: str, passcode: str | None, *, force: bool) -> dict | None:
    from sqlalchemy import select

    from .db.base import session_scope
    from .db.models import AuthCredential

    with session_scope() as session:
        cred = session.scalar(select(AuthCredential).where(AuthCredential.role == role))
        if cred is not None and cred.passcode_hash and not force:
            return None
        passcode = passcode or _friendly_passcode()
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

    owner = _provision(config, "owner", args.owner_passcode, force=args.force)
    reader = _provision(config, "reader", args.reader_passcode, force=args.force)
    for cred in (owner, reader):
        if cred:
            print(f"== {cred['role'].upper()} credentials (shown ONCE — store them now) ==")
            print(f"   passcode: {cred['passcode']}")
            print(f"   api key : {cred['api_key']}")
            print()
    if owner is None and reader is None and not args.force:
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
    cred = _provision(config, args.role, args.passcode, force=True)
    assert cred is not None
    print(f"{args.role} passcode set.")
    print(f"   passcode: {cred['passcode']}")
    print(f"   api key : {cred['api_key']}  (store it — only the hash is kept)")
    return 0


def _headless_context(config: Config):
    """Build a context without the web server, for one-off CLI tasks (no watcher/worker started)."""
    from .state import build_context

    ctx = build_context(config, use_process_pool=False)
    ctx.translator.load_safe()
    ctx.registry.discover(config.base_dir / "plugins")
    return ctx


def _refresh_tags(config: Config) -> int:
    import httpx

    from .db.base import session_scope
    from .tags.translation import fetch_tagdb, ingest_tagdb

    async def _go() -> dict:
        async with httpx.AsyncClient() as client:
            return await fetch_tagdb(client)

    _ensure_db(config)
    data = asyncio.run(_go())
    with session_scope() as session:
        return ingest_tagdb(session, data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mangacouch", description="MangaCouch server + tools")
    parser.add_argument("--version", action="version", version=f"mangacouch {__version__}")
    parser.add_argument("--base", help="base directory holding config.toml + the data roots")
    parser.add_argument("--log-level", default="INFO")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="create config + database and provision credentials")
    p_init.add_argument("--owner-passcode")
    p_init.add_argument("--reader-passcode")
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

    p_pass = sub.add_parser("set-passcode", help="set or reset a role's passcode + API key")
    p_pass.add_argument("role", choices=["owner", "reader"])
    p_pass.add_argument("--passcode")
    p_pass.set_defaults(func=cmd_set_passcode)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.log_level)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
