from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from .archives import Limits, check_zip_directory
from .configuration import MAX_CONFIG_BYTES, HTTPSettings, RuntimeProfile, load_profile
from .database import APPLICATION_ID, SCHEMA, Library, database_uri
from .errors import InvalidInput
from .files import (
    CHUNK_SIZE,
    atomic_write,
    file_stamp,
    hash_file,
    publish_file,
    require_free_space,
    sync_directory,
    sync_file,
)
from .ingest import FileLibrary
from .migrate import upgrade
from .models import SHA256, Model
from .preferences import LibraryPreferences

MAX_DATABASE_BYTES = 8 * 1024**3


def snapshot_directory(library: Library) -> Path:
    return library.path.parent / (".mangacouch-snapshots-" + library.path.name)


def backup_before_upgrade(database: Path, output: Path) -> Path | None:
    from .schema import SCHEMA_VERSION

    with closing(sqlite3.connect(database_uri(database.resolve(), "ro"), uri=True)) as source:
        version = source.execute("PRAGMA user_version").fetchone()[0]
        if (
            source.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID
            or version >= SCHEMA_VERSION
        ):
            return None
        target = Path(str(output) + ".pre-upgrade.sqlite")
        with target.open("xb"):
            pass
        try:
            os.chmod(target, 0o600)
            with closing(sqlite3.connect(target)) as destination:
                source.backup(destination)
            sync_file(target)
            sync_directory(target.parent)
        except BaseException:
            with suppress(OSError):
                target.unlink(missing_ok=True)
            raise
        return target


class InvalidSnapshot(ValueError):
    pass


class SnapshotEntry(Model):
    sha256: SHA256
    size: Annotated[int, Field(gt=0, le=MAX_DATABASE_BYTES)]


class SnapshotManifest(Model):
    schema_name: Literal["mangacouch.snapshot/1"] = Field(alias="schema")
    files: dict[Literal["library.sqlite", "runtime.json", "sources.key"], SnapshotEntry]

    @model_validator(mode="after")
    def exact_entries(self) -> Self:
        if not {"library.sqlite", "runtime.json"} <= set(self.files):
            raise ValueError("Snapshot requires a database and runtime configuration")
        return self


@dataclass(frozen=True)
class RestoredSnapshot:
    library: Library
    profile: RuntimeProfile
    directory: Path


