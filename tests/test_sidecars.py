from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from helpers import (
    CRASH_AFTER,
    content,
    edit,
    external,
    import_book,
    login,
    make_zip,
    pending_sidecars,
    python,
    ready,
    register_book,
    synced,
)
from helpers import finish_download as finish
from pydantic import ValidationError

from mangacouch.database import Library, RevisionConflict
from mangacouch.errors import InvalidInput
from mangacouch.eze import eze_path
from mangacouch.eze_cache import EzeCache
from mangacouch.files import RootUnavailable
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, NativeSidecar, PersonalState, read_native
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync


def test_sync_coalesces_edits_and_does_not_export_personal_state(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "first")
    edit(sync, book_id, "latest")
    personal = PersonalState(rating=4.5, completed=True, resume_page=0)
    sync.library.save_personal(book_id, personal, expected_revision=1)
    assert synced(sync.batch())[book_id]["state"] == "synced"
    path = sync.path(sync.library.get_book(book_id))
    data = path.read_bytes()
    value = json.loads(data)
    assert value["metadata"]["title"] == "latest" and value["revision"] == 3
    assert set(value) == {"schema", "content_sha256", "revision", "metadata"}
    assert book_id.encode() not in data and b'"completed"' not in data
    assert pending_sidecars(sync.library) == []
    before = path.stat().st_mtime_ns
    assert sync.sync_one(book_id)["state"] == "unchanged"
    assert path.stat().st_mtime_ns == before and path.read_bytes() == data
    assert sync.library.get_book(book_id).personal == personal


def test_format_only_external_change_is_observed_without_rewrite(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    data = json.dumps(json.loads(path.read_bytes()), indent=4).encode()
    path.write_bytes(data)
    before = path.stat().st_mtime_ns
    assert sync.sync_one(book_id)["state"] == "unchanged"
    assert path.read_bytes() == data and path.stat().st_mtime_ns == before and sync.conflicts() == []
    assert sync._state(book_id)["observed_hash"] == hashlib.sha256(data).hexdigest()


def test_new_db_edit_during_publish_remains_pending(sync: SidecarSync, tmp_path: Path, monkeypatch):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "published")
    publish = sync._publish

    def edit_while_publishing(target, data):
        publish(target, data)
        edit(sync, book_id, "newer DB edit")

    with monkeypatch.context() as patch:
        patch.setattr(sync, "_publish", edit_while_publishing)
        assert sync.sync_one(book_id) == {"state": "pending", "revision": 2}
    assert pending_sidecars(sync.library) == [(book_id, 3)]
    assert sync.sync_one(book_id) == {"state": "synced", "revision": 3}
    assert (
        json.loads(sync.path(sync.library.get_book(book_id)).read_bytes())["metadata"]["title"]
        == "newer DB edit"
    )


@pytest.mark.parametrize("choice", ["library", "file", "fields"])
def test_external_edits_require_explicit_resolution(sync: SidecarSync, tmp_path: Path, choice: str):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "baseline")
    sync.sync_one(book_id)
    edit(sync, book_id, "local")
    original = external(sync, book_id, "external")
    result = sync.sync_one(book_id)
    assert result["state"] == "conflict" and len(sync.conflicts()) == 1
    assert sync.sync_one(book_id) == result
    conflict = sync.conflicts()[0]
    assert conflict["fields"] == ["title", "language"]
    assert conflict["library"]["metadata"]["title"] == "local"
    assert conflict["file"]["metadata"]["title"] == "external"
    assert sync.path(sync.library.get_book(book_id)).read_bytes() == original
    assert sync.library.get_book(book_id).metadata.title == "local"
    if choice == "fields":
        book = sync.resolve(result["conflict_id"], fields={"title": "file", "language": "library"})
        assert book.metadata.title == "external" and book.metadata.language == "en"
        assert book.metadata.protected_fields == ["language", "title"]
        assert book.metadata.field_sources == {"title": "manual", "language": "manual"}
    else:
        book = sync.resolve(result["conflict_id"], choice=choice)  # type: ignore[arg-type]
        assert book.metadata.title == {"library": "local", "file": "external"}[choice]
    assert sync.path(book).read_bytes() == original
    assert sync.conflicts() == [] and pending_sidecars(sync.library)
    assert sync.sync_one(book_id)["state"] in {"synced", "unchanged"}
    assert pending_sidecars(sync.library) == []
    written = json.loads(sync.path(book).read_bytes())
    assert written["metadata"]["title"] == book.metadata.title
    assert sync.library.search(book.metadata.title or "") == [book_id]


