from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import uvicorn

from .archives import Limits, image_media_type
from .auth import KEY_KINDS, Auth
from .configuration import HTTPSettings, load_http_settings, save_http_settings
from .database import Book, Library
from .diagnostics import library_status
from .errors import InvalidInput
from .files import RootUnavailable
from .http import create_app
from .ingest import FileLibrary
from .lifecycle import initialize_library, load_profile, startup_status
from .scanning import Scanner
from .sidecars import SidecarSync
from .snapshots import MAX_CONFIG_BYTES, create_snapshot, restore_snapshot
from .source_config import SourceVault

# Long enough that a reader turning pages keeps its connection; uvicorn's default is 5 s.
KEEP_ALIVE_SECONDS = 30


def book_summary(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.metadata.title or book.metadata.original_title or book.content.original_filename,
        "pages": book.content.page_count,
        "path": book.content.relative_path,
        "availability": book.availability,
        "review": book.review_state,
    }


def read_passcode(prompt: str) -> str:
    if sys.stdin.isatty():
        return getpass.getpass(prompt)
    value = (
        sys.stdin.buffer.readline().decode("utf-8") if hasattr(sys.stdin, "buffer") else sys.stdin.readline()
    )
    if not value:
        raise InvalidInput("No passcode provided on stdin")
    return value.rstrip("\r\n")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="MangaCouch personal comic library")
    parser.add_argument("--db", type=Path)
    parser.add_argument("--root", type=Path, default=os.environ.get("MANGACOUCH_ROOT"))
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=os.environ.get("MANGACOUCH_MAX_BYTES") or None,
        help="Override the restored/default import byte limit",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    for command in ("import", "index"):
        commands.add_parser(command).add_argument("source", type=Path)
    commands.add_parser("list").add_argument("--query", default="")
    commands.add_parser("doctor")
    commands.add_parser("recover")
    scan = commands.add_parser("scan")
    scan.add_argument(
        "--limit", type=int, default=10, help="Work items in this batch (1-10); repeat to continue"
    )
    scan.add_argument("--resume", action="store_true", help="Explicitly resume a paused scan")
    scan.add_argument(
        "--keep-source",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep files in import/ after verified import (default: the unfinished scan's choice, else no)",
    )
    commands.add_parser("scan-status")
    commands.add_parser("scan-pause")
    commands.add_parser("auth-init")
    commands.add_parser("change-passcode")
    reset = commands.add_parser("sources-reset", help="Reset source credentials locally; stop serve first")
    reset.add_argument("--confirm", action="store_true")
    key = commands.add_parser("key-create")
    key.add_argument("--kind", choices=KEY_KINDS, required=True)
    key.add_argument("--label", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--host", default=os.environ.get("MANGACOUCH_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=os.environ.get("MANGACOUCH_PORT", "8780"))
    serve.add_argument("--origin", default=os.environ.get("MANGACOUCH_ORIGIN"))
    serve.add_argument("--allowed-host", action="append", help="Allowed Host; repeat for multiple hosts")
    serve.add_argument("--upload-bytes", type=int, default=os.environ.get("MANGACOUCH_UPLOAD_BYTES") or None)
    serve.add_argument(
        "--check", action="store_true", help="Check startup without listening or saving settings"
    )
    health = commands.add_parser("health")
    health.add_argument("--host", default=os.environ.get("MANGACOUCH_HOST", "127.0.0.1"))
    health.add_argument("--port", type=int, default=os.environ.get("MANGACOUCH_PORT", "8780"))
    sync = commands.add_parser("sync")
    sync.add_argument("--book-id")
    sync.add_argument("--limit", type=int, default=100)
    sync.add_argument("--after", default="")
    commands.add_parser("conflicts").add_argument("--limit", type=int, default=100)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("conflict_id")
    selection = resolve.add_mutually_exclusive_group(required=True)
    selection.add_argument("--choice", choices=("library", "file"))
    selection.add_argument("--fields", type=Path, help="JSON mapping each differing field to a version")
    commands.add_parser("snapshot").add_argument("output", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("output", type=Path)
    page = commands.add_parser("page")
    page.add_argument("book_id")
    page.add_argument("index", type=int, help="Zero-based page index")
    page.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command != "restore" and args.db is None and os.environ.get("MANGACOUCH_DB"):
        args.db = Path(os.environ["MANGACOUCH_DB"])
    if args.command == "serve" and args.allowed_host is None and os.environ.get("MANGACOUCH_ALLOWED_HOSTS"):
        args.allowed_host = [
            host.strip().lower() for host in os.environ["MANGACOUCH_ALLOWED_HOSTS"].split(",") if host.strip()
        ]
    try:
        if args.command == "restore":
            if args.db is not None:
                raise InvalidInput("restore uses its new output directory; do not pass --db")
            restored = restore_snapshot(
                args.snapshot, args.output, root=args.root, max_archive_bytes=args.max_bytes
            )
            files = FileLibrary(
                restored.library, Path(restored.profile.library_root_hint), restored.profile.limits
            )
            print(
                json.dumps(
                    {
                        "restored": True,
                        "diagnostics": library_status(files),
                        "http_settings": load_http_settings(restored.library).model_dump(),
                        "database": str(restored.library.path),
                        "runtime_configuration": str(restored.directory / "runtime.json"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.db is None:
            raise InvalidInput("--db is required except for restore")
        limits = Limits()
        if args.command != "init":
            if not args.db.is_file():
                raise InvalidInput(f"Database not found at {args.db}; run mangacouch init first")
            profile = load_profile(args.db)
            if profile is not None:
                limits = profile.limits
                if args.root is None:
                    args.root = Path(profile.library_root_hint)
        if args.root is None:
            raise InvalidInput("--root is required when no runtime configuration is present")
        if args.max_bytes is not None:
            limits = replace(limits, max_archive_bytes=args.max_bytes)
        if args.command == "init":
            library = initialize_library(args.db, args.root, limits)
            result = {
                "initialized": True,
                "database": str(library.path),
                "runtime_configuration": str(library.path.parent / "runtime.json"),
                "authentication_configured": False,
                "next_step": "auth-init",
            }
        else:
            library = Library(args.db)
            files = FileLibrary(library, args.root, limits)
            if args.command in {"auth-init", "change-passcode"}:
                auth = Auth(library)
                new = read_passcode("New passcode (8-256 characters): ")
                if new != read_passcode("Repeat new passcode: "):
                    raise InvalidInput("Passcodes do not match")
                if args.command == "auth-init":
                    auth.initialize(new)
                else:
                    auth.change_passcode(new)
                result = {"authentication_configured": True}
            elif args.command == "sources-reset":
                if not args.confirm:
                    raise InvalidInput(
                        "Stop serve, then use sources-reset --confirm to discard source credentials"
                    )
                SourceVault(library).reset_credentials()
                result = {"source_credentials_reset": True, "settings_preserved": True}
            elif args.command == "key-create":
                identifier, key = Auth(library).create_key(args.kind, args.label)
                result = {"id": identifier, "key": key, "kind": args.kind}
            elif args.command == "serve":
                values = load_http_settings(library).model_dump()
                if args.origin is not None:
                    values["public_origin"] = args.origin
                if args.allowed_host is not None:
                    values["allowed_hosts"] = args.allowed_host
                if args.upload_bytes is not None:
                    values["max_upload_bytes"] = args.upload_bytes
                settings = HTTPSettings.model_validate(values)
                report = startup_status(files, settings, host=args.host, port=args.port)
                if args.check:
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return 0 if report["startup_ready"] else 2
                if not report["startup_ready"]:
                    raise InvalidInput("; ".join(report["blockers"]))
                app = create_app(files, settings)
                save_http_settings(library, settings)
                print(json.dumps(report, ensure_ascii=False), flush=True)
                uvicorn.run(
                    app,
                    host=args.host,
                    port=args.port,
                    workers=1,
                    proxy_headers=False,
                    access_log=False,
                    timeout_keep_alive=KEEP_ALIVE_SECONDS,
                    timeout_graceful_shutdown=30,
                )
                return 0
            elif args.command == "doctor":
                SourceVault(library).load()
                try:
                    files.root.check(writable=True)
                except RootUnavailable:
                    print(json.dumps({"ready": False, "error": "root_unavailable"}))
                    return 2
                result = {"ready": True, "availability": files.refresh_availability()}
            elif args.command == "health":
                # The periodic health probe must not write to the books volume.
                files.root.check()
                if not Auth(library).configured():
                    raise InvalidInput("Authentication is not initialized")
                settings = load_http_settings(library)
                host: str = args.host
                host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                authority = urlsplit(settings.public_origin).netloc
                address = f"[{host}]" if ":" in host else host
                try:
                    response = httpx.get(
                        f"http://{address}:{args.port}/api/auth/me",
                        headers={"Host": authority},
                        timeout=3,
                        trust_env=False,
                    )
                except httpx.HTTPError as error:
                    raise InvalidInput("HTTP service is not reachable") from error
                if response.status_code != 401 or response.json() != {"error": "invalid_credentials"}:
                    raise InvalidInput("HTTP authentication boundary is not ready")
                result = {"ready": True}
            elif args.command in {"import", "index"}:
                imported = files.import_file(args.source, index_existing=args.command == "index")
                result = {
                    "book": book_summary(imported.book),
                    "created": imported.created,
                    "job": imported.job_id,
                    "warnings": imported.warnings,
                }
            elif args.command in {"scan", "scan-status", "scan-pause"}:
                scanner = Scanner(files)
                current = scanner.latest()
                if args.command == "scan-status":
                    result = {"scan": current}
                elif args.command == "scan-pause":
                    result = {"scan": scanner.pause(current["id"]) if current else None}
                else:
                    # Repeating `scan` continues a batch run; only an explicit flag changes its policy.
                    keep = args.keep_source
                    if keep is None:
                        keep = bool(current and current["state"] != "done" and current["keep_source"])
                    current = scanner.start(keep_source=keep)
                    if args.resume:
                        current = scanner.resume(current["id"])
                    scanner.step(current["id"], limit=args.limit)
                    result = {"scan": scanner.get(current["id"])}
            elif args.command == "recover":
                result = {"jobs": files.recover()}
            elif args.command == "list":
                result = {
                    "books": [
                        book_summary(library.get_book(book_id)) for book_id in library.search(args.query)
                    ]
                }
            elif args.command == "sync":
                synchronizer = SidecarSync(files)
                result = (
                    synchronizer.sync_one(args.book_id)
                    if args.book_id
                    else synchronizer.batch(limit=args.limit, after_id=args.after)
                )
            elif args.command == "conflicts":
                result = {"conflicts": SidecarSync(files).conflicts(limit=args.limit)}
            elif args.command == "resolve":
                fields = None
                if args.fields:
                    with args.fields.open("rb") as stream:
                        fields = json.loads(stream.read(MAX_CONFIG_BYTES + 1))
                    if not isinstance(fields, dict):
                        raise InvalidInput("Field choices must be a JSON object")
                resolved = SidecarSync(files).resolve(
                    args.conflict_id, choice=args.choice or "library", fields=fields
                )
                result = {"book": book_summary(resolved), "sidecar": "pending"}
            elif args.command == "snapshot":
                result = {"snapshot": str(create_snapshot(files, args.output))}
            else:
                data = files.page_bytes(args.book_id, args.index)
                with args.output.open("xb") as stream:
                    stream.write(data)
                result = {
                    "written": str(args.output),
                    "bytes": len(data),
                    "format": image_media_type(data).removeprefix("image/"),
                }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        code = getattr(error, "code", None) or type(error).__name__
        print(json.dumps({"error": code, "detail": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
