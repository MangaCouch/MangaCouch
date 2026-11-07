from __future__ import annotations

import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import make_zip, save_metadata

from mangacouch import files as storage
from mangacouch.archives import Limits
from mangacouch.files import publish_file
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
    vault.save(SourceSettings(), {"nh_key": "test-only"}, 0)
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

        def race(staged, destination):
            assert not destination.exists()
            destination.write_bytes(b"keep")
            return rename(staged, destination)

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
def test_interrupted_copy_import_resumes_without_exposing_partial_book(files, tmp_path, after_publish):
    source = make_zip(tmp_path / "source.zip")
    script = """
import errno, os, sys
from pathlib import Path
from mangacouch import files as storage
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary

def no_link(*args, **kwargs):
    raise OSError(errno.ENOTSUP, "unsupported")
os.link = no_link
rename = storage._rename_new
def stop(source, target):
    if sys.argv[4] == "1":
        rename(source, target)
    os._exit(73)
storage._rename_new = stop
FileLibrary(Library(Path(sys.argv[1])), Path(sys.argv[2])).import_file(Path(sys.argv[3]))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(files.library.path),
            str(files.root.path),
            str(source),
            str(int(after_publish)),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 73, result.stderr
    assert files.library.search("") == []
    targets = list(files.root.path.glob("*/*.cbz"))
    assert len(targets) == int(after_publish)
    for target in targets:
        assert target.read_bytes() == source.read_bytes()
    assert list(files.recover().values()) == ["recovered"]
    book = files.library.get_book(files.library.search("")[0])
    assert files.root.resolve(book.content.relative_path).read_bytes() == source.read_bytes()