@pytest.mark.parametrize("change", ["db", "external"])
def test_conflict_preview_cannot_be_applied_after_a_new_change(
    sync: SidecarSync, tmp_path: Path, change: str
):
    book_id = import_book(sync.files, tmp_path).id
    sync.sync_one(book_id)
    external(sync, book_id, "external")
    conflict_id = sync.sync_one(book_id)["conflict_id"]
    if change == "db":
        edit(sync, book_id, "new local")
    else:
        external(sync, book_id, "new external")
    original = sync.path(sync.library.get_book(book_id)).read_bytes()
    with pytest.raises(RevisionConflict):
        sync.resolve(conflict_id, choice="file")
    assert sync.path(sync.library.get_book(book_id)).read_bytes() == original
    assert sync.sync_one(book_id)["conflict_id"] != conflict_id


@pytest.mark.parametrize("damage", ["truncated", "schema", "binding", "private"])
def test_invalid_sidecar_is_preserved_and_not_imported(sync: SidecarSync, tmp_path: Path, damage: str):
    book_id = import_book(sync.files, tmp_path).id
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    value = json.loads(path.read_bytes())
    if damage == "schema":
        value["schema"] = "unknown/9"
    elif damage == "binding":
        value["content_sha256"] = "0" * 64
    elif damage == "private":
        value["metadata"]["archiver_key"] = "DO_NOT_SAVE"
    data = b'{"broken"' if damage == "truncated" else json.dumps(value).encode()
    path.write_bytes(data)
    assert sync.sync_one(book_id) == {"state": "error", "error": "sidecar_invalid"}
    assert path.read_bytes() == data and sync.library.get_book(book_id).metadata == Metadata()
    with sync.library.connection() as db:
        assert "DO_NOT_SAVE" not in "\n".join(db.iterdump())


def test_deleted_sidecar_is_rewritten_from_the_library(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "library title")
    sync.sync_one(book_id)
    path = sync.path(sync.library.get_book(book_id))
    path.unlink()
    assert sync.sync_one(book_id)["state"] == "synced"
    assert json.loads(path.read_bytes())["metadata"]["title"] == "library title"
    assert sync.conflicts() == []


def test_eze_timestamp_and_source_fields_survive_sync_and_field_selection(sync: SidecarSync, tmp_path: Path):
    source = make_zip(tmp_path / "eze.zip")
    source.with_suffix(".eze.json").write_bytes((Path(__file__).parent / "fixtures/eze.json").read_bytes())
    book = sync.files.import_file(source).book
    assert sync.sync_one(book.id)["state"] == "synced"
    path = sync.path(book)
    native = read_native(path.read_bytes(), book.content.sha256)
    assert native.metadata.uploaded_at is not None
    assert native.metadata.source_rating == book.metadata.source_rating
    native.metadata.title = "External title"
    path.write_bytes(native.canonical_bytes())
    result = sync.sync_one(book.id)
    resolved = sync.resolve(result["conflict_id"], fields={"title": "file"})
    assert resolved.metadata.uploaded_at == book.metadata.uploaded_at
    assert (
        resolved.metadata.source == book.metadata.source and resolved.metadata.parent == book.metadata.parent
    )
    assert resolved.metadata.title == "External title"
    assert sync.sync_one(book.id)["state"] == "synced"


@pytest.mark.parametrize("method", ["_publish", "_record"])
def test_process_exit_at_sidecar_boundary_recovers(sync: SidecarSync, tmp_path: Path, method: str):
    book_id = import_book(sync.files, tmp_path).id
    edit(sync, book_id, "baseline")
    sync.sync_one(book_id)
    edit(sync, book_id, "durable new value")
    code = (
        CRASH_AFTER
        + """
import sys
from pathlib import Path
from mangacouch.database import Library
from mangacouch.ingest import FileLibrary
from mangacouch.sidecars import SidecarSync
sync = SidecarSync(FileLibrary(Library(Path(sys.argv[1])), Path(sys.argv[2])))
crash_after(sync, sys.argv[4], 73)
sync.sync_one(sys.argv[3])
"""
    )
    process = python("-c", code, sync.library.path, sync.root.path, book_id, method)
    assert process.returncode == 73, process.stderr
    reopened = SidecarSync(FileLibrary(Library(sync.library.path), sync.root.path))
    assert reopened.sync_one(book_id)["state"] in {"synced", "unchanged"}
    assert pending_sidecars(reopened.library) == [] and reopened.conflicts() == []
    assert not list(sync.root.path.rglob("*.tmp"))
    native = read_native(
        sync.path(sync.library.get_book(book_id)).read_bytes(), sync.library.get_book(book_id).content.sha256
    )
    assert native.metadata.title == "durable new value"


