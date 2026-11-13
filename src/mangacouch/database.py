from __future__ import annotations

import logging
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePath
from urllib.parse import quote

from .errors import InvalidInput, NotFound, RevisionConflict
from .metadata_import import MetadataImport
from .models import (
    EH_SITES,
    Content,
    Metadata,
    NativeSidecar,
    PersonalState,
    canonical_json,
    normalized,
)
from .schema import APPLICATION_ID, SCHEMA, SCHEMA_VERSION
from .searching import SearchOptions, search_ids

__all__ = [
    "APPLICATION_ID",
    "SCHEMA",
    "SCHEMA_VERSION",
    "Book",
    "Library",
    "RevisionConflict",
    "busy",
    "connect",
    "write_personal",
]

LOGGER = logging.getLogger(__name__)
BUSY_TIMEOUT = 8.0
"""Seconds a connection waits for another connection's write lock; a request then answers 503
database_busy. Background work that can afford to wait asks for more (`background_thread`)."""
SLOW_TRANSACTION = 2.0
"""A write transaction held longer than this is logged with its caller: every other writer, and
any reader opening the database meanwhile, waits behind it."""


_thread = threading.local()


def background_thread(busy_timeout: float = 30.0) -> None:
    """Set the database wait for this background thread only."""
    _thread.busy_timeout = busy_timeout


def busy(error: BaseException) -> bool:
    """Another connection held the database past the busy timeout: transient, not a storage fault."""
    return getattr(error, "sqlite_errorcode", 0) & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


@dataclass(frozen=True)
class Book:
    id: str
    content: Content
    metadata: Metadata
    metadata_revision: int
    personal: PersonalState
    personal_revision: int
    review_state: str
    availability: str
    thumbnail_revision: int


def search_text(content: Content, metadata: Metadata) -> str:
    fields = [
        content.original_filename,
        metadata.title or "",
        metadata.original_title or "",
        metadata.language or "",
        *(f"{t.namespace}:{t.value}" for t in metadata.tags),
    ]
    return normalized("\n".join(fields))


def write_tags(db: sqlite3.Connection, book_id: str, metadata: Metadata) -> None:
    """Keep the normalized search index in the same transaction as its metadata."""
    db.execute("DELETE FROM book_tag WHERE book_id=?", (book_id,))
    db.executemany(
        "INSERT OR IGNORE INTO book_tag(book_id,namespace,value) VALUES(?,?,?)",
        [(book_id, normalized(tag.namespace) or "other", normalized(tag.value)) for tag in metadata.tags],
    )


def write_source_links(db: sqlite3.Connection, book_id: str, metadata: Metadata) -> None:
    db.execute(
        "UPDATE book SET source_site_key=?,source_rating=? WHERE id=?",
        (
            metadata.source.site if metadata.source else "",
            metadata.source_rating.average if metadata.source_rating else None,
            book_id,
        ),
    )
    if metadata.source and metadata.source.site not in EH_SITES:
        # Online checks only serve E(x)Hentai: an older check's failure no longer applies.
        db.execute(
            "UPDATE source_check SET state='ignored',error_code=NULL,reasons_json='[]',retry_at=0,"
            "generation=generation+1 WHERE book_id=? AND state!='ignored'",
            (book_id,),
        )
    db.execute("DELETE FROM book_newer WHERE book_id=?", (book_id,))
    db.executemany(
        "INSERT OR IGNORE INTO book_newer VALUES(?,?,?)",
        (
            (book_id, "ehentai" if source.site == "exhentai" else source.site, source.gallery_id)
            for source in metadata.newer_versions
        ),
    )


