from __future__ import annotations

import hashlib
import json
import os
import tempfile

from .eze import export_eze, eze_path
from .files import file_stamp, sync_directory
from .models import Metadata, canonical_json


def queue_eze(db, book_id: str, metadata: Metadata, *, fetched_at=None, gdata=None):
    if metadata.source is None or metadata.source.site not in {"ehentai", "exhentai"}:
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
                    db.execute("UPDATE eze_cache SET synced_hash=? WHERE book_id=?", (digest, book_id))
                    return
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=".mangacouch-eze-", delete=False
                ) as out:
                    temporary = out.name
                    out.write(data)
                    out.flush()
                    os.fsync(out.fileno())
                os.replace(temporary, path)
                sync_directory(path.parent)
            finally:
                if temporary and os.path.exists(temporary):
                    os.unlink(temporary)
            db.execute("UPDATE eze_cache SET synced_hash=? WHERE book_id=?", (digest, book_id))

    def sync_pending(self, limit=10) -> int:
        """Write pending Eze sidecars; returns how many were written.

        A book whose write keeps failing stays pending but does not count, so the maintenance
        worker idles before trying it again instead of retrying it in a tight loop.
        """
        with self.files.library.connection() as db:
            ids = [
                row[0]
                for row in db.execute(
                    "SELECT e.book_id FROM eze_cache e JOIN book b ON b.id=e.book_id "
                    "WHERE synced_hash IS NULL AND b.availability='available' ORDER BY e.book_id LIMIT ?",
                    (limit,),
                )
            ]
        written = 0
        for book_id in ids:
            try:
                self.sync_one(book_id)
                written += 1
            except (OSError, ValueError):
                book = self.files.library.get_book(book_id)
                state, _ = self.files.availability(book)
                with self.files.library.transaction() as db:
                    db.execute("UPDATE book SET availability=? WHERE id=?", (state, book_id))
        return written
