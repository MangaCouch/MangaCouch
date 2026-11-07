from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from helpers import finish_scan as finish
from helpers import inbox, make_zip, manager_client, native_file

from mangacouch.eze import eze_path
from mangacouch.eze_cache import EzeCache
from mangacouch.files import ROOT_MARKER, hash_file
from mangacouch.ingest import FileLibrary
from mangacouch.models import PersonalState
from mangacouch.scanning import Scanner
from mangacouch.snapshots import create_snapshot, restore_snapshot


def test_cleanup_permission_failure_never_links_retained_source_to_library(files, monkeypatch):
    source = make_zip(inbox(files) / "kept.cbz")
    expected = source.read_bytes()
    unlink = Path.unlink

    def deny_source(path, *args, **kwargs):
        if path == source:
            raise PermissionError("Source directory is read-only")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_source)
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["counts"]["indexed"] == 1
    book = files.library.get_book(files.library.search("")[0])
    target = files.root.resolve(book.content.relative_path)
    assert not source.samefile(target)
    source.write_bytes(b"edited retained source")
    assert target.read_bytes() == expected


def test_scan_copies_verifies_then_consumes_source(files):
    path = make_zip(inbox(files) / "incoming.cbz")
    digest = hash_file(path)
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    book = files.library.get_book(files.library.search("")[0])
    assert result["counts"]["indexed"] == 1 and not path.exists()
    assert book.content.relative_path == book.content.managed_path
    assert hash_file(files.root.resolve(book.content.relative_path)) == digest
    assert files.root.resolve(book.content.relative_path + ".mangacouch.json").is_file()


def test_keep_source_and_repeat_scan_preserve_personal_state(files, monkeypatch):
    path = make_zip(inbox(files) / "kept.cbz")
    scanner = Scanner(files)
    finish(scanner, scanner.start(keep_source=True)["id"])
    book = files.library.get_book(files.library.search("")[0])
    state = PersonalState(rating=4.5, completed=True, resume_page=0)
    files.library.save_personal(book.id, state, expected_revision=1)
    monkeypatch.setattr(files, "import_file", lambda *a, **kw: pytest.fail("Unchanged source reimported"))
    result = finish(scanner, scanner.start(keep_source=True)["id"])
    assert result["counts"]["unchanged"] == 1 and path.exists()
    assert files.library.get_book(book.id).personal == state
    finish(scanner, scanner.start()["id"])
    assert not path.exists()


def test_duplicate_content_is_consumed_without_duplicate_record(files):
    folder = inbox(files)
    path = make_zip(folder / "a.cbz")
    (folder / "b.cbz").write_bytes(path.read_bytes())
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["counts"]["indexed"] == 1 and result["counts"]["duplicate"] == 1
    assert len(files.library.search("")) == 1
    assert not list(folder.glob("*.cbz"))


def test_eze_and_native_are_saved_before_sources_removed(files):
    path = make_zip(inbox(files) / "paired.cbz")
    eze_path(path).write_text(
        json.dumps(
            {
                "gallery_info": {
                    "title": "Original",
                    "source": {"site": "ehentai", "gid": 123, "token": "abc123"},
                }
            }
        )
    )
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["counts"]["indexed"] == 1
    book = files.library.get_book(files.library.search("")[0])
    target = files.root.resolve(book.content.relative_path)
    assert eze_path(target).exists() and not eze_path(path).exists() and not path.exists()
    assert book.metadata.title == "Original"


def test_broken_archive_and_invalid_sidecar_remain_in_inbox(files):
    folder = inbox(files)
    (folder / "broken.cbz").write_bytes(b"not a zip")
    path = make_zip(folder / "valid.cbz")
    eze_path(path).write_text("invalid")
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["counts"]["error"] == 1 and result["counts"]["indexed"] == 1
    assert (folder / "broken.cbz").exists() and path.exists() and eze_path(path).exists()
    finish(scanner, scanner.start()["id"])
    assert path.exists() and eze_path(path).exists()


