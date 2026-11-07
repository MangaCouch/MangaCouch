from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import InvalidInput, NotFound, RevisionConflict
from .metadata_import import MetadataImport
from .models import Content, Metadata, NativeSidecar, PersonalState, canonical_json, normalized
from .schema import APPLICATION_ID, SCHEMA, SCHEMA_VERSION
from .searching import SearchOptions, search_ids

__all__ = [
    "APPLICATION_ID",
    "SCHEMA",
    "SCHEMA_VERSION",
    "Book",
    "Library",
    "RevisionConflict",
    "write_personal",
]


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


def write_personal(
    db: sqlite3.Connection, book_id: str, old: PersonalState, new: PersonalState, expected_revision: int
) -> None:
    """Compare-and-set a personal state and record the reading progress it implies."""
    result = db.execute(
        "UPDATE personal SET state_json=?,revision=revision+1 WHERE book_id=? AND revision=?",
        (new.model_dump_json(), book_id, expected_revision),
    )
    if result.rowcount != 1:
        raise RevisionConflict("Personal state changed; reload the current values")
    if (new.resume_page, new.completed) == (old.resume_page, old.completed):
        # Rating or bookmark edits do not move reading progress, so a Komga client that saw the
        # previous revision still holds current progress; advance its observation with the write.
        db.execute(
            "UPDATE client_observation SET revision=? WHERE book_id=? AND revision=?",
            (expected_revision + 1, book_id, expected_revision),
        )
        return
    now = int(time.time())
    if new.completed and not old.completed:
        kind = "completed"
    elif new.resume_page is None and not new.completed:
        kind = "reset"
    else:
        kind = "progress"
    db.execute(
        "INSERT INTO reading_event(book_id,created_at,page,completed,kind) VALUES(?,?,?,?,?)",
        (book_id, now, new.resume_page, int(new.completed), kind),
    )
    if kind == "reset":
        db.execute("DELETE FROM reading_activity WHERE book_id=?", (book_id,))
    else:
        db.execute(
            "INSERT INTO reading_activity VALUES(?,?) "
            "ON CONFLICT(book_id) DO UPDATE SET updated_at=excluded.updated_at",
            (book_id, now),
        )


class Library:
    def __init__(self, path: Path):
        self.path = path.resolve()
        if self.path.with_name(self.path.name + ".restore-pending").exists():
            raise InvalidInput("Restore is incomplete; use a completed restore directory")
        if self.path.with_name(self.path.name + ".init-pending").exists():
            raise InvalidInput("Initialization is incomplete; inspect the state and root before retrying")
        with self.connection() as db:
            if (
                db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
                or db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
            ):
                raise InvalidInput("Not a supported MangaCouch database; initialize a separate new database")

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

    @contextmanager
    def connection(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        """Open a connection; WAL with synchronous=NORMAL is crash-safe, durable=True also
        survives power loss for the last commit (snapshot/restore paths)."""
        db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, isolation_level=None, timeout=8)
        db.row_factory = sqlite3.Row
        db.create_function("mc_normalized", 1, lambda value: normalized(value or ""), deterministic=True)
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=" + ("FULL" if durable else "NORMAL"))
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self, *, durable: bool = False) -> Iterator[sqlite3.Connection]:
        with self.connection(durable=durable) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise

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
            if source_import is not None:
                report = {
                    "candidates": {
                        key: value.model_dump(mode="json") for key, value in source_import.candidates.items()
                    },
                    "conflicts": source_import.conflicts,
                    "ignored_fields": source_import.ignored_fields,
                    "warnings": source_import.warnings,
                    "sources": source_import.sources,
                    "native_revision": source_import.native.revision if source_import.native else None,
                }
                db.execute(
                    "INSERT INTO metadata_import(book_id,report_json) VALUES(?,?)",
                    (book_id, canonical_json(report)),
                )
        return book_id, created

    def get_book(self, book_id: str) -> Book:
        with self.connection() as db:
            row = db.execute(
                "SELECT b.*,p.state_json,p.revision AS personal_revision FROM book b "
                "JOIN personal p ON p.book_id=b.id WHERE b.id=?",
                (book_id,),
            ).fetchone()
        if row is None:
            raise NotFound("Book not found", code="book_not_found")
        return self._book(row)

    def get_books(self, book_ids: list[str], db: sqlite3.Connection) -> list[Book]:
        """Books in the requested order; ids that no longer exist are skipped."""
        rows = db.execute(
            "SELECT b.id,b.content_json,b.metadata_json,b.metadata_revision,b.review_state,"
            "b.availability,p.state_json,p.revision AS personal_revision FROM book b "
            "JOIN personal p ON p.book_id=b.id WHERE b.id IN (SELECT value FROM json_each(?))",
            (canonical_json(book_ids),),
        ).fetchall()
        by_id = {row["id"]: self._book(row) for row in rows}
        return [by_id[identifier] for identifier in book_ids if identifier in by_id]

    @staticmethod
    def _book(row: sqlite3.Row) -> Book:
        return Book(
            id=row["id"],
            content=Content.model_validate_json(row["content_json"]),
            metadata=Metadata.model_validate_json(row["metadata_json"]),
            metadata_revision=row["metadata_revision"],
            personal=PersonalState.model_validate_json(row["state_json"]),
            personal_revision=row["personal_revision"],
            review_state=row["review_state"],
            availability=row["availability"],
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
        previous = Metadata.model_validate_json(row["metadata_json"])
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

    def save_personal(self, book_id: str, state: PersonalState, *, expected_revision: int) -> Book:
        with self.transaction() as db:
            row = db.execute(
                "SELECT b.content_json,p.state_json FROM book b "
                "JOIN personal p ON p.book_id=b.id WHERE b.id=?",
                (book_id,),
            ).fetchone()
            if row is None:
                raise NotFound("Book not found", code="book_not_found")
            content = Content.model_validate_json(row["content_json"])
            pages = [*state.bookmarks, *([] if state.resume_page is None else [state.resume_page])]
            if any(page >= content.page_count for page in pages):
                raise InvalidInput("Personal state references a page outside this book")
            old = PersonalState.model_validate_json(row["state_json"])
            write_personal(db, book_id, old, state, expected_revision)
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
