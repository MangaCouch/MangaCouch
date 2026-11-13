from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest
from helpers import CRASH_AFTER, make_zip, python, save_metadata

from mangacouch import files as files_module
from mangacouch import files as storage
from mangacouch.archives import Limits
from mangacouch.files import FileLock, RootUnavailable, atomic_write, publish_file
from mangacouch.ingest import FileLibrary
from mangacouch.lifecycle import initialize_library
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import create_snapshot, restore_snapshot
from mangacouch.source_config import SourceSettings, SourceVault


@pytest.fixture
def no_links(monkeypatch):
    def unsupported(*args, **kwargs):
        raise OSError(errno.ENOTSUP, "No hard links on this filesystem")

    monkeypatch.setattr(os, "link", unsupported)


def test_complete_library_workflow_without_hardlinks(tmp_path, no_links):
    root = tmp_path / "books"
    root.mkdir()
    library = initialize_library(tmp_path / "library.sqlite", root, Limits())
    files = FileLibrary(library, root)
    source = make_zip(tmp_path / "source.zip")
    original = source.read_bytes()
    book = files.import_file(source).book
    assert source.read_bytes() == original
    assert files.root.resolve(book.content.relative_path).read_bytes() == original
    sync = SidecarSync(files)
    assert sync.sync_one(book.id)["state"] == "synced"
    save_metadata(
        library, book.id, book.metadata.model_copy(update={"title": "Updated"}), expected_revision=1
    )
    assert sync.sync_one(book.id)["state"] == "synced"
    vault = SourceVault(library)
    encrypted = vault.encrypt({"cookie": "test-only"})
    assert vault.decrypt(encrypted) == {"cookie": "test-only"}
    vault.save(SourceSettings(), {"eh_cookie": "test-only"}, 0)
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=root)
    assert SourceVault(restored.library).decrypt(encrypted) == {"cookie": "test-only"}
    assert FileLibrary(restored.library, root).page_bytes(book.id, 0) == files.page_bytes(book.id, 0)


@pytest.mark.parametrize("code", [errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP, errno.ENOSYS])
def test_link_failures_fall_back_to_verified_copy(tmp_path, monkeypatch, code):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(os.urandom(2 * 1024**2 + 7))

    def fail(*args, **kwargs):
        raise OSError(code, "link unavailable")

    monkeypatch.setattr(os, "link", fail)
    publish_file(source, target)
    assert source.read_bytes() == target.read_bytes()
    assert not source.samefile(target)
    assert not list(tmp_path.glob(".mangacouch-publish-*"))


@pytest.mark.parametrize("existing", [False, True])
def test_copy_publication_never_overwrites_a_destination(tmp_path, monkeypatch, no_links, existing):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    if existing:
        target.write_bytes(b"keep")
    else:
        rename = storage._rename_new

        def race(staged, destination, **kwargs):
            assert not destination.exists()
            destination.write_bytes(b"keep")
            return rename(staged, destination, **kwargs)

        monkeypatch.setattr(storage, "_rename_new", race)
    with pytest.raises(FileExistsError):
        publish_file(source, target)
    assert source.read_bytes() == b"source" and target.read_bytes() == b"keep"
    assert not list(tmp_path.glob(".mangacouch-publish-*"))


def test_copy_failure_leaves_no_final_or_partial_file(tmp_path, monkeypatch, no_links):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")

    def full(*args):
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "fsync", full)
    with pytest.raises(OSError, match="disk full"):
        publish_file(source, target)
    assert source.read_bytes() == b"source" and not target.exists()
    assert not list(tmp_path.glob(".mangacouch-publish-*"))


