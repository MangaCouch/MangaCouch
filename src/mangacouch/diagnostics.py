from __future__ import annotations

from .files import RootUnavailable
from .ingest import FileLibrary


def library_status(files: FileLibrary) -> dict:
    try:
        files.root.check(writable=True)
        ready = True
    except RootUnavailable:
        ready = False
    with files.library.connection() as db:
        db.execute("BEGIN")
        books = db.execute("SELECT count(*) FROM book").fetchone()[0]
        availability = dict(db.execute("SELECT availability,count(*) FROM book GROUP BY availability"))
        imports = dict(db.execute("SELECT state,count(*) FROM import_job GROUP BY state"))
        sidecars = db.execute(
            "SELECT count(*),count(last_error) FROM sidecar_sync "
            "WHERE pending_revision>synced_revision OR last_error IS NOT NULL"
        ).fetchone()
        conflicts = db.execute("SELECT count(*) FROM sidecar_conflict WHERE state='pending'").fetchone()[0]
        scan = db.execute("SELECT id,state FROM scan_run WHERE state!='done'").fetchone()
    return {
        "ready": ready,
        "books": books,
        "database": str(files.library.path),
        "library_root": str(files.root.path),
        "availability_last_known": availability,
        "imports": imports,
        "sidecars": {"pending": sidecars[0], "errors": sidecars[1]},
        "conflicts": conflicts,
        "scan": dict(scan) if scan else None,
    }
