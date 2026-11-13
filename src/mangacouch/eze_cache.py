from __future__ import annotations

import hashlib
import json

from .eze import export_eze, eze_path
from .files import RootUnavailable, atomic_write, file_stamp
from .maintenance import Retry
from .models import EH_SITES, Metadata, canonical_json


def queue_eze(db, book_id: str, metadata: Metadata, *, fetched_at=None, gdata=None):
    if metadata.source is None or metadata.source.site not in EH_SITES:
        return
    payload = canonical_json(export_eze(metadata, fetched_at=fetched_at, gdata=gdata))
    db.execute(
        "INSERT INTO eze_cache(book_id,payload_json) VALUES(?,?) ON CONFLICT(book_id) "
        "DO UPDATE SET payload_json=excluded.payload_json,synced_hash=NULL",
        (book_id, payload),
    )


class EzeCache:
    def __init__(self, files):
        self.files = files
        self.retry = Retry()

    def sync_one(self, book_id: str):
        with self.files.root.mutation(), self.files.library.connection() as db:
            row = db.execute("SELECT * FROM eze_cache WHERE book_id=?", (book_id,)).fetchone()
            if row is None:
                return
            book = self.files.library.get_book(book_id)
            archive = self.files.root.resolve(book.content.relative_path)
            file_stamp(archive)
            path = eze_path(archive)
            data = (json.dumps(json.loads(row["payload_json"]), ensure_ascii=False, indent=2) + "\n").encode()
            digest = hashlib.sha256(data).hexdigest()
            if path.exists():
                file_stamp(path)
                if path.stat().st_size == len(data) and path.read_bytes() == data:
                    with self.files.library.transaction() as write:
                        write.execute("UPDATE eze_cache SET synced_hash=? WHERE book_id=?", (digest, book_id))
                    return
            atomic_write(path, data)
            with self.files.library.transaction() as write:
                write.execute("UPDATE eze_cache SET synced_hash=? WHERE book_id=?", (digest, book_id))

    def sync_pending(self, limit=10) -> int:
        """Write pending Eze sidecars; returns how many were written.

        A rotating cursor with per-book backoff keeps books whose write keeps failing (say, a
        read-only folder) from blocking the rest, and lets the worker idle instead of spinning.
        A failed write is not a missing archive: availability changes only when the archive is gone.
        """

        def select(after: str, count: int) -> list[str]:
            with self.files.library.connection() as db:
                return [
                    row[0]
                    for row in db.execute(
                        "SELECT e.book_id FROM eze_cache e JOIN book b ON b.id=e.book_id "
                        "WHERE e.synced_hash IS NULL AND b.availability='available' AND e.book_id>? "
                        "ORDER BY e.book_id LIMIT ?",
                        (after, count),
                    )
                ]

        written = 0
        for book_id in self.retry.take(select, limit):
            try:
                self.sync_one(book_id)
            except RootUnavailable:
                raise
            except KeyError:
                continue
            except (OSError, ValueError):
                self.retry.failed(book_id)
                try:
                    state, _ = self.files.availability(self.files.library.get_book(book_id))
                except KeyError:
                    continue
                if state != "available":
                    self.files.root.check()  # an offline root is not a missing archive either
                    self.files.observe_unavailable(self.files.library.get_book(book_id), state)
                continue
            self.retry.succeeded(book_id)
            written += 1
        return written