def test_changed_sidecar_is_reimported_instead_of_consuming_stale_receipt(files, monkeypatch):
    path = make_zip(inbox(files) / "kept.cbz")
    eze_path(path).write_text('{"gallery_info":{"title":"First"}}')
    scanner = Scanner(files)
    finish(scanner, scanner.start(keep_source=True)["id"])
    eze_path(path).write_text("invalid replacement")
    original = files.import_file
    calls = []

    def imported(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(files, "import_file", imported)
    finish(scanner, scanner.start()["id"])
    assert calls == [path] and path.exists() and eze_path(path).exists()


def test_existing_library_paths_and_unrelated_json_are_untouched(files):
    path = make_zip(files.root.path / "existing.cbz")
    book = files.import_file(path, index_existing=True).book
    folder = inbox(files)
    (folder / "unrelated.json").write_text("{}")
    (folder / "link.cbz").symlink_to(path)
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["archives"] == 0 and path.exists()
    assert files.library.get_book(book.id).content.relative_path == "existing.cbz"
    assert (folder / "unrelated.json").read_text() == "{}"


def test_changed_source_during_cleanup_is_retained(files, monkeypatch):
    path = make_zip(inbox(files) / "changing.cbz")
    original = files.import_file

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        path.write_bytes(b"new contents")
        return result

    monkeypatch.setattr(files, "import_file", changed)
    scanner = Scanner(files)
    result = finish(scanner, scanner.start()["id"])
    assert result["items"][0]["error_code"] == "content_changed" and path.exists()
    assert result["items"][0]["error_message"]
    assert len(files.library.search("")) == 1


def test_parallel_starts_and_scan_lock(files):
    scanner = Scanner(files)
    with ThreadPoolExecutor() as pool:
        ids = list(pool.map(lambda _: scanner.start()["id"], range(3)))
    assert len(set(ids)) == 1
    with files.root.mutation(lock_name=".mangacouch-scan.lock", blocking=False):
        assert scanner.step(ids[0])["state"] == "active"
    assert finish(scanner, ids[0])["state"] == "done"


def test_pause_during_copy_and_resume(files, monkeypatch):
    path = make_zip(inbox(files) / "paused.cbz")
    scanner = Scanner(files)
    identifier = scanner.start()["id"]
    scanner.step(identifier)
    original = files.import_file
    entered, release = threading.Event(), threading.Event()

    def slow(*a, **kw):
        entered.set()
        assert release.wait(3)
        return original(*a, **kw)

    monkeypatch.setattr(files, "import_file", slow)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(scanner.step, identifier)
        assert entered.wait(3)
        scanner.pause(identifier)
        release.set()
        assert future.result(timeout=3)["state"] == "paused"
    assert path.exists() and files.library.search("") == []
    monkeypatch.setattr(files, "import_file", original)
    scanner.resume(identifier)
    assert finish(scanner, identifier)["counts"]["indexed"] == 1


def test_root_loss_pauses_and_recovers(files):
    path = make_zip(inbox(files) / "paused.cbz")
    scanner = Scanner(files)
    identifier = scanner.start()["id"]
    marker = files.root.path / ROOT_MARKER
    data = marker.read_bytes()
    marker.unlink()
    assert scanner.step(identifier)["state"] == "paused"
    assert path.exists()
    marker.write_bytes(data)
    scanner.resume(identifier)
    assert finish(scanner, identifier)["counts"]["indexed"] == 1


def test_scan_receipts_and_keep_policy_survive_backup(files, tmp_path):
    folder = inbox(files)
    for i in range(55):
        (folder / f"{i:03}.cbz").write_bytes(b"broken")
    scanner = Scanner(files)
    identifier = scanner.start(keep_source=True)["id"]
    scanner.step(identifier, limit=3)
    scanner.pause(identifier)
    restored = restore_snapshot(create_snapshot(files, tmp_path / "scan.zip"), tmp_path / "restored")
    service = Scanner(FileLibrary(restored.library, files.root.path))
    assert service.get(identifier)["keep_source"]
    service.resume(identifier)
    assert finish(service, identifier)["counts"]["error"] == 55
    first = service.get(identifier)
    assert len(first["items"]) == 50 and len(service.get(identifier, after=first["after"])["items"]) == 5


def test_scan_retains_eze_with_field_warnings(files):
    scanner = Scanner(files)
    run = scanner.start()
    source = make_zip(files.root.path / "import" / "a.cbz")
    sidecar = source.with_suffix(".eze.json")
    sidecar.write_text(json.dumps({"title": "Example", "category": 123, "tags": {"female": [42]}}))
    for _ in range(3):
        run = scanner.step(run["id"], limit=10)
    assert run["state"] == "done" and set(run) == {"id", "state", "error_code", "archives", "processed"}
    assert source.exists() and sidecar.exists()
    assert any(item["warnings"] for item in scanner.get(run["id"])["items"])


def test_http_scan_lifespan_advances_without_manual_step(files):
    import time

    incoming = files.root.path / "import"
    incoming.mkdir(exist_ok=True)
    source = make_zip(incoming / "scan.cbz")
    with manager_client(files) as client:
        response = client.post("/api/scans?keep_source=true")
        assert response.status_code == 200
        identifier = response.json()["id"]
        deadline = time.monotonic() + 10
        result = client.get(f"/api/scans/{identifier}")
        while time.monotonic() < deadline:
            result = client.get(f"/api/scans/{identifier}")
            assert result.status_code == 200
            if result.json()["state"] == "done":
                break
            time.sleep(0.05)
        assert result.json()["state"] == "done"
        assert client.get("/api/scans").json()["scan"]["id"] == identifier
        assert len(files.library.search("")) == 1
        assert source.exists()


def test_explicit_eze_retains_valid_metadata_and_generic_json_is_ignored(files, tmp_path):
    archive = make_zip(tmp_path / "book.cbz")
    generic = archive.with_suffix(".json")
    generic.write_text('{"gallery_info":{"title":"Not Eze"}}')
    assert files._local_eze(archive) == (None, [])
    payload = {
        "gallery_info": {"title": "Eze"},
        "gallery_info_full": {"title_original": "Original"},
        "extension": {"keep": True},
    }
    eze_path(archive).write_text(json.dumps(payload))
    result = files.import_file(archive)
    EzeCache(files).sync_one(result.book.id)
    cached = json.loads(eze_path(files.root.resolve(result.book.content.relative_path)).read_bytes())
    assert cached["gallery_info"]["title"] == "Eze"
    assert cached["gallery_info"]["title_original"] == "Original"
    assert "extension" not in cached
    assert generic.exists()


def test_native_pair_overrides_import_metadata_without_contaminating_eze(files):
    archive = make_zip(inbox(files) / "paired.cbz")
    native_file(archive)
    payload = {
        "gallery_info": {"title": "From Eze", "source": {"site": "ehentai", "gid": 123, "token": "abc123"}}
    }
    eze_path(archive).write_text(json.dumps(payload))
    scan = Scanner(files)
    finish(scan, scan.start()["id"])
    book = files.library.get_book(files.library.search("")[0])
    assert book.metadata.title == "本地标题"
    cached = json.loads(eze_path(files.root.resolve(book.content.relative_path)).read_bytes())
    assert cached["gallery_info"]["title"] == "From Eze"
    assert cached["gallery_info"]["source"]["gid"] == 123


def test_new_or_ambiguous_sidecars_prevent_source_cleanup(files, monkeypatch):
    archive = make_zip(inbox(files) / "incoming.cbz")
    original = files.import_file

    def import_file(*args, **kwargs):
        result = original(*args, **kwargs)
        eze_path(archive).write_text('{"gallery_info":{"title":"Just arrived"}}')
        return result

    monkeypatch.setattr(files, "import_file", import_file)
    scan = Scanner(files)
    result = finish(scan, scan.start()["id"])
    assert result["counts"]["error"] == 1 and archive.exists() and eze_path(archive).exists()
    archive.with_suffix(".eze.json").write_text('{"gallery_info":{"title":"Another"}}')
    assert files._local_eze(archive) == (None, ["multiple_eze_sidecars"])