@pytest.mark.parametrize("after_publish", [False, True])
def test_interrupted_copy_import_fails_without_exposing_partial_book(files, tmp_path, after_publish):
    source = make_zip(tmp_path / "source.zip")
    script = (
        CRASH_AFTER
        + """
import errno, sys
from pathlib import Path
from mangacouch import ingest as storage
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary

def no_link(*args, **kwargs):
    raise OSError(errno.ENOTSUP, "unsupported")
os.link = no_link
crash_after(storage, "_rename_new", 73, run_original=sys.argv[4] == "1")
FileLibrary(Library(Path(sys.argv[1])), Path(sys.argv[2])).import_file(Path(sys.argv[3]))
"""
    )
    result = python("-c", script, files.library.path, files.root.path, source, int(after_publish))
    assert result.returncode == 73, result.stderr
    assert files.library.search("") == []
    targets = list(files.root.path.glob("*/*.cbz"))
    assert len(targets) == int(after_publish)
    for target in targets:
        assert target.read_bytes() == source.read_bytes()
    assert list(files.recover().values()) == ["interrupted; retry source import"]
    assert files.library.search("") == []
    assert not list(files.root.path.glob("*/*.cbz"))
    assert source.exists()


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
@pytest.mark.parametrize("bits", [0o644, 0o640, 0o600])
def test_copy_publication_keeps_the_source_permissions(tmp_path, no_links, bits):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    source.chmod(bits)
    publish_file(source, target)
    assert mode(target) == bits


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_archives_and_sidecars_follow_the_umask_and_rewrites_keep_permissions(tmp_path, no_links, umask_022):
    root = tmp_path / "books"
    root.mkdir()
    library = initialize_library(tmp_path / "library.sqlite", root, Limits())
    files = FileLibrary(library, root)
    book = files.import_file(make_zip(tmp_path / "source.zip")).book
    assert mode(files.root.resolve(book.content.relative_path)) == 0o644
    sync = SidecarSync(files)
    assert sync.sync_one(book.id)["state"] == "synced"
    sidecar = sync.path(book)
    assert mode(sidecar) == 0o644
    sidecar.chmod(0o640)
    save_metadata(
        library, book.id, book.metadata.model_copy(update={"title": "Updated"}), expected_revision=1
    )
    assert sync.sync_one(book.id)["state"] == "synced"
    assert mode(sidecar) == 0o640 and b"Updated" in sidecar.read_bytes()
    assert not list(root.rglob("*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_database_key_and_snapshots_stay_private_without_hardlinks(tmp_path, no_links, umask_022):
    root = tmp_path / "books"
    root.mkdir()
    library = initialize_library(tmp_path / "library.sqlite", root, Limits())
    assert mode(library.path) == 0o600
    files = FileLibrary(library, root)
    files.import_file(make_zip(tmp_path / "source.zip"))
    SourceVault(library).save(SourceSettings(), {"eh_cookie": "test-only"}, 0)
    assert mode(tmp_path / "library.sqlite.sources.key") == 0o600
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    assert mode(snapshot) == 0o600
    restored = restore_snapshot(snapshot, tmp_path / "restored", root=root)
    assert {path.name: mode(path) for path in restored.directory.iterdir() if path.is_file()} == {
        "library.sqlite": 0o600,
        "library.sqlite.sources.key": 0o600,
        "runtime.json": 0o600,
    }


def test_root_check_is_cached_briefly_but_notices_a_missing_root(files: FileLibrary, tmp_path, monkeypatch):
    calls = []
    original = files_module.check_root
    monkeypatch.setattr(
        files_module, "check_root", lambda *a, **kw: calls.append(kw.get("writable")) or original(*a, **kw)
    )
    files.root.check()
    files.root.check()
    files.root.check()
    assert calls == [False]
    files.root.check(writable=True)
    assert calls == [False, True]
    files.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        files.root.check()
    with pytest.raises(RootUnavailable):
        files.root.check()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_atomic_write_follows_the_umask_unless_a_mode_is_given(tmp_path: Path):
    previous = os.umask(0o027)
    try:
        atomic_write(tmp_path / "shared.json", b"{}")
        atomic_write(tmp_path / "secret.key", b"key", mode=0o600)
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "shared.json").stat().st_mode) == 0o640
    assert stat.S_IMODE((tmp_path / "secret.key").stat().st_mode) == 0o600
    atomic_write(tmp_path / "shared.json", b"[]")
    assert (tmp_path / "shared.json").read_bytes() == b"[]"
    with pytest.raises(FileExistsError):
        atomic_write(tmp_path / "secret.key", b"other", replace=False)
    assert (tmp_path / "secret.key").read_bytes() == b"key"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["secret.key", "shared.json"]


def test_file_lock_excludes_other_handles_until_closed(tmp_path: Path):
    path = tmp_path / "work.lock"
    with FileLock(path), pytest.raises(BlockingIOError):
        FileLock(path, blocking=False)
    FileLock(path, blocking=False).close()


posix_only = pytest.mark.skipif(os.name == "nt", reason="Windows renames never replace")


