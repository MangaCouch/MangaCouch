"""Process-wide log setup: one timestamped stream on stderr for MangaCouch and uvicorn.

Levels:
    error    failures that need a look (unhandled exceptions, database errors)
    warning  work that stopped or is held back (rate limits, failed downloads, rejected hosts)
    info     what MangaCouch is doing (downloads, source checks, imports, scans, sign-ins)
    debug    every outbound source request and every HTTP request served
"""

from __future__ import annotations

import logging
import logging.config
import sys

LEVELS = ("debug", "info", "warning", "error")
FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
QUIET = ("httpx", "httpcore", "PIL", "multipart", "asyncio", "watchfiles")
"""Libraries kept at warning: httpx logs full URLs (archive keys) at info, the rest is noise."""


def parse_level(value: str | None, default: str) -> str:
    level = (value or default).strip().lower()
    if level not in LEVELS:
        raise ValueError(f"Log level must be one of {', '.join(LEVELS)}")
    return level


def configure(level: str = "info", stream=None) -> None:
    """Send `mangacouch.*` at `level`, uvicorn at info (or quieter) and others at warning to stderr."""
    numeric = getattr(logging, level.upper())
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"plain": {"format": FORMAT, "datefmt": DATE_FORMAT}},
            "handlers": {
                "stderr": {
                    "class": "logging.StreamHandler",
                    "formatter": "plain",
                    "stream": stream or sys.stderr,
                }
            },
            "root": {"level": "WARNING", "handlers": ["stderr"]},
            "loggers": {
                "mangacouch": {"level": numeric},
                "uvicorn": {"level": max(numeric, logging.INFO), "handlers": [], "propagate": True},
                "uvicorn.error": {"level": max(numeric, logging.INFO), "handlers": [], "propagate": True},
                "uvicorn.access": {"level": logging.WARNING, "handlers": [], "propagate": True},
                **{name: {"level": logging.WARNING} for name in QUIET},
            },
        }
    )


def short_id(identifier: str | None) -> str:
    """The first block of a UUID, enough to follow one job through the log."""
    return (identifier or "-").split("-", 1)[0]