def _validate_database(path: Path, *, root: Path | None = None, limits: Limits | None = None) -> None:
    """Check a database copy and upgrade it to the current format (`root` and `limits` are the
    library's, for data that only its archives hold); it must then match this version's schema."""
    with closing(sqlite3.connect(database_uri(path, "ro"), uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise InvalidSnapshot(
                "Snapshot contains a different database format; pre-1.0 backups need separate conversion"
            )
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise InvalidSnapshot("Snapshot database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchall():
            raise InvalidSnapshot("Snapshot contains broken database references")
    try:
        upgrade(path, root=root, limits=limits)
    except InvalidInput as error:
        if error.code == "root_unavailable":
            raise
        raise InvalidSnapshot("Snapshot database schema is unsupported") from error
    with closing(sqlite3.connect(database_uri(path, "rw"), uri=True)) as db:
        db.execute("PRAGMA trusted_schema=OFF")
        query = (
            "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        with closing(sqlite3.connect(":memory:")) as expected:
            expected.executescript(SCHEMA)
            if db.execute(query).fetchall() != expected.execute(query).fetchall():
                raise InvalidSnapshot("Snapshot database objects do not match this application version")
        settings = db.execute("SELECT settings_json FROM http_config").fetchone()
        if settings:
            value = HTTPSettings.model_validate_json(settings[0], extra="ignore")
            db.execute("UPDATE http_config SET settings_json=?", (value.model_dump_json(),))
        preferences = db.execute("SELECT settings_json FROM library_preferences").fetchone()
        if preferences:
            value = LibraryPreferences.model_validate_json(preferences[0], extra="ignore")
            db.execute("UPDATE library_preferences SET settings_json=?", (value.model_dump_json(),))
        db.commit()


def _validate_source_key(database: Path, key: Path) -> None:
    from cryptography.fernet import Fernet, InvalidToken

    from .source_config import SourceSettings

    try:
        cipher = Fernet(key.read_bytes())
        with closing(sqlite3.connect(database)) as db:
            settings = db.execute("SELECT settings_json,secrets FROM source_config").fetchone()
            if settings:
                SourceSettings.model_validate_json(settings[0])
                if settings[1]:
                    cipher.decrypt(settings[1].encode())
            for (value,) in db.execute(
                "SELECT remote_secret FROM download_job WHERE remote_secret IS NOT NULL"
            ):
                cipher.decrypt(value.encode())
    except (OSError, ValueError, InvalidToken):
        raise InvalidSnapshot(
            "Snapshot source key is missing or does not match its encrypted state"
        ) from None


def _copy_database(library: Library, output: Path) -> None:
    """Copy the live database in one backup step.

    A single step reads from one WAL snapshot, so concurrent writers neither
    block it nor force it to restart (stepwise backups restart on every write).
    """
    atomic_write(output, b"", mode=0o600, replace=False)
    with (
        closing(sqlite3.connect(database_uri(library.path, "ro"), uri=True, timeout=8)) as source,
        closing(sqlite3.connect(output, isolation_level=None)) as destination,
    ):
        needed = (
            source.execute("PRAGMA page_count").fetchone()[0]
            * source.execute("PRAGMA page_size").fetchone()[0]
        )
        require_free_space(output.parent, needed * 2)
        source.backup(destination)
        for table in ("duplicate_edge", "page_band", "page_feature", "duplicate_progress"):
            destination.execute(f"DELETE FROM {table}")
        destination.execute("PRAGMA foreign_keys=ON")
        destination.execute("UPDATE book SET indexed_pages=0")
        destination.execute("VACUUM")
        destination.execute("PRAGMA journal_mode=DELETE")
    if output.stat().st_size > MAX_DATABASE_BYTES:
        raise InvalidSnapshot("Snapshot database exceeds the supported size")
    with output.open("r+b") as stream:
        os.fsync(stream.fileno())


def _needs_source_key(db: sqlite3.Connection) -> bool:
    return bool(
        db.execute(
            "SELECT EXISTS(SELECT 1 FROM source_config WHERE secrets!='') "
            "OR EXISTS(SELECT 1 FROM download_job WHERE remote_secret IS NOT NULL)"
        ).fetchone()[0]
    )


def create_snapshot(files: FileLibrary, destination: Path) -> Path:
    from .source_config import SourceVault

    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Snapshot destination already exists")
    SourceVault(files.library).load()
    profile = load_profile(files.library.path) or RuntimeProfile(
        schema="mangacouch.runtime/2", library_root_hint=str(files.root.path), limits=files.limits
    )
    with tempfile.TemporaryDirectory(prefix=".mangacouch-snapshot-", dir=destination.parent) as temporary:
        temporary_path = Path(temporary)
        database = temporary_path / "library.sqlite"
        _copy_database(files.library, database)
        _validate_database(database)
        config = temporary_path / "runtime.json"
        atomic_write(
            config,
            profile.model_dump_json(by_alias=True, exclude_defaults=True).encode(),
            mode=0o600,
            replace=False,
        )
        members = [database, config]
        with closing(sqlite3.connect(database)) as state:
            needs_key = _needs_source_key(state)
        if needs_key:
            from .source_config import SourceKeyLost

            key = temporary_path / "sources.key"
            try:
                atomic_write(key, SourceVault(files.library).path.read_bytes(), mode=0o600, replace=False)
                _validate_source_key(database, key)
            except (OSError, InvalidSnapshot) as error:
                raise SourceKeyLost(
                    "Reset the unreadable source credentials before creating a backup"
                ) from error
            members.append(key)
        manifest = SnapshotManifest.model_validate(
            {
                "schema": "mangacouch.snapshot/1",
                "files": {
                    path.name: {"sha256": hash_file(path), "size": path.stat().st_size} for path in members
                },
            }
        )
        archive_path = temporary_path / "snapshot.zip"
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in members:
                archive.write(path, path.name)
            archive.writestr("manifest.json", manifest.model_dump_json(by_alias=True))
        os.chmod(archive_path, 0o600)
        with archive_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        publish_file(archive_path, destination)
        try:
            sync_directory(destination.parent)
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
    return destination


def _extract_verified(snapshot: Path, directory: Path, root: Path | None) -> RuntimeProfile:
    before = file_stamp(snapshot)
    try:
        check_zip_directory(snapshot, Limits(max_entries=4, max_directory_bytes=MAX_CONFIG_BYTES))
        with zipfile.ZipFile(snapshot) as archive:
            entries = archive.infolist()
            names = {entry.filename for entry in entries}
            if len(entries) != len(names) or names not in (
                {"library.sqlite", "runtime.json", "manifest.json"},
                {"library.sqlite", "runtime.json", "manifest.json", "sources.key"},
            ):
                raise InvalidSnapshot("Unexpected, duplicate or unsafe snapshot entries")
            for entry in entries:
                maximum = MAX_DATABASE_BYTES if entry.filename == "library.sqlite" else MAX_CONFIG_BYTES
                if (
                    entry.file_size > maximum
                    or entry.flag_bits & 1
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise InvalidSnapshot("Snapshot member exceeds size or file-type restrictions")
            manifest = SnapshotManifest.model_validate_json(archive.read("manifest.json"))
            if set(manifest.files) != names - {"manifest.json"}:
                raise InvalidSnapshot("Snapshot manifest members do not match")
            required_space = sum(entry.size for entry in manifest.files.values())
            if shutil.disk_usage(directory).free < required_space + 8 * 1024**2:
                raise OSError("Insufficient space to restore snapshot")
            for name, expected in manifest.files.items():
                if archive.getinfo(name).file_size != expected.size:
                    raise InvalidSnapshot("Snapshot member size does not match its manifest")
                digest = hashlib.sha256()
                size = 0
                path = directory / name
                with archive.open(name) as source, path.open("xb") as target:
                    os.chmod(path, 0o600)
                    while data := source.read(CHUNK_SIZE):
                        size += len(data)
                        if size > expected.size:
                            raise InvalidSnapshot("Snapshot expansion exceeds its manifest")
                        digest.update(data)
                        target.write(data)
                    target.flush()
                    os.fsync(target.fileno())
                if size != expected.size or digest.hexdigest() != expected.sha256:
                    raise InvalidSnapshot("Snapshot checksum mismatch")
        if file_stamp(snapshot) != before:
            raise InvalidSnapshot("Snapshot changed while being restored")
        profile = RuntimeProfile.model_validate_json((directory / "runtime.json").read_bytes())
        if root is None and Path(profile.library_root_hint).is_absolute():
            root = Path(profile.library_root_hint)
        _validate_database(directory / "library.sqlite", root=root, limits=profile.limits)
        with closing(sqlite3.connect(directory / "library.sqlite")) as db:
            needs_key = _needs_source_key(db)
        if needs_key or (directory / "sources.key").exists():
            _validate_source_key(directory / "library.sqlite", directory / "sources.key")
        return profile
    except InvalidSnapshot:
        raise
    except InvalidInput as error:
        if error.code == "root_unavailable":
            raise
        raise InvalidSnapshot("Snapshot failed database validation") from error
    except (zipfile.BadZipFile, ValueError, sqlite3.Error) as error:
        raise InvalidSnapshot("Snapshot failed format, checksum or database validation") from error


def _publish_restore(temporary: Path, destination: Path) -> None:
    destination.mkdir(mode=0o700)
    pending = destination / "library.sqlite.restore-pending"
    owned: list[Path] = []
    try:
        atomic_write(pending, b"Restore in progress; do not open this database.\n", mode=0o600, replace=False)
        owned.append(pending)
        sync_directory(destination)
        sync_directory(destination.parent)
        names = ["library.sqlite", "runtime.json"]
        if (temporary / "sources.key").exists():
            names.append("sources.key")
        from .source_config import source_key_path

        for name in names:
            path = (
                source_key_path(destination / "library.sqlite")
                if name == "sources.key"
                else destination / name
            )
            publish_file(temporary / name, path)
            owned.append(path)
        with closing(sqlite3.connect(destination / "library.sqlite", isolation_level=None)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute(
                "UPDATE download_job SET state='paused',retry_at=0,revision=revision+1 "
                "WHERE state NOT IN ('done','cancelled')"
            )
            db.execute(
                "UPDATE download_job SET "
                "details_json=json_set(details_json,'$.phase','recover'),"
                "retry_at=0 WHERE state='paused' AND (phase='uncertain' OR "
                "json_extract(details_json,'$.submission_uncertain')=1)"
            )
            db.execute(
                "UPDATE removal SET receipt_json=json_set(receipt_json,'$.delete_pending',json('false'),"
                "'$.deletion_stopped','restored_from_backup') "
                "WHERE json_extract(receipt_json,'$.delete_pending')=1"
            )
            db.execute(
                "INSERT INTO source_config VALUES(1,?, '',1) ON CONFLICT(singleton) DO UPDATE SET "
                "settings_json=json_set(settings_json,'$.online_paused',json('true')),revision=revision+1",
                ('{"online_paused":true}',),
            )
            db.execute("UPDATE source_check SET state='queued',generation=generation+1 WHERE state='running'")
            db.execute(
                "UPDATE credential SET revoked_at=? WHERE kind='session' AND revoked_at IS NULL",
                (int(time.time()),),
            )
        sync_directory(destination)
        pending.unlink()
        sync_directory(destination)
    except BaseException:
        for path in [
            *reversed(owned),
            destination / "library.sqlite-wal",
            destination / "library.sqlite-shm",
        ]:
            with suppress(OSError, ValueError):
                path.unlink(missing_ok=True)
        with suppress(OSError):
            destination.rmdir()
        raise


def restore_snapshot(
    snapshot: Path,
    destination: Path,
    *,
    root: Path | None = None,
    max_archive_bytes: int | None = None,
) -> RestoredSnapshot:
    snapshot = snapshot.absolute()
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Restore requires a new destination directory")
    with tempfile.TemporaryDirectory(prefix=".mangacouch-restore-", dir=destination.parent) as temporary:
        directory = Path(temporary)
        profile = _extract_verified(snapshot, directory, root)
        if root is None and not Path(profile.library_root_hint).is_absolute():
            raise ValueError("Snapshot root is not absolute on this platform; provide --root explicitly")
        if max_archive_bytes is not None:
            profile.limits = replace(profile.limits, max_archive_bytes=max_archive_bytes)
        if root is not None:
            root = root.absolute()
            FileLibrary(Library(directory / "library.sqlite"), root, profile.limits).root.check(writable=True)
            profile.library_root_hint = str(root)
        if root is not None or max_archive_bytes is not None:
            atomic_write(
                directory / "runtime.json",
                profile.model_dump_json(by_alias=True, exclude_defaults=True).encode(),
                mode=0o600,
            )
        _publish_restore(directory, destination)
    return RestoredSnapshot(Library(destination / "library.sqlite"), profile, destination)
