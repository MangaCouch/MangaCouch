from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import make_zip, pending_sidecars, save_metadata

from mangacouch import __main__ as cli
from mangacouch import files as file_module
from mangacouch import lifecycle
from mangacouch.archives import Limits
from mangacouch.auth import Auth, Unauthorized
from mangacouch.configuration import HTTPSettings, load_http_settings
from mangacouch.database import Library
from mangacouch.files import ROOT_MARKER, LibraryRoot, RootUnavailable
from mangacouch.ingest import FileLibrary
from mangacouch.lifecycle import initialize_library, load_profile
from mangacouch.models import Metadata, PersonalState
from mangacouch.scanning import Scanner
from mangacouch.sidecars import SidecarSync
from mangacouch.snapshots import MAX_CONFIG_BYTES, create_snapshot, restore_snapshot

PASSCODE = "lifecycle-fixture-passcode"
ENV = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src"), "PYTHONDONTWRITEBYTECODE": "1"}


def run(*arguments, status=0, stdin=None, cwd=None):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "mangacouch", *map(str, arguments)],
        env=ENV,
        input=stdin,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert PASSCODE not in result.stdout + result.stderr
    return json.loads(result.stdout)


def required_profile(database):
    profile = load_profile(database)
    assert profile is not None
    return profile


