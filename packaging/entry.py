"""PyInstaller entry script — bakes UTF-8 mode in (§3.4, R7), then dispatches to the CLI.

Defaults to ``serve`` when frozen and launched with no subcommand (double-clicking the app).
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")

from mangacouch.cli import main

if __name__ == "__main__":
    argv = sys.argv[1:]
    if getattr(sys, "frozen", False) and not argv:
        argv = ["serve"]
    sys.exit(main(argv))