def test_failed_replace_leaves_no_temporary_file_and_is_retried(
    sync: SidecarSync, tmp_path: Path, monkeypatch
):
    book_id = import_book(sync.files, tmp_path).id
    replace_file = os.replace

    def fail(*_args):
        raise OSError("simulated disk error")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail)
        assert sync.sync_one(book_id) == {"state": "error", "error": "storage_error"}
    assert os.replace is replace_file
    assert not list(sync.root.path.rglob("*.tmp"))
    assert pending_sidecars(sync.library) == [(book_id, 1)]
    assert sync.sync_one(book_id)["state"] == "synced"


def test_batch_isolates_bad_sidecar_and_stops_on_offline_root(sync: SidecarSync, tmp_path: Path):
    first, second = (
        import_book(sync.files, tmp_path, "first").id,
        import_book(sync.files, tmp_path, "second").id,
    )
    sync.path(sync.library.get_book(first)).write_bytes(b"invalid")
    result = synced(sync.batch())
    assert result[first]["state"] == "error" and result[second]["state"] == "synced"
    first_page = sync.batch(limit=1)
    second_page = sync.batch(limit=1, after=first_page["after"])
    assert set(synced(first_page)) | set(synced(second_page)) == {first, second}
    sync.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        sync.batch()
    assert set(sync.library.search("")) == {first, second}


def test_cli_sync_conflict_resolution_and_restore_loads_config(sync: SidecarSync, tmp_path: Path):
    book_id = import_book(sync.files, tmp_path).id

    def run(arguments, status=0):
        result = python("-m", "mangacouch", *arguments)
        assert result.returncode == status, result.stdout + result.stderr
        return json.loads(result.stdout)

    context = ["--db", sync.library.path, "--root", sync.root.path]
    assert synced(run([*context, "sync"]))[book_id]["state"] == "synced"
    external(sync, book_id, "CLI external")
    conflict_id = run([*context, "sync", "--book-id", book_id])["conflict_id"]
    assert run([*context, "conflicts"])["conflicts"][0]["id"] == conflict_id
    run([*context, "resolve", conflict_id, "--choice", "file"])
    snapshot = tmp_path / "snapshot.zip"
    run([*context, "--max-bytes", 10, "snapshot", snapshot])
    restored = tmp_path / "restored"
    assert run(["restore", snapshot, restored])["restored"] is True
    new_context = ["--db", restored / "library.sqlite"]
    assert run([*new_context, "list"])["items"][0]["title"] == "CLI external"
    run([*new_context, "import", tmp_path / "sample.zip"], status=1)
    sync.root.path.rename(tmp_path / "offline")
    assert run([*new_context, "doctor"], status=2)["error"] == "root_unavailable"
    assert len(run([*new_context, "list"])["items"]) == 1


def test_eze_refresh_mirrors_source_but_retains_native_manual_fields_and_diff(queue, files):
    service, remote = queue
    done = finish(service, ready(service))
    book = files.library.get_book(done["book_id"])
    eze = eze_path(files.root.resolve(book.content.relative_path))
    native = SidecarSync(files).path(book)
    changed = book.metadata.model_copy(deep=True)
    changed.title = "Hand edited"
    book = Review(files).edit(book.id, changed, expected_revision=book.metadata_revision)
    SidecarSync(files).sync_one(book.id)
    before = native.read_bytes()
    remote.title = "Fresh source title"
    observation = service.sources.fetch(book.metadata.source, comments=False)
    service.online.apply(book.id, observation, book.metadata_revision)
    assert native.read_bytes() == before
    assert json.loads(eze.read_bytes())["gallery_info"]["title"] == "Fresh source title"
    assert json.loads(eze.read_bytes())["gdata"]["title"] == "Fresh source title"
    current = files.library.get_book(book.id)
    assert current.metadata.title == "Hand edited"
    assert "title" in current.metadata.protected_fields
    candidate = Review(files).preview(book.id)["candidates"]["online:ehentai"]
    assert candidate["metadata"]["title"] == "Fresh source title"
    SidecarSync(files).sync_one(book.id)
    assert json.loads(native.read_bytes())["metadata"]["title"] == "Hand edited"


