from __future__ import annotations

import json

import pytest
from helpers import CSRF, edit, external, import_book, login, synced

from mangacouch.files import RootUnavailable
from mangacouch.models import PersonalState
from mangacouch.sidecars import SidecarSync


def test_status_is_read_only_paged_and_distinguishes_persisted_states(
    sync: SidecarSync, tmp_path, monkeypatch
):
    ids = {
        state: import_book(sync.files, tmp_path, state).id
        for state in ["pending", "error", "conflict", "synced"]
    }
    for state in ["error", "conflict", "synced"]:
        sync.sync_one(ids[state])
    external(sync, ids["conflict"], "external title")
    assert sync.sync_one(ids["conflict"])["state"] == "conflict"
    sync.path(sync.library.get_book(ids["error"])).write_text("not json")
    assert sync.sync_one(ids["error"])["state"] == "error"
    before = [sync.library.get_book(book_id) for book_id in ids.values()]
    existing = {str(p): p.read_bytes() for p in sync.root.path.rglob("*") if p.is_file()}

    def no_files(*_args, **_kwargs):
        pytest.fail("Status must not touch the media root")

    monkeypatch.setattr(sync.root, "check", no_files)
    monkeypatch.setattr(sync.root, "resolve", no_files)
    rows, cursor = [], ""
    while True:
        page = sync.status(limit=1, after=cursor)
        assert page["counts"] == dict.fromkeys(ids, 1)
        rows.extend(page["items"])
        if page["after"] is None:
            break
        cursor = page["after"]
    assert {row["state"]: row["id"] for row in rows} == {
        state: key for state, key in ids.items() if state != "synced"
    }
    assert len({row["id"] for row in rows}) == 3
    assert [sync.library.get_book(book_id) for book_id in ids.values()] == before
    assert {str(p): p.read_bytes() for p in sync.root.path.rglob("*") if p.is_file()} == existing


def test_pending_sync_skips_clean_books_while_full_check_finds_external_edits(sync, tmp_path):
    clean, pending = (
        import_book(sync.files, tmp_path, "clean").id,
        import_book(sync.files, tmp_path, "pending").id,
    )
    sync.sync_one(clean)
    before = external(sync, clean, "externally changed")
    assert set(synced(sync.batch(mode="pending"))) == {pending}
    result = synced(sync.batch(mode="all"))
    assert result[clean]["state"] == "conflict"
    assert sync.path(sync.library.get_book(clean)).read_bytes() == before
    assert sync.library.get_book(clean).metadata.title is None
    assert sync.status()["counts"]["conflict"] == 1


def test_bounded_pass_retains_edits_behind_cursor_for_next_pass(sync, tmp_path):
    ids = sorted(import_book(sync.files, tmp_path, str(n)).id for n in range(3))
    first = sync.batch(limit=1, through_id=ids[1], mode="pending")
    assert list(synced(first)) == [ids[0]]
    assert first["after"] is not None and first["through_id"] == ids[1]
    edit(sync, ids[0], "new edit behind the cursor")
    second = sync.batch(limit=1, after=first["after"], through_id=first["through_id"], mode="pending")
    assert list(synced(second)) == [ids[1]] and second["after"] is None
    assert {row["id"] for row in sync.status()["items"]} == {ids[0], ids[2]}
    assert set(synced(sync.batch(mode="pending"))) == {ids[0], ids[2]}
    assert sync.status()["counts"]["synced"] == 3


def test_repeating_lost_full_batch_response_does_not_rewrite_or_lose_personal_state(sync, tmp_path):
    identifier = import_book(sync.files, tmp_path).id
    sync.library.save_personal(identifier, PersonalState(rating=4.5, resume_page=0), expected_revision=1)
    first = sync.batch(limit=1)
    path = sync.path(sync.library.get_book(identifier))
    stamp, data = path.stat().st_mtime_ns, path.read_bytes()
    retry = sync.batch(limit=1)
    assert synced(first)[identifier]["state"] == "synced"
    assert synced(retry)[identifier]["state"] == "unchanged"
    assert path.stat().st_mtime_ns == stamp and path.read_bytes() == data
    assert sync.library.get_book(identifier).personal.rating == 4.5