@pytest.fixture
def initialized(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    library = initialize_library(tmp_path / "library.sqlite", root, Limits(max_archive_bytes=100000))
    return FileLibrary(library, root, required_profile(library.path).limits)


def test_cli_scan_batches_keep_the_unfinished_scans_source_policy(initialized, tmp_path):
    database = initialized.library.path
    (initialized.root.path / "import").mkdir()
    for index in range(3):
        make_zip(initialized.root.path / "import" / f"{index}.cbz", [f"{index}.png", "x.png"])
    first = run("--db", database, "scan", "--keep-source", "--limit", 1)["scan"]
    assert first["state"] != "done" and first["keep_source"] is True
    # Repeating `scan` to continue must not silently switch to consuming the originals.
    again = run("--db", database, "scan", "--limit", 1)["scan"]
    assert again["id"] == first["id"] and again["keep_source"] is True
    flipped = run("--db", database, "scan", "--no-keep-source", "--limit", 1)["scan"]
    assert flipped["id"] == first["id"] and flipped["keep_source"] is False


def test_cli_init_persists_absolute_root_and_limits_across_working_directories(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    result = run("--db", "library.sqlite", "--root", "books", "--max-bytes", 10, "init", cwd=tmp_path)
    database = Path(result["database"])
    assert result["initialized"] and result["next_step"] == "auth-init"
    assert not result["authentication_configured"]
    assert required_profile(database).library_root_hint == str(root)
    source = make_zip(tmp_path / "source.zip")
    assert "byte limit" in run("--db", database, "import", source, status=1)["detail"]
    added = run("--db", database, "--max-bytes", 100000, "import", source)
    assert run("--db", database, "list")["books"][0]["id"] == added["book"]["id"]
    assert required_profile(database).limits.max_archive_bytes == 10
    if os.name != "nt":
        assert database.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("stdin", ["", PASSCODE + "\n", PASSCODE + "\ndifferent-passcode\n"])
def test_cli_auth_rejects_incomplete_or_mismatched_piped_passcodes(initialized, stdin):
    result = run("--db", initialized.library.path, "auth-init", stdin=stdin, status=1)
    assert result["error"] == "invalid_input"
    assert not Auth(initialized.library).configured()


@pytest.mark.parametrize(
    "existing",
    [
        "library.sqlite",
        "runtime.json",
        "library.sqlite.init-pending",
        "library.sqlite.restore-pending",
        "books/" + ROOT_MARKER,
    ],
)
def test_initialization_rejects_existing_state_before_touching_other_paths(tmp_path, existing):
    root = tmp_path / "books"
    root.mkdir()
    path = tmp_path / existing
    path.write_bytes(b"preserve-existing")
    before = {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with pytest.raises(FileExistsError):
        initialize_library(tmp_path / "library.sqlite", root, Limits())
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_initialization_publication_failure_removes_only_owned_files(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()
    original = make_zip(root / "keep.zip").read_bytes()
    link = lifecycle.os.link

    def fail_profile(source, target, **kwargs):
        if Path(target) == tmp_path / "runtime.json":
            raise OSError("fixture disk full")
        link(source, target, **kwargs)

    monkeypatch.setattr(lifecycle.os, "link", fail_profile)
    with pytest.raises(OSError):
        initialize_library(tmp_path / "library.sqlite", root, Limits())
    assert list(root.iterdir()) == [root / "keep.zip"]
    assert (root / "keep.zip").read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["books"]


def test_root_binding_failure_rolls_back_identity_and_removes_owned_marker(tmp_path, monkeypatch):
    root = tmp_path / "books"
    root.mkdir()
    library = Library.initialize(tmp_path / "library.sqlite")

    def fail(*args, **kwargs):
        raise OSError("fixture root permission loss")

    monkeypatch.setattr(file_module.tempfile, "TemporaryFile", fail)
    with pytest.raises(OSError):
        LibraryRoot.bind(library, root)
    assert not (root / ROOT_MARKER).exists()
    with library.connection() as db:
        assert db.execute("SELECT count(*) FROM library_root").fetchone()[0] == 0


@pytest.mark.parametrize("stop_at", [1, 2])
def test_hard_stop_during_initialization_never_opens_as_complete(tmp_path, stop_at):
    root = tmp_path / "books"
    root.mkdir()
    code = """
import os, sys
from pathlib import Path
from mangacouch.lifecycle import initialize_library
from mangacouch.archives import Limits
link = os.link
calls = 0
def stop(source, target, **kwargs):
    global calls
    link(source, target, **kwargs)
    if ".mangacouch-link-check-" in str(source):
        return
    calls += 1
    if calls == int(sys.argv[3]):
        os._exit(75)
os.link = stop
initialize_library(Path(sys.argv[1]), Path(sys.argv[2]), Limits())
"""
    database = tmp_path / "library.sqlite"
    result = subprocess.run(
        [sys.executable, "-B", "-c", code, str(database), str(root), str(stop_at)],
        env=ENV,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 75
    assert database.exists() and (tmp_path / "library.sqlite.init-pending").exists()
    with pytest.raises(ValueError, match="incomplete"):
        Library(database)
    with pytest.raises(FileExistsError):
        initialize_library(database, root, Limits())


@pytest.mark.parametrize("damage", ["oversize", "invalid", "relative", "symlink"])
def test_invalid_runtime_is_not_silently_replaced_by_defaults(initialized, tmp_path, damage):
    path = tmp_path / "runtime.json"
    if damage == "oversize":
        path.write_bytes(b" " * (MAX_CONFIG_BYTES + 1))
    elif damage == "invalid":
        path.write_bytes(b"{broken")
    elif damage == "relative":
        value = json.loads(path.read_text())
        value["library_root_hint"] = "books"
        path.write_text(json.dumps(value))
    else:
        path.rename(tmp_path / "original.json")
        path.symlink_to(tmp_path / "original.json")
    result = run("--db", initialized.library.path, "--root", initialized.root.path, "list", status=1)
    assert result["error"] in {"invalid_input", "ValidationError"}
    assert initialized.library.search("") == []


def test_startup_check_reports_auth_and_does_not_save_overrides_or_start_listener(initialized):
    context = ("--db", initialized.library.path)
    blocked = run(*context, "serve", "--check", status=2)
    assert blocked["mode"] == "blocked" and not blocked["authentication_configured"]
    run(*context, "auth-init", stdin=PASSCODE + "\n" + PASSCODE + "\n")
    ready = run(
        *context,
        "serve",
        "--check",
        "--origin",
        "http://localhost:9898",
        "--port",
        9898,
        "--upload-bytes",
        12345,
    )
    assert ready["startup_ready"] and ready["mode"] == "normal"
    assert ready["effective_max_upload_bytes"] == 12345
    assert load_http_settings(initialized.library) == HTTPSettings()
    with initialized.library.connection() as db:
        assert db.execute("SELECT count(*) FROM http_config").fetchone()[0] == 0


def test_missing_built_asset_blocks_start_before_saving_settings(initialized, tmp_path, monkeypatch, capsys):
    Auth(initialized.library).initialize(PASSCODE)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text('<script src="/assets/missing.js"></script>')
    monkeypatch.setattr(lifecycle, "WEB_DIR", web)
    monkeypatch.setattr(
        sys,
        "argv",
        ["mangacouch", "--db", str(initialized.library.path), "serve", "--origin", "http://localhost:9898"],
    )
    assert cli.main() == 1
    assert "Build web" in capsys.readouterr().out
    assert load_http_settings(initialized.library) == HTTPSettings()


@pytest.mark.parametrize("port", [0, -1, 65536])
def test_invalid_listening_port_rejected_before_configuration_write(initialized, port):
    run("--db", initialized.library.path, "serve", "--port", port, status=1)
    assert load_http_settings(initialized.library) == HTTPSettings()


def test_offline_preflight_keeps_tasks_personal_and_last_known_availability(initialized, tmp_path):
    Auth(initialized.library).initialize(PASSCODE)
    book = initialized.import_file(make_zip(tmp_path / "source.zip")).book
    Scanner(initialized).start()
    initialized.root.path.rename(tmp_path / "offline")
    with initialized.library.connection() as db:
        before = list(db.iterdump())
    report = run("--db", initialized.library.path, "serve", "--check")
    assert report["startup_ready"] and report["mode"] == "diagnostics_only"
    assert not report["diagnostics"]["ready"] and report["diagnostics"]["books"] == 1
    assert report["diagnostics"]["scan"]["state"] == "active"
    with initialized.library.connection() as db:
        assert list(db.iterdump()) == before
    assert initialized.library.get_book(book.id) == book
    (tmp_path / "offline").rename(initialized.root.path)
    assert run("--db", initialized.library.path, "serve", "--check")["mode"] == "normal"


def test_cli_restore_relocated_root_persists_limits_and_restores_reading_without_task_replay(
    initialized,
    tmp_path,
):
    auth = Auth(initialized.library)
    auth.initialize(PASSCODE)
    _, session = auth.login(PASSCODE, "before restore")
    _, key = auth.create_key("readonly", "reader")
    book = initialized.import_file(make_zip(tmp_path / "source.zip")).book
    state = PersonalState(rating=4.5, resume_page=0, bookmarks=[0])
    initialized.library.save_personal(book.id, state, expected_revision=book.personal_revision)
    save_metadata(initialized.library, book.id, Metadata(title="pending title"), expected_revision=1)
    Scanner(initialized).start()
    archive = tmp_path / "snapshot.zip"
    run("--db", initialized.library.path, "snapshot", archive)
    moved = tmp_path / "moved books"
    initialized.root.path.rename(moved)
    output = tmp_path / "restored"
    result = run("--root", moved, "--max-bytes", 54321, "restore", archive, output)
    assert result["restored"] and result["diagnostics"]["ready"]
    restored = Library(output / "library.sqlite")
    profile = required_profile(restored.path)
    assert profile.library_root_hint == str(moved) and profile.limits.max_archive_bytes == 54321
    assert restored.get_book(book.id).personal == state
    assert pending_sidecars(restored) == [(book.id, 2)]
    restored_auth = Auth(restored)
    assert restored_auth.authenticate(key, session=False).kind == "readonly"
    with pytest.raises(Unauthorized):
        restored_auth.authenticate(session, session=True)
    context = ("--db", restored.path)
    assert run(*context, "serve", "--check")["mode"] == "normal"
    assert run(*context, "list")["books"][0]["title"] == "pending title"
    run(*context, "page", book.id, 0, tmp_path / "restored-page.png")
    assert (tmp_path / "restored-page.png").read_bytes().startswith(b"\x89PNG")
    scan = Scanner(FileLibrary(restored, moved)).latest()
    assert scan is not None and scan["state"] in {"active", "done"}
    assert SidecarSync(FileLibrary(restored, moved)).sync_one(book.id)["state"] == "synced"


def test_restore_wrong_root_and_invalid_override_do_not_publish_or_touch_original(initialized, tmp_path):
    archive = create_snapshot(initialized, tmp_path / "snapshot.zip")
    original = archive.read_bytes()
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    with pytest.raises(RootUnavailable):
        restore_snapshot(archive, tmp_path / "wrong-result", root=wrong)
    with pytest.raises(ValueError):
        restore_snapshot(archive, tmp_path / "bad-limit", max_archive_bytes=0)
    run("--db", initialized.library.path, "restore", archive, tmp_path / "ignored-db", status=1)
    assert not any((tmp_path / name).exists() for name in ("wrong-result", "bad-limit", "ignored-db"))
    assert archive.read_bytes() == original
    assert not list(wrong.iterdir())


def test_serve_dispatch_uses_saved_runtime_and_advances_scan(
    initialized,
    monkeypatch,
    capsys,
):
    import uvicorn

    Auth(initialized.library).initialize(PASSCODE)
    Scanner(initialized).start()
    called = []

    def serve(app, **options):
        called.append(options)
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login",
                json={"passcode": PASSCODE},
                headers={"Origin": "http://testserver", "X-MangaCouch-Request": "1"},
            )
            assert response.status_code == 200
            assert client.get("/api/books").status_code == 200

    monkeypatch.setattr(uvicorn, "run", serve)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "mangacouch",
            "--db",
            str(initialized.library.path),
            "serve",
            "--origin",
            "http://testserver",
            "--port",
            "9898",
        ],
    )
    assert cli.main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["startup_ready"]
    assert called[0]["workers"] == 1 and called[0]["port"] == 9898
    assert not called[0]["proxy_headers"] and not called[0]["access_log"]
    assert load_http_settings(initialized.library).public_origin == "http://testserver"
    scan = Scanner(initialized).latest()
    assert scan is not None and scan["state"] in {"active", "done"}


def test_concurrent_initialization_keeps_winner_and_does_not_erase_root(tmp_path):
    root = tmp_path / "books"
    root.mkdir()
    database = tmp_path / "library.sqlite"

    def initialize():
        try:
            return initialize_library(database, root, Limits())
        except FileExistsError:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: initialize(), range(2)))
    assert sum(item is not None for item in results) == 1
    LibraryRoot(Library(database), root).check(writable=True)
    assert load_profile(database) is not None
    assert not (tmp_path / "library.sqlite.init-pending").exists()


def test_snapshot_nonabsolute_root_requires_explicit_verified_override(initialized, tmp_path):
    archive = create_snapshot(initialized, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(archive) as source:
        entries = {name: source.read(name) for name in source.namelist()}
    profile = json.loads(entries["runtime.json"])
    profile["library_root_hint"] = "relative/books"
    entries["runtime.json"] = json.dumps(profile).encode()
    manifest = json.loads(entries["manifest.json"])
    manifest["files"]["runtime.json"] = {
        "size": len(entries["runtime.json"]),
        "sha256": hashlib.sha256(entries["runtime.json"]).hexdigest(),
    }
    entries["manifest.json"] = json.dumps(manifest).encode()
    altered = tmp_path / "relative.zip"
    with zipfile.ZipFile(altered, "w") as output:
        for name, data in entries.items():
            output.writestr(name, data)
    with pytest.raises(ValueError, match="provide --root"):
        restore_snapshot(altered, tmp_path / "rejected")
    assert not (tmp_path / "rejected").exists()
    restored = restore_snapshot(altered, tmp_path / "accepted", root=initialized.root.path)
    assert restored.profile.library_root_hint == str(initialized.root.path)
