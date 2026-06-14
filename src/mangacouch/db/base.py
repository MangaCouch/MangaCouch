"""SQLAlchemy engine + session, opened in a removable-media-safe mode (§3.1).

WAL's shared-memory mapping is unreliable on exFAT/removable/network filesystems, so we use
``journal_mode=TRUNCATE`` with ``synchronous=NORMAL`` and a generous ``busy_timeout`` instead.
Transactions are short and frequently committed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from natsort import natsort_keygen, ns
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None

_natkey = natsort_keygen(alg=ns.IGNORECASE | ns.LOCALE)


def _natural_collation(a: str | None, b: str | None) -> int:
    """A unicode-aware natural-order collation for title sorting (§5.1)."""
    ka, kb = _natkey(a or ""), _natkey(b or "")
    return (ka > kb) - (ka < kb)


def _apply_pragmas(dbapi_connection, _record) -> None:
    cur = dbapi_connection.cursor()
    # Removable-media safe: not WAL. Short, frequently-committed transactions instead.
    cur.execute("PRAGMA journal_mode=TRUNCATE")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # Tolerate brief writer contention between the web app and the worker threads.
    cur.execute("PRAGMA busy_timeout=8000")
    cur.execute("PRAGMA temp_store=MEMORY")
    cur.close()
    # Natural, unicode-aware ordering for `ORDER BY title COLLATE NATURAL`.
    dbapi_connection.create_collation("NATURAL", _natural_collation)


def init_engine(db_path: Path, *, echo: bool = False) -> Engine:
    """Create (once) the engine for ``library.sqlite`` and its session factory."""
    global _engine, _Session
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        future=True,
        # Workers run on threads; allow cross-thread use. SQLite serialises writes internally and
        # busy_timeout handles contention.
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _apply_pragmas)
    _engine = engine
    _Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Engine not initialised; call init_engine() first.")
    return _engine


def get_sessionmaker() -> sessionmaker[Session]:
    if _Session is None:
        raise RuntimeError("Session factory not initialised; call init_engine() first.")
    return _Session


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session context: commit on success, rollback on error, always close."""
    factory = get_sessionmaker()
    sess = factory()
    try:
        yield sess
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.close()