def test_removed_book_between_selection_and_sync_does_not_stop_other_books(sync, tmp_path, monkeypatch):
    ids = sorted(import_book(sync.files, tmp_path, str(n)).id for n in range(2))
    original = sync._sync_one

    def remove_then_sync(identifier, **options):
        if identifier == ids[0]:
            with sync.library.transaction() as db:
                db.execute("DELETE FROM book WHERE id=?", (identifier,))
        return original(identifier, **options)

    monkeypatch.setattr(sync, "_sync_one", remove_then_sync)
    result = synced(sync.batch())
    assert result[ids[0]]["state"] == "removed" and result[ids[1]]["state"] == "synced"


def test_offline_root_keeps_pending_work_and_restart_reads_it(sync, tmp_path):
    identifier = import_book(sync.files, tmp_path).id
    sync.root.path.rename(tmp_path / "offline")
    with pytest.raises(RootUnavailable):
        sync.batch(mode="pending")
    reopened = SidecarSync(sync.files)
    assert reopened.status()["items"][0]["id"] == identifier
    assert reopened.status()["counts"]["pending"] == 1
    (tmp_path / "offline").rename(sync.root.path)
    assert synced(reopened.batch(mode="pending"))[identifier]["state"] == "synced"


@pytest.mark.usefixtures("without_maintenance")
def test_http_status_is_authenticated_readonly_and_available_offline(client, files, auth, tmp_path):
    service = SidecarSync(files)
    identifier = import_book(service.files, tmp_path).id
    assert client.get("/api/sidecar/status").status_code == 401
    _, key = auth.create_key("readonly", "status fixture")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.post("/api/sidecar/sync", headers=headers, json={"mode": "pending"}).status_code == 403
    files.root.path.rename(tmp_path / "offline")
    status = client.get("/api/sidecar/status", headers=headers)
    assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
    assert status.json()["items"][0]["id"] == identifier
    assert not list((tmp_path / "offline").rglob("*.mangacouch.json"))


def test_http_sync_bounds_csrf_and_external_confirmation_roundtrip(client, files, tmp_path):
    service = SidecarSync(files)
    identifier = import_book(service.files, tmp_path).id
    login(client)
    assert client.post("/api/sidecar/sync", json={"mode": "pending"}).status_code == 403
    first = client.post("/api/sidecar/sync", json={"mode": "pending", "limit": 1}, headers=CSRF)
    assert first.status_code == 200 and first.json()["after"] is None
    changed = external(service, identifier, "external choice")
    found = client.post("/api/sidecar/sync", json={"mode": "all"}, headers=CSRF).json()
    assert synced(found)[identifier]["state"] == "conflict"
    conflict = client.get(f"/api/sidecar/conflicts?book_id={identifier}").json()["items"][0]
    assert service.path(files.library.get_book(identifier)).read_bytes() == changed
    response = client.post(
        f"/api/sidecar/conflicts/{conflict['id']}/resolve", headers=CSRF, json={"choice": "file"}
    )
    assert response.status_code == 200 and response.json()["metadata"]["title"] == "external choice"
    client.post("/api/sidecar/sync", json={"mode": "pending"}, headers=CSRF).raise_for_status()
    assert client.get("/api/sidecar/status").json()["counts"]["synced"] == 1
    assert (
        json.loads(service.path(files.library.get_book(identifier)).read_text())["metadata"]["title"]
        == "external choice"
    )


@pytest.mark.parametrize(
    "payload", [{"mode": "automatic"}, {"through_id": "x" * 65}, {"after": "not-a-cursor"}, {"limit": 1001}]
)
def test_http_rejects_invalid_batch_bounds(client, payload):
    login(client)
    assert client.post("/api/sidecar/sync", json=payload, headers=CSRF).status_code == 422
    assert client.get("/api/sidecar/status?limit=101").status_code == 422
