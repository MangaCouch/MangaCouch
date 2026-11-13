"""The 1.0 baseline and the transactional mechanism for future library upgrades."""

from __future__ import annotations

import errno
import hashlib
import json
import sqlite3
import zipfile
from contextlib import closing
from pathlib import Path

import pytest
from helpers import python

from mangacouch import database, migrate, schema, snapshots
from mangacouch.database import Library
from mangacouch.errors import InvalidInput
from mangacouch.files import FileLock
from mangacouch.lifecycle import open_library
from mangacouch.snapshots import InvalidSnapshot, create_snapshot, restore_snapshot

LEGACY_APPLICATION_ID = 0x4D435232
NEXT_TABLE = "CREATE TABLE upgrade_probe(value INTEGER NOT NULL)"


def state_of(path: Path):
    with closing(sqlite3.connect(path)) as db:
        return (
            db.execute("PRAGMA application_id").fetchone()[0],
            db.execute("PRAGMA user_version").fetchone()[0],
            list(db.iterdump()),
        )


@pytest.fixture
def future_format(files, monkeypatch):
    """Exercise an artificial schema 1 -> 2 upgrade without retaining any 0.x format."""
    for module in (schema, database, migrate):
        monkeypatch.setattr(module, "SCHEMA_VERSION", 2)
    next_schema = schema.SCHEMA + NEXT_TABLE + ";"
    for module in (schema, database, snapshots):
        monkeypatch.setattr(module, "SCHEMA", next_schema)

    def step(context):
        context.db.execute(NEXT_TABLE)
        context.db.execute("INSERT INTO upgrade_probe VALUES(7)")

    monkeypatch.setitem(migrate.STEPS, 1, step)
    return files


def test_new_library_is_schema_one_in_a_distinct_format(files):
    app_id, version, _ = state_of(files.library.path)
    assert version == schema.SCHEMA_VERSION == 1
    assert app_id == schema.APPLICATION_ID != LEGACY_APPLICATION_ID
    assert migrate.STEPS == {}


def test_opening_current_library_is_a_noop(files):
    before = state_of(files.library.path)
    open_library(files.library.path, root=files.root.path)
    assert state_of(files.library.path) == before
    assert not list(files.library.path.parent.glob("*.pre-upgrade.sqlite"))


@pytest.mark.parametrize("version", [1, 22])
def test_pre_one_database_is_refused_even_when_its_version_matches(files, version):
    with files.library.connection() as db:
        db.execute(f"PRAGMA application_id={LEGACY_APPLICATION_ID}")
        db.execute(f"PRAGMA user_version={version}")
    before = state_of(files.library.path)
    for operation in (Library, migrate.upgrade, open_library):
        with pytest.raises(InvalidInput, match="pre-1.0"):
            operation(files.library.path)
    assert state_of(files.library.path) == before
    assert not list(files.library.path.parent.glob("*.pre-upgrade.sqlite"))


@pytest.mark.parametrize("version", [0, 2, 99])
def test_unknown_versions_are_refused_without_modification(files, version):
    with files.library.connection() as db:
        db.execute(f"PRAGMA user_version={version}")
    before = state_of(files.library.path)
    for operation in (Library, migrate.upgrade):
        with pytest.raises(InvalidInput, match="newer MangaCouch" if version > 1 else "schema version"):
            operation(files.library.path)
    assert state_of(files.library.path) == before
    assert not list(files.library.path.parent.glob("*.pre-upgrade.sqlite"))