def test_eze_write_failure_remains_pending_and_recovers_without_touching_native(queue, files, monkeypatch):
    service, remote = queue
    done = finish(service, ready(service))
    book = files.library.get_book(done["book_id"])
    native = SidecarSync(files).path(book).read_bytes()
    remote.title = "Next source"
    observed = service.sources.fetch(book.metadata.source, comments=False)
    replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *_: (_ for _ in ()).throw(OSError("write failure")))
    service.online.apply(book.id, observed, book.metadata_revision)
    with files.library.connection() as db:
        assert db.execute("SELECT synced_hash FROM eze_cache").fetchone()[0] is None
    assert SidecarSync(files).path(book).read_bytes() == native
    # A write that keeps failing is no progress: the maintenance worker must idle, not spin.
    assert EzeCache(files).sync_pending() == 0
    monkeypatch.setattr(os, "replace", replace)
    assert EzeCache(files).sync_pending() == 1
    assert (
        json.loads(eze_path(files.root.resolve(book.content.relative_path)).read_bytes())["gallery_info"][
            "title"
        ]
        == "Next source"
    )


def test_native_has_bound_content_and_semantic_hash_without_private_data(library: Library):
    book, _ = register_book(
        library,
        content(),
        Metadata(title="书", protected_fields=["title"], field_sources={"title": "manual"}),
    )
    native = library.native_sidecar(book.id)
    data = native.canonical_bytes()
    reserialized = json.dumps(json.loads(data), ensure_ascii=False, indent=4).encode()
    assert read_native(reserialized, book.content.sha256).semantic_hash() == native.semantic_hash()
    assert book.id.encode() not in data
    assert read_native(data, book.content.sha256).metadata.protected_fields == ["title"]
    with pytest.raises(ValueError, match="different file content"):
        read_native(data, "f" * 64)
    for field in ["user_rating", "progress", "favorites", "extensions", "archive_id"]:
        invalid = json.loads(data)
        invalid[field] = "forbidden"
        with pytest.raises(ValidationError):
            read_native(json.dumps(invalid).encode(), book.content.sha256)
    invalid = json.loads(data)
    invalid["schema"] = "mangacouch.sidecar/3"
    with pytest.raises(ValidationError):
        read_native(json.dumps(invalid).encode(), book.content.sha256)
    del invalid["schema"]
    with pytest.raises(ValidationError):
        read_native(json.dumps(invalid).encode(), book.content.sha256)


def test_published_native_sidecar_schema_matches_the_model():
    path = Path(__file__).parents[1] / "schema" / "native-sidecar.schema.json"
    published = json.loads(path.read_text(encoding="utf-8"))
    assert published.pop("$comment")
    assert published == NativeSidecar.model_json_schema(by_alias=True)


def test_conflict_filter_selects_book_before_applying_limit(
    client: TestClient, files: FileLibrary, tmp_path: Path
):
    ids = [import_book(files, tmp_path).id]
    ids.append(files.import_file(make_zip(tmp_path / "other.zip", ["different.png"])).book.id)
    sync = SidecarSync(files)
    for identifier in ids:
        sync.sync_one(identifier)
        path = sync.path(files.library.get_book(identifier))
        document = json.loads(path.read_text())
        document["metadata"]["title"] = "external change"
        path.write_text(json.dumps(document))
        assert sync.sync_one(identifier)["state"] == "conflict"
    login(client)
    conflicts = client.get(f"/api/sidecar/conflicts?book_id={ids[1]}&limit=1").json()["items"]
    assert len(conflicts) == 1 and conflicts[0]["book_id"] == ids[1]
    assert client.get("/api/sidecar/conflicts?book_id=missing").json() == {"items": [], "after": None}
    first = client.get("/api/sidecar/conflicts?limit=1").json()
    assert [item["book_id"] for item in first["items"]] == ids[:1] and first["after"]
    second = client.get("/api/sidecar/conflicts", params={"limit": 1, "after": first["after"]}).json()
    assert [item["book_id"] for item in second["items"]] == ids[1:] and second["after"] is None
    assert client.get("/api/sidecar/conflicts?after=bad").status_code == 422


def test_superseded_sidecar_conflicts_are_dropped_and_pending_ones_kept(sync: SidecarSync, tmp_path):
    book_id = import_book(sync.files, tmp_path).id
    sync.sync_one(book_id)
    external(sync, book_id, "first external")
    assert sync.sync_one(book_id)["state"] == "conflict"
    external(sync, book_id, "second external")
    assert sync.sync_one(book_id)["state"] == "conflict"
    with sync.library.connection() as db:
        assert sorted(row[0] for row in db.execute("SELECT state FROM sidecar_conflict")) == [
            "pending",
            "superseded",
        ]
    assert sync.purge() == 1
    assert [conflict["book_id"] for conflict in sync.conflicts()] == [book_id]


