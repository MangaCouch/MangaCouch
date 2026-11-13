from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import signal
import sqlite3
import sys
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from .auth import KEY_KINDS, Auth
from .configuration import HTTPSettings, load_http_settings, load_profile, save_http_settings
from .database import Book, Library
from .errors import InvalidInput
from .files import LibraryRoot, RootUnavailable
from .limits import Limits
from .logs import LEVELS, configure, parse_level
from .preferences import preferred_title

# Modules that load libvips and PDFium (archives, ingest, lifecycle, snapshots, ...) are imported
# by the commands that need them, so `health`, the container's periodic probe, stays light.

if TYPE_CHECKING:
    import uvicorn

# Long enough that a reader turning pages keeps its connection; uvicorn's default is 5 s.
KEEP_ALIVE_SECONDS = 30
# Open requests get this long after a stop request; the lifespan shutdown then takes up to 60 s more.
# deploy/compose.yaml (stop_grace_period) and the desktop launcher (STOP_TIMEOUT) wait for both.
GRACEFUL_SHUTDOWN_SECONDS = 15
# Commands that only look: they never upgrade a library from an older release, which is irreversible.
READ_ONLY = {"health", "list", "key-list", "scan-status", "conflicts", "page"}
EPILOG = (
    "Options marked env also read that MANGACOUCH_* environment variable; an explicit option wins. "
    "On Windows and macOS, `mangacouch desktop` (or the standalone executable started without "
    "arguments) opens the tray launcher instead. Command overview: docs/install.md#command-line."
)


def book_summary(book: Book) -> dict:
    return {
        "id": book.id,
        "title": preferred_title(book),
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


def stop_on_stdin_eof(stop: Callable[[], None]) -> None:
    def wait() -> None:
        try:
            while os.read(sys.stdin.fileno(), 4096):
                pass
        except (OSError, ValueError):
            pass
        stop()

    threading.Thread(target=wait, name="stdin-eof", daemon=True).start()


# Console control events after which Windows ends the process once the handler returns.
CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 2, 5, 6
# Registered console handlers stay referenced for the life of the process: Windows may call one
# at any moment, including while the interpreter shuts down.
_CONSOLE_HANDLERS: list[Any] = []


def stop_on_windows_session_end(stop: Callable[[], None], finished: threading.Event) -> Any:
    """On Windows, turn console close, logoff and shutdown events into a graceful stop.

    The desktop launcher runs `serve` in a hidden console, whose default handler would end it at
    once. The handler runs on a thread of its own and holds the process until the server has
    finished or Windows's own deadline expires. Returns the registered callback; None elsewhere
    or if registration fails.
    """
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    prototype = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)  # pyright: ignore[reportAttributeAccessIssue]

    def handle(event: int) -> bool:
        if event not in {CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT}:
            return False  # Ctrl+C and Ctrl+Break keep Python's own handling
        stop()
        finished.wait(GRACEFUL_SHUTDOWN_SECONDS + 60)
        return True

    callback = prototype(handle)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # pyright: ignore[reportAttributeAccessIssue]
        register = kernel32.SetConsoleCtrlHandler
        register.argtypes = [prototype, wintypes.BOOL]
        register.restype = wintypes.BOOL
        _CONSOLE_HANDLERS.append(callback)
        if register(callback, True):
            return callback
    except (OSError, AttributeError):
        pass
    return None


def listed(name: str, convert=str.strip) -> list[str] | None:
    """A comma-separated environment variable; unset or empty means the default."""
    values = [convert(item.strip()) for item in os.environ.get(name, "").split(",") if item.strip()]
    return values or None


def serve_settings(args: argparse.Namespace) -> HTTPSettings:
    """This run's settings: the defaults plus this run's options, never values saved by an earlier run."""
    values: dict = {}
    if args.origin is not None:
        values["public_origin"] = args.origin
    if args.allowed_host is not None:
        values["allowed_hosts"] = args.allowed_host
    if args.upload_bytes is not None:
        values["max_upload_bytes"] = args.upload_bytes
    if args.cache_bytes is not None:
        values["thumbnail_cache_bytes"] = args.cache_bytes
    if args.trusted_proxy is not None:
        values["trusted_proxies"] = args.trusted_proxy
    return HTTPSettings.model_validate(values)


