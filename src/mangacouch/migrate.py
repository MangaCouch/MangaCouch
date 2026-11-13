"""Upgrade an existing library to the current format before anything else uses it.

Every format change of a library is one step here: the database schema and the data stored in
it (metadata, settings, download jobs, removal receipts, page manifests). `upgrade` runs the
steps a database still needs in one transaction at startup and on restore; the rest of
MangaCouch reads and writes only the current format, and `Library` refuses any other version.

The 1.0.0 format starts at schema 1; earlier formats are not migrated here. Future steps
are appended to STEPS under the version they upgrade from, raising schema.SCHEMA_VERSION by one.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .database import connect, refuse_pending
from .errors import InvalidInput
from .files import FileLock
from .limits import Limits
from .schema import APPLICATION_ID, SCHEMA_VERSION

LOGGER = logging.getLogger(__name__)
UNSUPPORTED = (
    "Not a supported MangaCouch database format; pre-1.0 libraries need separate conversion "
    "before opening, or initialize a new library"
)


@dataclass(frozen=True)
class Upgrade:
    db: sqlite3.Connection
    root: Path | None
    """The library's books folder; archives are read only for data the database never held."""
    limits: Limits
    cancelled: Callable[[], bool] = lambda: False

    def checkpoint(self) -> None:
        if self.cancelled():
            raise InterruptedError("Library upgrade cancelled")


Step = Callable[[Upgrade], None]


def upgrade(
    database: Path,
    *,
    root: Path | None = None,
    limits: Limits | None = None,
    cancelled: Callable[[], bool] = lambda: False,
    backup: Path | None = None,
) -> None:
    """Upgrade the library database to the current schema, after saving a copy of the old one
    as `<backup>.pre-upgrade.sqlite` (by default beside the database)."""
    path = database.resolve()
    refuse_pending(path)
    with closing(connect(path)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise InvalidInput(UNSUPPORTED)
        if version > SCHEMA_VERSION:
            raise InvalidInput(
                "Library database was upgraded by a newer MangaCouch; "
                "run that release or restore a pre-upgrade backup"
            )
        if version == SCHEMA_VERSION:
            return
        if version < 1 or version not in STEPS:
            raise InvalidInput("Not a supported MangaCouch database schema version")
    try:
        lock = FileLock(path.with_name(path.name + ".downloads.lock"), blocking=False)
    except BlockingIOError:
        # Only a held lock means another server; any other error (permissions, a read-only
        # state folder) is reported as itself.
        raise InvalidInput(
            "Stop the other MangaCouch server before upgrading this library", code="library_busy"
        ) from None
    with lock:
        from .snapshots import backup_before_upgrade

        saved = backup_before_upgrade(
            path, backup or path.with_name(f"{path.name}.schema-{version}-{uuid.uuid4().hex}")
        )
        if saved is not None:
            LOGGER.info("Pre-upgrade database saved to %s", saved)
        _upgrade(path, root=root, limits=limits, cancelled=cancelled)


def _upgrade(
    database: Path,
    *,
    root: Path | None = None,
    limits: Limits | None = None,
    cancelled: Callable[[], bool] = lambda: False,
) -> None:
    """Bring the library database at `database` to SCHEMA_VERSION, or raise and leave it unchanged.

    All steps and a foreign key check run in one transaction; another process that upgraded the
    database first leaves nothing to do.
    """
    path = database.resolve()
    refuse_pending(path)
    with closing(connect(path)) as db:
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise InvalidInput(UNSUPPORTED)
        if db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
            return
        context = Upgrade(db, root.absolute() if root else None, limits or Limits(), cancelled)
        context.checkpoint()
        db.set_progress_handler(lambda: int(cancelled()), 1000)
        # Table rebuilds run with foreign keys off, as the ALTER TABLE procedure requires; the pragma
        # is a no-op inside a transaction, so it is set first and the check runs before the commit.
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            while version in STEPS:
                context.checkpoint()
                LOGGER.info("Upgrading library database from schema %d", version)
                STEPS[version](context)
                version += 1
            if version != SCHEMA_VERSION:
                raise InvalidInput(UNSUPPORTED)
            if db.execute("PRAGMA foreign_key_check").fetchall():
                raise InvalidInput("Library database has broken references; restore a snapshot")
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.set_progress_handler(None, 0)
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.set_progress_handler(None, 0)
            db.execute("PRAGMA foreign_keys=ON")


STEPS: dict[int, Step] = {}