def test_pre_one_snapshot_cannot_be_restored_as_schema_one(files, tmp_path):
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(snapshot) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    old = tmp_path / "old.sqlite"
    old.write_bytes(members["library.sqlite"])
    with closing(sqlite3.connect(old)) as db:
        db.execute(f"PRAGMA application_id={LEGACY_APPLICATION_ID}")
    members["library.sqlite"] = old.read_bytes()
    manifest = json.loads(members["manifest.json"])
    manifest["files"]["library.sqlite"] = {
        "sha256": hashlib.sha256(members["library.sqlite"]).hexdigest(),
        "size": len(members["library.sqlite"]),
    }
    members["manifest.json"] = json.dumps(manifest).encode()
    rewritten = tmp_path / "old-snapshot.zip"
    with zipfile.ZipFile(rewritten, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    destination = tmp_path / "restored"
    with pytest.raises(InvalidSnapshot, match="pre-1.0 backups"):
        restore_snapshot(rewritten, destination)
    assert not destination.exists()


def test_future_upgrade_saves_original_and_runs_once(future_format):
    files = future_format
    before = state_of(files.library.path)
    open_library(files.library.path, root=files.root.path)
    backups = list(files.library.path.parent.glob("*.schema-1-*.pre-upgrade.sqlite"))
    assert len(backups) == 1
    assert state_of(backups[0]) == before
    with files.library.connection() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT value FROM upgrade_probe").fetchone()[0] == 7
    migrated = state_of(files.library.path)
    open_library(files.library.path, root=files.root.path)
    assert state_of(files.library.path) == migrated
    assert list(files.library.path.parent.glob("*.pre-upgrade.sqlite")) == backups


def test_failed_future_upgrade_rolls_back_schema_and_rows(future_format, monkeypatch):
    files = future_format
    before = state_of(files.library.path)

    def broken(context):
        context.db.execute(NEXT_TABLE)
        context.db.execute("DELETE FROM search_revision")
        raise RuntimeError("step failed")

    monkeypatch.setitem(migrate.STEPS, 1, broken)
    with pytest.raises(RuntimeError, match="step failed"):
        migrate.upgrade(files.library.path)
    assert state_of(files.library.path) == before


def test_incomplete_future_chain_rolls_back(future_format, monkeypatch):
    path = future_format.library.path
    before = state_of(path)
    monkeypatch.setattr(migrate, "SCHEMA_VERSION", 3)
    with pytest.raises(InvalidInput, match="supported MangaCouch database"):
        migrate.upgrade(path)
    assert state_of(path) == before


def test_future_upgrade_checks_foreign_keys(future_format):
    path = future_format.library.path
    with closing(sqlite3.connect(path)) as db:
        db.execute("INSERT INTO personal(book_id,state_json) VALUES('missing','{}')")
        db.commit()
    before = state_of(path)
    with pytest.raises(InvalidInput, match="broken references"):
        migrate.upgrade(path)
    assert state_of(path) == before


def test_cancelled_future_step_rolls_back(future_format, monkeypatch):
    path = future_format.library.path
    before = state_of(path)
    cancelled = False

    def step(context):
        nonlocal cancelled
        context.db.execute(NEXT_TABLE)
        cancelled = True
        context.checkpoint()

    monkeypatch.setitem(migrate.STEPS, 1, step)
    with pytest.raises(InterruptedError, match="cancelled"):
        migrate.upgrade(path, cancelled=lambda: cancelled)
    assert state_of(path) == before


def test_backup_failure_prevents_any_upgrade(future_format, monkeypatch):
    path = future_format.library.path
    before = state_of(path)

    def fail(*args):
        raise OSError("backup disk full")

    monkeypatch.setattr(snapshots, "backup_before_upgrade", fail)
    with pytest.raises(OSError, match="backup disk full"):
        migrate.upgrade(path)
    assert state_of(path) == before


def pre_upgrade_copies(directory):
    return sorted(path.name for path in directory.glob("*.pre-upgrade.sqlite"))


def test_snapshot_of_an_old_library_saves_one_pre_upgrade_copy(files, tmp_path):
    output = tmp_path / "out" / "backup.zip"
    output.parent.mkdir()
    script = """
import sys
from mangacouch import database, migrate, schema, snapshots
from mangacouch.__main__ import main
migrate.SCHEMA_VERSION = database.SCHEMA_VERSION = schema.SCHEMA_VERSION = 2
statement = "CREATE TABLE upgrade_probe(value)"
schema.SCHEMA = database.SCHEMA = snapshots.SCHEMA = schema.SCHEMA + statement + ";"
migrate.STEPS[1] = lambda context: context.db.execute(statement)
sys.argv = ["mangacouch", *sys.argv[1:]]
raise SystemExit(main())
"""
    result = python("-c", script, "--db", files.library.path, "--root", files.root.path, "snapshot", output)
    assert result.returncode == 0, result.stdout + result.stderr
    assert pre_upgrade_copies(output.parent) == ["backup.zip.pre-upgrade.sqlite"]
    assert pre_upgrade_copies(files.library.path.parent) == []
    with closing(sqlite3.connect(str(output) + ".pre-upgrade.sqlite")) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with closing(sqlite3.connect(files.library.path)) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert output.is_file()


def test_lock_file_errors_other_than_a_held_lock_are_reported_as_themselves(future_format, monkeypatch):
    from mangacouch import migrate

    files = future_format

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", "library.sqlite.downloads.lock")

    with monkeypatch.context() as patch:
        patch.setattr(migrate, "FileLock", denied)
        with pytest.raises(PermissionError):
            migrate.upgrade(files.library.path)
    lock_path = files.library.path.with_name(files.library.path.name + ".downloads.lock")
    with FileLock(lock_path), pytest.raises(InvalidInput) as error:
        migrate.upgrade(files.library.path)
    assert error.value.code == "library_busy"