def health(library: Library, root: Path, args: argparse.Namespace) -> dict:
    """Whether the running server answers on its authentication boundary."""
    import httpx

    # The periodic health probe must not write to the books volume.
    LibraryRoot(library, root).check()
    if not Auth(library).configured():
        raise InvalidInput("Authentication is not initialized")
    origin: str = args.origin or load_http_settings(library).public_origin
    host: str = args.host
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    authority = urlsplit(origin).netloc
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
    if response.status_code != 401 or response.json().get("error") != "invalid_credentials":
        raise InvalidInput("HTTP authentication boundary is not ready")
    return {"ready": True}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MangaCouch personal comic library", epilog=EPILOG)
    parser.add_argument(
        "--db",
        type=Path,
        help="Library database, such as state/library.sqlite (env MANGACOUCH_DB); restore does not take it",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=os.environ.get("MANGACOUCH_ROOT") or None,
        help="Books folder (env MANGACOUCH_ROOT); init needs it, later commands read it from runtime.json",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=os.environ.get("MANGACOUCH_MAX_BYTES") or None,
        help="Largest archive accepted from uploads and import/, in bytes (env MANGACOUCH_MAX_BYTES; "
        "init and restore save it, other commands apply it to this run; default: the saved limit)",
    )
    parser.add_argument(
        "--log-level",
        choices=LEVELS,
        type=str.lower,
        default=None,
        help="Log detail on stderr (env MANGACOUCH_LOG_LEVEL; default info for serve, else warning)",
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("init", help="Create a library: the database (--db) and its books folder (--root)")
    commands.add_parser("import", help="Copy a CBZ/ZIP or PDF file into the library").add_argument(
        "source", type=Path
    )
    commands.add_parser(
        "index", help="Register a file that is already inside the books folder, without copying it"
    ).add_argument("source", type=Path)
    listing = commands.add_parser("list", help="List books as JSON")
    listing.add_argument("--query", default="", help="Search terms, as in the web app")
    listing.add_argument("--limit", type=int, default=50, help="Page size (1-1000)")
    listing.add_argument("--after", default="", help="Continuation cursor from the previous page")
    commands.add_parser(
        "doctor", help="Check the books folder and book availability; warn about a lost source key"
    )
    commands.add_parser("recover", help="Finish or roll back interrupted imports (serve does this on start)")
    scan = commands.add_parser("scan", help="Import one batch of files from import/; repeat to continue")
    scan.add_argument(
        "--limit", type=int, default=10, help="Work items in this batch (1-10); repeat to continue"
    )
    scan.add_argument(
        "--keep-source",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Keep files in import/ after verified import (default: the unfinished scan's choice, else no)",
    )
    commands.add_parser("scan-status", help="Show the latest scan")
    commands.add_parser("scan-pause", help="Pause the active scan")
    commands.add_parser("auth-init", help="Set the passcode of a new library (asked twice)")
    commands.add_parser(
        "change-passcode",
        help="Set a new passcode without the old one; signs out every device",
    ).add_argument("--revoke-keys", action="store_true", help="Revoke API keys as well")
    commands.add_parser("key-list", help="List API keys without their secrets")
    commands.add_parser("key-revoke", help="Revoke an API key").add_argument("id")
    reset = commands.add_parser("sources-reset", help="Reset source credentials locally; stop serve first")
    reset.add_argument("--confirm", action="store_true")
    key = commands.add_parser("key-create", help="Create an API key and print it once")
    key.add_argument("--kind", choices=KEY_KINDS, required=True)
    key.add_argument("--label", required=True)
    serve = commands.add_parser("serve", help="Run the web server")
    serve.add_argument(
        "--host",
        default=os.environ.get("MANGACOUCH_HOST") or "127.0.0.1",
        help="Listen address (env MANGACOUCH_HOST; default 127.0.0.1)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=os.environ.get("MANGACOUCH_PORT") or "8780",
        help="Listen port (env MANGACOUCH_PORT; default 8780)",
    )
    serve.add_argument(
        "--origin",
        default=os.environ.get("MANGACOUCH_ORIGIN") or None,
        help="Exact URL clients use (env MANGACOUCH_ORIGIN; default http://127.0.0.1:8780)",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        type=str.lower,
        help="Another accepted Host; repeat for several (env MANGACOUCH_ALLOWED_HOSTS, comma-separated)",
    )
    serve.add_argument(
        "--upload-bytes",
        type=int,
        default=os.environ.get("MANGACOUCH_UPLOAD_BYTES") or None,
        help="Largest web upload in bytes (env MANGACOUCH_UPLOAD_BYTES; default 2 GiB)",
    )
    serve.add_argument(
        "--cache-bytes",
        type=int,
        default=os.environ.get("MANGACOUCH_CACHE_BYTES") or None,
        help="Disk budget of the thumbnail cache in bytes (env MANGACOUCH_CACHE_BYTES; default 20 GiB)",
    )
    serve.add_argument(
        "--trusted-proxy",
        action="append",
        help="Reverse proxy IP or network whose X-Forwarded-For names the client; repeat for several "
        "(env MANGACOUCH_TRUSTED_PROXIES, comma-separated)",
    )
    serve.add_argument(
        "--check", action="store_true", help="Check startup without listening or saving settings"
    )
    # Used by the desktop tray: closing stdin (or the tray exiting) shuts the server down gracefully,
    # which also works on Windows, where a windowless child cannot receive Ctrl+C.
    serve.add_argument("--stop-on-stdin-eof", action="store_true", help=argparse.SUPPRESS)
    health = commands.add_parser("health", help="Check that the running server answers; exit 0 when ready")
    health.add_argument(
        "--host",
        default=os.environ.get("MANGACOUCH_HOST") or "127.0.0.1",
        help="Address serve listens on (env MANGACOUCH_HOST)",
    )
    health.add_argument(
        "--port",
        type=int,
        default=os.environ.get("MANGACOUCH_PORT") or "8780",
        help="Port serve listens on (env MANGACOUCH_PORT)",
    )
    health.add_argument(
        "--origin",
        default=os.environ.get("MANGACOUCH_ORIGIN") or None,
        help="Origin serve runs with (env MANGACOUCH_ORIGIN; default: the one serve last started with)",
    )
    sync = commands.add_parser("sync", help="Write pending .mangacouch.json sidecars")
    sync.add_argument("--book-id")
    sync.add_argument("--limit", type=int, default=100)
    sync.add_argument("--after", default="", help="Continue from the `after` cursor a previous run printed")
    commands.add_parser("conflicts", help="List sidecar conflicts").add_argument(
        "--limit", type=int, default=100
    )
    resolve = commands.add_parser("resolve", help="Resolve a sidecar conflict")
    resolve.add_argument("conflict_id")
    selection = resolve.add_mutually_exclusive_group(required=True)
    selection.add_argument("--choice", choices=("library", "file"))
    selection.add_argument("--fields", type=Path, help="JSON mapping each differing field to a version")
    commands.add_parser(
        "snapshot", help="Write a backup zip of the database, settings and credentials (not the books)"
    ).add_argument("output", type=Path)
    restore = commands.add_parser("restore", help="Restore a backup zip into a new state folder")
    restore.add_argument("snapshot", type=Path)
    restore.add_argument("output", type=Path, help="New folder for the restored database; must not exist")
    page = commands.add_parser("page", help="Write one page image of a book to a file")
    page.add_argument("book_id")
    page.add_argument("index", type=int, help="Zero-based page index")
    page.add_argument("output", type=Path)
    return parser


def _main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["desktop"] or (not argv and sys.platform in {"darwin", "win32"}):
        try:
            from .desktop import main as desktop_main
        except ImportError:  # Linux standalone builds leave the launcher out
            print("This build has no desktop launcher; see `mangacouch --help`.", file=sys.stderr)
            return 2
        return desktop_main(argv[1:])
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    cancelled = threading.Event()
    server: uvicorn.Server | None = None

    def stop() -> None:
        cancelled.set()
        if server is not None:
            server.should_exit = True

    if args.command == "serve" and args.stop_on_stdin_eof:
        # NumPy's Windows DLL initialization can deadlock while another thread reads stdin.
        # Preload it while keeping cancellation active during database upgrades.
        import numpy  # noqa: F401

        stop_on_stdin_eof(stop)
    try:
        level = parse_level(
            args.log_level or os.environ.get("MANGACOUCH_LOG_LEVEL"),
            "info" if args.command == "serve" else "warning",
        )
    except ValueError as error:
        parser.error(str(error))
    configure(level)
    if args.command != "restore" and args.db is None and os.environ.get("MANGACOUCH_DB"):
        args.db = Path(os.environ["MANGACOUCH_DB"])
    if args.command == "serve":
        if args.allowed_host is None:
            args.allowed_host = listed("MANGACOUCH_ALLOWED_HOSTS", str.lower)
        if args.trusted_proxy is None:
            args.trusted_proxy = listed("MANGACOUCH_TRUSTED_PROXIES")
    try:
        if args.command == "restore":
            if args.db is not None:
                raise InvalidInput("restore uses its new output directory; do not pass --db")
            from .diagnostics import library_status
            from .ingest import FileLibrary
            from .snapshots import restore_snapshot

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
            from .lifecycle import initialize_library

            library = initialize_library(args.db, args.root, limits)
            result = {
                "initialized": True,
                "database": str(library.path),
                "runtime_configuration": str(library.path.parent / "runtime.json"),
                "authentication_configured": False,
                "next_step": "auth-init",
            }
        elif args.command == "health":
            result = health(Library(args.db), args.root, args)
        else:
            from .ingest import FileLibrary
            from .lifecycle import open_library, startup_status

            read_only = args.command in READ_ONLY or (args.command == "serve" and args.check)
            # Read-only commands do not upgrade a library from an older release; they refuse it.
            library = (
                Library(args.db)
                if read_only
                else open_library(
                    args.db,
                    args.root,
                    limits,
                    cancelled=cancelled.is_set,
                    # A backup taken from an older release keeps its one pre-upgrade copy beside it.
                    backup=args.output if args.command == "snapshot" else None,
                )
            )
            files = FileLibrary(library, args.root, limits)
            if args.command in {"auth-init", "change-passcode"}:
                auth = Auth(library)
                new = read_passcode("New passcode (8-256 characters): ")
                if new != read_passcode("Repeat new passcode: "):
                    raise InvalidInput("Passcodes do not match")
                if args.command == "auth-init":
                    auth.initialize(new)
                else:
                    auth.change_passcode(new, revoke_keys=args.revoke_keys)
                result = {"authentication_configured": True}
                if args.command == "change-passcode":
                    result |= {"sessions_revoked": True, "api_keys_unchanged": not args.revoke_keys}
            elif args.command == "sources-reset":
                if not args.confirm:
                    raise InvalidInput(
                        "Stop serve, then use sources-reset --confirm to discard source credentials"
                    )
                from .source_config import SourceVault

                SourceVault(library).reset_credentials()
                result = {"source_credentials_reset": True, "settings_preserved": True}
            elif args.command == "key-list":
                result = {
                    "items": [item for item in Auth(library).list_credentials() if item["kind"] != "session"]
                }
            elif args.command == "key-revoke":
                Auth(library).revoke(args.id)
                result = {"revoked": args.id}
            elif args.command == "key-create":
                identifier, key = Auth(library).create_key(args.kind, args.label)
                result = {"id": identifier, "key": key, "kind": args.kind}
            elif args.command == "serve":
                settings = serve_settings(args)
                report = startup_status(files, settings, host=args.host, port=args.port)
                if args.check:
                    print(json.dumps(report, ensure_ascii=False, indent=2))
                    return 0 if report["startup_ready"] else 2
                if not report["startup_ready"]:
                    raise InvalidInput("; ".join(report["blockers"]))
                import uvicorn

                from .http import create_app

                app = create_app(files, settings)
                print(json.dumps(report, ensure_ascii=False), flush=True)
                options = {
                    "host": args.host,
                    "port": args.port,
                    "workers": 1,
                    "limit_concurrency": 128,
                    "proxy_headers": False,
                    "access_log": False,
                    "timeout_keep_alive": KEEP_ALIVE_SECONDS,
                    "timeout_graceful_shutdown": GRACEFUL_SHUTDOWN_SECONDS,
                    # logs.configure already routes uvicorn's loggers; keep its own config out.
                    "log_config": None,
                }

                class ConfiguredServer(uvicorn.Server):
                    async def startup(self, sockets=None):
                        await super().startup(sockets=sockets)
                        if self.started:
                            try:
                                save_http_settings(library, settings)
                            except BaseException:
                                await self.shutdown(sockets=sockets)
                                raise

                server = ConfiguredServer(uvicorn.Config(app, **options))
                if cancelled.is_set():
                    return 0
                finished = threading.Event()
                stop_on_windows_session_end(stop, finished)
                try:
                    server.run()
                finally:
                    finished.set()
                return 0
            elif args.command == "doctor":
                from .source_config import SourceVault

                # A lost source key only disables online sources; the library itself stays usable.
                warnings = []
                try:
                    SourceVault(library).load()
                except ValueError as error:
                    warnings.append({"warning": "source_key_lost", "detail": str(error)})
                try:
                    files.root.check(writable=True)
                except RootUnavailable:
                    print(json.dumps({"ready": False, "error": "root_unavailable"}))
                    return 2
                result = {
                    "ready": True,
                    "availability": files.refresh_availability(),
                    "source_key_lost": bool(warnings),
                    "warnings": warnings,
                }
            elif args.command in {"import", "index"}:
                from .sidecars import SidecarSync

                imported = files.import_file(args.source, index_existing=args.command == "index")
                result = {
                    "book": book_summary(imported.book),
                    "created": imported.created,
                    "job": imported.job_id,
                    "warnings": imported.warnings,
                    "sidecar_sync": SidecarSync(files).publish(imported.book.id),
                }
            elif args.command in {"scan", "scan-status", "scan-pause"}:
                from .scanning import Scanner

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
                    scanner.step(current["id"], limit=args.limit)
                    result = {"scan": scanner.get(current["id"])}
            elif args.command == "recover":
                result = {"jobs": files.recover()}
            elif args.command == "list":
                from .cursors import decode_cursor, encode_cursor

                if not 1 <= args.limit <= 1000:
                    raise InvalidInput("List limit must be between 1 and 1000")
                (after,) = decode_cursor(args.after, str) if args.after else ("",)
                ids = []
                while len(ids) < args.limit:
                    chunk = library.search(args.query, limit=min(200, args.limit - len(ids)), after_id=after)
                    ids.extend(chunk)
                    if not chunk:
                        break
                    after = chunk[-1]
                result = {
                    "items": [book_summary(book) for book in library.get_books(ids)],
                    "after": encode_cursor(ids[-1]) if len(ids) == args.limit else None,
                }
            elif args.command in {"sync", "conflicts", "resolve"}:
                from .sidecars import SidecarSync

                synchronizer = SidecarSync(files)
                if args.command == "sync":
                    result = (
                        synchronizer.sync_one(args.book_id)
                        if args.book_id
                        else synchronizer.batch(limit=args.limit, after=args.after)
                    )
                elif args.command == "conflicts":
                    result = {"conflicts": synchronizer.conflicts(limit=args.limit)}
                else:
                    from .snapshots import MAX_CONFIG_BYTES

                    fields = None
                    if args.fields:
                        with args.fields.open("rb") as stream:
                            fields = json.loads(stream.read(MAX_CONFIG_BYTES + 1))
                        if not isinstance(fields, dict):
                            raise InvalidInput("Field choices must be a JSON object")
                    resolved = synchronizer.resolve(
                        args.conflict_id, choice=args.choice or "library", fields=fields
                    )
                    result = {
                        "book": book_summary(resolved),
                        "sidecar_sync": synchronizer.publish(resolved.id),
                    }
            elif args.command == "snapshot":
                from .snapshots import create_snapshot

                result = {"snapshot": str(create_snapshot(files, args.output))}
            else:
                from .archives import image_media_type

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
        if cancelled.is_set():
            return 0
        code = getattr(error, "code", None) or type(error).__name__
        print(json.dumps({"error": code, "detail": str(error)}, ensure_ascii=False))
        return 1


def main() -> int:
    def terminate(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        return _main()
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