def write_personal(
    db: sqlite3.Connection,
    book_id: str,
    old: PersonalState,
    new: PersonalState,
    expected_revision: int,
    *,
    record_activity: bool = True,
) -> None:
    """Compare-and-set a personal state and record the reading progress it implies."""
    if old.completed and new.completed:
        new = new.model_copy(update={"resume_page": old.resume_page})
    result = db.execute(
        "UPDATE personal SET state_json=?,revision=revision+1 WHERE book_id=? AND revision=?",
        (new.model_dump_json(), book_id, expected_revision),
    )
    if result.rowcount != 1:
        raise RevisionConflict("Personal state changed; reload the current values")
    if (new.resume_page, new.completed) == (old.resume_page, old.completed):
        # Rating edits do not move reading progress, so a Komga client that saw the
        # previous revision still holds current progress; advance its observation with the write.
        db.execute(
            "UPDATE client_observation SET revision=? WHERE book_id=? AND revision=?",
            (expected_revision + 1, book_id, expected_revision),
        )
        return
    if old.completed and not new.completed:
        # Undoing a manual completion (menu, bulk action, a client's "mark as read") also takes it
        # out of the statistics. A completion reached by reading stays: it really happened.
        latest = db.execute(
            "SELECT id,is_reading FROM reading_event WHERE book_id=? AND kind='completed' "
            "ORDER BY id DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        if latest is not None and not latest[1]:
            db.execute("DELETE FROM reading_event WHERE id=?", (latest[0],))
    if not record_activity and not (new.completed and not old.completed):
        if new.resume_page is None and not new.completed:
            # "Mark as unread" also leaves the recently read list; the reading history stays.
            db.execute("DELETE FROM reading_activity WHERE book_id=?", (book_id,))
        return
    now = int(time.time())
    if new.completed and not old.completed:
        kind = "completed"
    elif new.resume_page is None and not new.completed:
        kind = "reset"
    else:
        kind = "progress"
    db.execute(
        "INSERT INTO reading_event(book_id,created_at,page,completed,kind,is_reading) VALUES(?,?,?,?,?,?)",
        (book_id, now, new.resume_page, int(new.completed), kind, int(record_activity)),
    )
    if not record_activity:
        return
    if kind == "reset":
        db.execute("DELETE FROM reading_activity WHERE book_id=?", (book_id,))
    else:
        db.execute(
            "INSERT INTO reading_activity VALUES(?,?) "
            "ON CONFLICT(book_id) DO UPDATE SET updated_at=excluded.updated_at",
            (book_id, now),
        )


def database_uri(path: PurePath, mode: str = "rw") -> str:
    """An SQLite URI for an absolute path. A Windows UNC path keeps its server in the path: SQLite
    rejects a URI authority other than localhost."""
    if not path.is_absolute():
        raise ValueError("database_uri needs an absolute path")
    text = quote(path.as_posix(), safe="/:")
    prefix = "file:///" if path.drive and not text.startswith("//") else "file://"
    return f"{prefix}{text}?mode={mode}"


def connect(path: Path, *, durable: bool = False, timeout: float = BUSY_TIMEOUT) -> sqlite3.Connection:
    """Open an existing library database the way the application uses it.

    WAL with synchronous=NORMAL is crash-safe; durable=True also survives power loss for the
    last commit (snapshot/restore paths).
    """
    db = sqlite3.connect(database_uri(path), uri=True, isolation_level=None, timeout=timeout)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_size_limit=67108864")
        db.create_function("mc_normalized", 1, lambda value: normalized(value or ""), deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=" + ("FULL" if durable else "NORMAL"))
    except BaseException:
        db.close()
        raise
    return db


def refuse_pending(path: Path) -> None:
    """Refuse a database that an interrupted restore or initialization left behind."""
    if path.with_name(path.name + ".restore-pending").exists():
        raise InvalidInput("Restore is incomplete; use a completed restore directory")
    if path.with_name(path.name + ".init-pending").exists():
        raise InvalidInput("Initialization is incomplete; inspect the state and root before retrying")


BOOK_ROWS = (
    "SELECT b.id,b.content_json,b.metadata_json,b.metadata_revision,b.review_state,b.availability,"
    "p.state_json,p.revision AS personal_revision,coalesce(t.revision,0) AS thumbnail_revision "
    "FROM book b JOIN personal p ON p.book_id=b.id LEFT JOIN thumbnail_revision t ON t.book_id=b.id "
)


class Library:
    def __init__(self, path: Path):
        """Open a library database in the current format; `migrate.upgrade` brings older ones there."""
        self.path = path.resolve()
        self.busy_timeout = BUSY_TIMEOUT
        self._keeper: sqlite3.Connection | None = None
        self._keeper_users = 0
        self._keeper_lock = threading.Lock()
        refuse_pending(self.path)
        with self.connection() as db:
            if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
                raise InvalidInput(
                    "Not a supported MangaCouch database format; pre-1.0 libraries need separate "
                    "conversion before opening, or initialize a new library"
                )
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                raise InvalidInput("Not a supported MangaCouch database schema version")
            if version > SCHEMA_VERSION:
                raise InvalidInput(
                    "Library database was upgraded by a newer MangaCouch; run that release, "
                    "or restore the backup taken before the upgrade"
                )
            if version != SCHEMA_VERSION:
                raise InvalidInput(
                    "Library database is in an older format; start `mangacouch serve` once to "
                    "upgrade it (read-only commands never upgrade)"
                )

    @classmethod
    def initialize(cls, path: Path) -> Library:
        path = path.resolve()
        with path.open("xb"):
            pass
        try:
            with closing(sqlite3.connect(path, isolation_level=None)) as db:
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("BEGIN IMMEDIATE;" + SCHEMA)
                db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                db.execute("COMMIT")
            return cls(path)
        except BaseException:
            for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                leftover.unlink(missing_ok=True)
            raise

    def keep_alive(self) -> None:
        """Retain a read-only WAL connection while a server lifespan uses this library."""
        with self._keeper_lock:
            if self._keeper is None:
                keeper = sqlite3.connect(
                    database_uri(self.path, "ro"), uri=True, isolation_level=None, check_same_thread=False
                )
                try:
                    keeper.execute("SELECT 1 FROM sqlite_schema LIMIT 1").fetchone()
                except BaseException:
                    keeper.close()
                    raise
                self._keeper = keeper
            self._keeper_users += 1

    def close_keep_alive(self) -> None:
        with self._keeper_lock:
            if self._keeper_users:
                self._keeper_users -= 1
            if self._keeper is not None and not self._keeper_users:
                self._keeper.close()
                self._keeper = None

    @contextmanager
    def connection(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection; see `connect` for the durability trade-off."""
        db = connect(
            self.path, durable=durable, timeout=getattr(_thread, "busy_timeout", None) or self.busy_timeout
        )
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connection(durable=durable) as db:
            db.execute("BEGIN IMMEDIATE")
            locked = time.monotonic()
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                if db.in_transaction:
                    db.execute("ROLLBACK")
                raise
            finally:
                with suppress(Exception):
                    held = time.monotonic() - locked
                    if held > SLOW_TRANSACTION:
                        caller = sys._getframe(2)
                        LOGGER.warning(
                            "Write transaction held the database for %.1f s (%s:%d %s)",
                            held,
                            os.path.basename(caller.f_code.co_filename),
                            caller.f_lineno,
                            caller.f_code.co_name,
                        )

    def register_record(
        self,
        db: sqlite3.Connection,
        content: Content,
        metadata: Metadata,
        source_import: MetadataImport | None,
    ) -> tuple[str, bool]:
        existing = db.execute(
            "SELECT id, content_json FROM book WHERE sha256=?", (content.sha256,)
        ).fetchone()
        if existing:
            previous = Content.model_validate_json(existing["content_json"])
            if previous.byte_size != content.byte_size or previous.page_count != content.page_count:
                raise InvalidInput("Same hash has inconsistent verified content properties")
            book_id, created = existing["id"], False
        else:
            book_id, created = str(uuid.uuid4()), True
            db.execute(
                "INSERT INTO book(id,sha256,content_json,metadata_json,search_text,"
                "language_key,work_type_key) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    book_id,
                    content.sha256,
                    content.model_dump_json(),
                    metadata.model_dump_json(),
                    search_text(content, metadata),
                    normalized(metadata.language or ""),
                    normalized(metadata.work_type or ""),
                ),
            )
            db.execute(
                "INSERT INTO personal(book_id,state_json) VALUES(?,?)",
                (book_id, PersonalState().model_dump_json()),
            )
            db.execute("INSERT INTO sidecar_sync(book_id,pending_revision) VALUES(?,1)", (book_id,))
            write_tags(db, book_id, metadata)
            write_source_links(db, book_id, metadata)
            if source_import is not None:
                report = {
                    "candidates": {
                        key: value.model_dump(mode="json") for key, value in source_import.candidates.items()
                    },
                    "ignored_fields": source_import.ignored_fields,
                    "warnings": source_import.warnings,
                    "sources": source_import.sources,
                }
                db.execute(
                    "INSERT INTO metadata_import(book_id,report_json) VALUES(?,?)",
                    (book_id, canonical_json(report)),
                )
        return book_id, created

    def get_book(self, book_id: str, db: sqlite3.Connection | None = None) -> Book:
        if db is None:
            with self.connection() as db:
                return self.get_book(book_id, db)
        row = db.execute(BOOK_ROWS + "WHERE b.id=?", (book_id,)).fetchone()
        if row is None:
            raise NotFound("Book not found", code="book_not_found")
        return self._book(row)

    def get_books(self, book_ids: list[str], db: sqlite3.Connection | None = None) -> list[Book]:
        """Books in the requested order; ids that no longer exist are skipped."""
        if db is None:
            with self.connection() as db:
                return self.get_books(book_ids, db)
        rows = db.execute(
            BOOK_ROWS + "WHERE b.id IN (SELECT value FROM json_each(?))", (canonical_json(book_ids),)
        ).fetchall()
        by_id = {row["id"]: self._book(row) for row in rows}
        return [by_id[identifier] for identifier in book_ids if identifier in by_id]

    @staticmethod
    def _book(row: sqlite3.Row) -> Book:
        return Book(
            id=row["id"],
            content=Content.model_validate_json(row["content_json"]),
            metadata=Metadata.stored(row["metadata_json"]),
            metadata_revision=row["metadata_revision"],
            personal=PersonalState.model_validate_json(row["state_json"]),
            personal_revision=row["personal_revision"],
            review_state=row["review_state"],
            availability=row["availability"],
            thumbnail_revision=row["thumbnail_revision"],
        )

    def write_metadata(
        self,
        db: sqlite3.Connection,
        book_id: str,
        metadata: Metadata,
        expected_revision: int,
    ) -> None:
        row = db.execute("SELECT * FROM book WHERE id=?", (book_id,)).fetchone()
        if row is None:
            raise NotFound("Book not found", code="book_not_found")
        if row["metadata_revision"] != expected_revision:
            raise RevisionConflict("Metadata changed; reload the current values")
        previous = Metadata.stored(row["metadata_json"])
        if previous != metadata:
            revision = expected_revision + 1
            content = Content.model_validate_json(row["content_json"])
            db.execute(
                "UPDATE book SET metadata_json=?,metadata_revision=?,search_text=?,"
                "language_key=?,work_type_key=?,review_state='needs_review' WHERE id=?",
                (
                    metadata.model_dump_json(),
                    revision,
                    search_text(content, metadata),
                    normalized(metadata.language or ""),
                    normalized(metadata.work_type or ""),
                    book_id,
                ),
            )
            db.execute("UPDATE sidecar_sync SET pending_revision=? WHERE book_id=?", (revision, book_id))
            write_tags(db, book_id, metadata)
            write_source_links(db, book_id, metadata)

    def save_personal(
        self,
        book_id: str,
        state: PersonalState,
        *,
        expected_revision: int,
        also: Callable[[sqlite3.Connection], None] | None = None,
        record_activity: bool = True,
        base_state: PersonalState | None = None,
    ) -> Book:
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.content_json,p.state_json,p.revision FROM book b "
                "JOIN personal p ON p.book_id=b.id WHERE b.id=?",
                (book_id,),
            ).fetchone()
            if row is None:
                raise NotFound("Book not found", code="book_not_found")
            content = Content.model_validate_json(row["content_json"])
            pages = [] if state.resume_page is None else [state.resume_page]
            if any(page >= content.page_count for page in pages):
                raise InvalidInput("Personal state references a page outside this book")
            old = PersonalState.model_validate_json(row["state_json"])
            if base_state is not None and expected_revision < row["revision"]:
                # Page-unload snapshots may overtake an earlier request. Rebase only
                # fields this queue changed; preserve concurrent rating/finished edits.
                changes = {
                    key: value
                    for key, value in state.model_dump().items()
                    if value != getattr(base_state, key)
                }
                for key, value in changes.items():
                    if key != "resume_page" and getattr(old, key) not in (getattr(base_state, key), value):
                        raise RevisionConflict("Personal state changed; reload the current values")
                state = old.model_copy(update=changes)
                expected_revision = row["revision"]
            write_personal(db, book_id, old, state, expected_revision, record_activity=record_activity)
            if also is not None:
                also(db)
        return self.get_book(book_id)

    def native_sidecar(self, book_id: str) -> NativeSidecar:
        book = self.get_book(book_id)
        return NativeSidecar(
            schema="mangacouch.native/1",
            content_sha256=book.content.sha256,
            revision=book.metadata_revision,
            metadata=book.metadata,
        )

    def search(
        self, query: str, *, limit: int = 50, after_id: str = "", options: SearchOptions | None = None
    ) -> list[str]:
        with self.connection() as db:
            return search_ids(db, query, limit=limit, after_id=after_id, options=options)
