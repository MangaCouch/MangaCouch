"""Desktop launcher for Windows and macOS: a tray icon that runs the server, plus a settings window.

`mangacouch desktop` (or the standalone executable started without arguments) opens the tray;
`mangacouch desktop --settings` opens only the settings window, which the tray runs as a child process.
Needs the `desktop` extra (pystray and Pillow) and Tk.
"""

from __future__ import annotations

import sys
import webbrowser

from ..files import FileLock
from .config import app_directory, load_config


def acquire_instance_lock() -> FileLock | None:
    """Hold an exclusive lock for this process's lifetime; None when another tray holds it."""
    path = app_directory() / "desktop.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        return FileLock(path, blocking=False)
    except BlockingIOError:
        return None


def main(argv: list[str]) -> int:
    if argv == ["--settings"]:
        from .settings_window import run

        return run(load_config())
    if argv:
        print("usage: mangacouch desktop [--settings]", file=sys.stderr)
        return 2
    try:
        lock = acquire_instance_lock()
    except OSError as error:
        print(f"Cannot lock the desktop launcher: {error}", file=sys.stderr)
        return 1
    if lock is None:
        # Already running: bring up the library instead of a second icon.
        config = load_config()
        if config.state_dir:
            webbrowser.open(config.local_url)
        return 0
    try:
        from .tray import Tray
    except ImportError as error:
        print(f"The desktop launcher needs the `desktop` extra: {error}", file=sys.stderr)
        return 1
    with lock:
        Tray().run()
    return 0