UNKNOWN: Any = "elsewhere"


class Conflict:
    """A book edited in the library ("local") while its sidecar was edited elsewhere ("external", "ja")."""

    def __init__(self, files: FileLibrary, tmp_path: Path):
        self.files, self.sync = files, SidecarSync(files)
        self.book_id = import_book(files, tmp_path).id
        edit(self.sync, self.book_id, "baseline")
        self.sync.sync_one(self.book_id)
        edit(self.sync, self.book_id, "local")
        native = files.library.native_sidecar(self.book_id)
        native.metadata = Metadata(title="external", language="ja")
        self.sidecar = self.sync.path(files.library.get_book(self.book_id))
        self.external = native.canonical_bytes()
        self.sidecar.write_bytes(self.external)
        result = self.sync.sync_one(self.book_id)
        assert result["state"] == "conflict"
        self.id = result["conflict_id"]
        assert self.sync.conflicts()[0]["fields"] == ["title", "language"]

    def assert_unresolved(self) -> None:
        """Nothing changed: the conflict still waits, the library and the file keep their versions."""
        assert [item["id"] for item in self.sync.conflicts()] == [self.id]
        metadata = self.files.library.get_book(self.book_id).metadata
        assert (metadata.title, metadata.language) == ("local", "en")
        assert metadata.protected_fields == [] and self.sidecar.read_bytes() == self.external


@pytest.fixture
def conflict(files, tmp_path) -> Conflict:
    return Conflict(files, tmp_path)


def test_a_resolved_conflict_cannot_be_resolved_again(conflict):
    resolved = conflict.sync.resolve(conflict.id, choice="file")
    assert resolved.metadata.title == "external" and conflict.sync.conflicts() == []
    for choice in ("library", "file"):
        with pytest.raises(RevisionConflict, match="no longer pending"):
            conflict.sync.resolve(conflict.id, choice=choice)
    with pytest.raises(RevisionConflict, match="no longer pending"):
        conflict.sync.resolve("unknown")
    assert conflict.files.library.get_book(conflict.book_id).metadata == resolved.metadata


@pytest.mark.parametrize(
    "fields",
    [{"title": "file"}, {"language": "library"}, {"title": "file", "language": "file", "uploader": "file"}],
)
def test_field_resolution_needs_a_choice_for_exactly_the_differing_fields(conflict, fields):
    with pytest.raises(InvalidInput, match="every differing metadata field"):
        conflict.sync.resolve(conflict.id, fields=fields)
    conflict.assert_unresolved()


def test_field_resolution_rejects_an_unknown_version(conflict):
    with pytest.raises(InvalidInput, match="Invalid field choice"):
        conflict.sync.resolve(conflict.id, fields={"title": "file", "language": UNKNOWN})
    conflict.assert_unresolved()


def test_whole_record_resolution_rejects_an_unknown_version(conflict):
    with pytest.raises(InvalidInput, match="Invalid conflict choice"):
        conflict.sync.resolve(conflict.id, choice=UNKNOWN)
    conflict.assert_unresolved()


@pytest.mark.usefixtures("without_maintenance")
def test_http_resolution_validates_the_choice_and_reports_a_settled_conflict(client, auth, conflict):
    _, key = auth.create_key("management", "sidecars")
    headers = {"Authorization": f"Bearer {key}"}
    route = f"/api/sidecar/conflicts/{conflict.id}/resolve"
    for body in ({"choice": "elsewhere"}, {"fields": {"title": "elsewhere", "language": "file"}}):
        response = client.post(route, json=body, headers=headers)
        assert response.status_code == 422 and response.json()["error"] == "invalid_input", body
    response = client.post(route, json={"fields": {"title": "file"}}, headers=headers)
    assert response.status_code == 422 and response.json()["error"] == "invalid_input"
    conflict.assert_unresolved()
    response = client.post(route, json={"fields": {"title": "file", "language": "library"}}, headers=headers)
    assert response.status_code == 200
    assert (response.json()["metadata"]["title"], response.json()["metadata"]["language"]) == (
        "external",
        "en",
    )
    response = client.post(route, json={"choice": "library"}, headers=headers)
    assert response.status_code == 409 and response.json()["error"] == "revision_conflict"