@posix_only
@pytest.mark.parametrize("hard_links", [True, False])
def test_publishing_works_without_a_no_replace_rename(monkeypatch, tmp_path: Path, hard_links: bool):
    """virtiofs, NFS and SMB refuse renameat2/renamex_np flags: a hard link (or, without links, a
    checked rename) still publishes without ever replacing an existing file."""
    monkeypatch.setattr(storage, "_rename_exclusive", lambda source, target: errno.EINVAL)
    if not hard_links:

        def no_links(*_args, **_kwargs):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(storage.os, "link", no_links)
    atomic_write(tmp_path / "new.json", b"first", replace=False)
    assert (tmp_path / "new.json").read_bytes() == b"first"
    with pytest.raises(FileExistsError):
        atomic_write(tmp_path / "new.json", b"second", replace=False)
    assert (tmp_path / "new.json").read_bytes() == b"first"
    source = tmp_path / "staged.cbz"
    source.write_bytes(b"archive")
    publish_file(source, tmp_path / "published.cbz")  # keeps its source
    assert (tmp_path / "published.cbz").read_bytes() == b"archive"
    other = tmp_path / "other.cbz"
    other.write_bytes(b"other")
    with pytest.raises(FileExistsError):
        publish_file(other, tmp_path / "published.cbz")
    assert (tmp_path / "published.cbz").read_bytes() == b"archive"
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "new.json",
        "other.cbz",
        "published.cbz",
        "staged.cbz",
    ]


@posix_only
def test_a_library_initializes_where_renames_cannot_refuse_replacement(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(storage, "_rename_exclusive", lambda source, target: errno.ENOTSUP)
    (tmp_path / "state").mkdir()
    (tmp_path / "books").mkdir()
    library = initialize_library(tmp_path / "state" / "library.sqlite", tmp_path / "books", Limits())
    assert library.path.exists() and not (tmp_path / "state" / "library.sqlite.init-pending").exists()


@posix_only
def test_other_rename_failures_are_not_mistaken_for_missing_support(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(storage, "_rename_exclusive", lambda source, target: errno.EACCES)
    with pytest.raises(PermissionError):
        atomic_write(tmp_path / "file", b"data", replace=False)
    assert list(tmp_path.iterdir()) == []


@posix_only
def test_directories_that_cannot_be_synced_do_not_stop_writes(monkeypatch, tmp_path: Path):
    fsync = os.fsync

    def no_directory_sync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "Invalid argument")
        fsync(fd)

    monkeypatch.setattr(storage.os, "fsync", no_directory_sync)
    atomic_write(tmp_path / "file", b"data", replace=False)
    assert (tmp_path / "file").read_bytes() == b"data"
    monkeypatch.setattr(
        storage.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(errno.EIO, "I/O error"))
    )
    with pytest.raises(OSError, match="I/O error"):
        storage.sync_directory(tmp_path)


def _without_exclusive_rename_or_links(monkeypatch):
    monkeypatch.setattr(storage, "_rename_exclusive", lambda source, target: errno.EINVAL)

    def no_links(*_args, **_kwargs):
        raise OSError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(storage.os, "link", no_links)


@posix_only
def test_a_file_created_by_another_program_first_is_never_replaced(monkeypatch, tmp_path: Path):
    _without_exclusive_rename_or_links(monkeypatch)
    target = tmp_path / "book.cbz"
    real_open = os.open

    def racing_open(path, flags, *args):
        if Path(path) == target and flags & os.O_EXCL:
            target.write_bytes(b"theirs")  # created just before our claim
        return real_open(path, flags, *args)

    monkeypatch.setattr(storage.os, "open", racing_open)
    source = tmp_path / "staged"
    source.write_bytes(b"ours")
    with pytest.raises(FileExistsError):
        storage._rename_new(source, target)
    assert target.read_bytes() == b"theirs" and source.read_bytes() == b"ours"


@posix_only
def test_replaced_claim_cannot_overwrite_another_program_file(monkeypatch, tmp_path: Path):
    _without_exclusive_rename_or_links(monkeypatch)
    source, target = tmp_path / "staged", tmp_path / "book.cbz"
    source.write_bytes(b"ours")

    def replace_claim(path):
        path.unlink()
        path.write_bytes(b"theirs")

    with pytest.raises(FileExistsError):
        storage._rename_new(source, target, claimed=replace_claim)
    assert target.read_bytes() == b"theirs" and source.read_bytes() == b"ours"


@posix_only
def test_fallback_publication_does_not_use_replace(monkeypatch, tmp_path: Path):
    _without_exclusive_rename_or_links(monkeypatch)
    monkeypatch.setattr(storage.os, "replace", lambda *a: pytest.fail("must never replace a claimed path"))
    source, target = tmp_path / "staged", tmp_path / "book.cbz"
    source.write_bytes(b"ours")
    storage._rename_new(source, target)
    assert not source.exists() and target.read_bytes() == b"ours"
