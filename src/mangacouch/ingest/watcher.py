"""``watchfiles`` folder watcher — keeps the manga folder in sync with the DB (§3), cross-platform.

Runs on a background thread (the ingest work is blocking) with a ``threading.Event`` stop flag,
replacing LANraragi's dedicated Shinobu process. Partial reads during a copy are rejected by the
truncated-file check and re-tried on the copy's final modify event.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from watchfiles import Change, watch

from ..core.archives import is_supported
from .pipeline import Ingestor

log = logging.getLogger("mangacouch.watcher")

# Sidecars and partials we should ignore as *archive* changes.
_IGNORE_SUFFIXES = (".tmp", ".part", ".crdownload", ".json")


class LibraryWatcher:
    def __init__(self, manga_root: Path, ingestor: Ingestor) -> None:
        self.manga_root = manga_root
        self.ingestor = ingestor
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mc-watcher", daemon=True)
        self._thread.start()
        log.info("watching %s", self.manga_root)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _run(self) -> None:
        try:
            for changes in watch(self.manga_root, stop_event=self._stop, debounce=1600):
                for change, raw_path in changes:
                    try:
                        self._handle(change, Path(raw_path))
                    except Exception:
                        log.exception("watcher failed on %s", raw_path)
        except Exception:
            log.exception("watcher loop crashed")

    def _handle(self, change: Change, path: Path) -> None:
        if path.suffix.lower() in _IGNORE_SUFFIXES:
            return
        try:
            rel = path.relative_to(self.manga_root).as_posix()
        except ValueError:
            return
        if change is Change.deleted:
            self.ingestor.remove_path(rel)
            log.info("removed %s", rel)
        elif is_supported(path) and path.is_file():
            archive_id = self.ingestor.index_file(path)
            if archive_id:
                log.info("indexed %s (%s)", rel, archive_id[:12])
