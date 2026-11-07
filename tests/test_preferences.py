from __future__ import annotations

import hashlib
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
from fastapi.testclient import TestClient
from helpers import CSRF, book, login
from pydantic import ValidationError

from mangacouch.auth import Auth
from mangacouch.database import Library, RevisionConflict
from mangacouch.files import ROOT_MARKER
from mangacouch.ingest import FileLibrary
from mangacouch.preferences import (
    DeviceDefaults,
    LibraryPreferences,
    load_preferences,
    save_preferences,
)
from mangacouch.snapshots import InvalidSnapshot, create_snapshot, restore_snapshot


def test_preferences_defaults_persist_across_connections_without_touching_book_state(
    files: FileLibrary, tmp_path
):
    item = book(files, tmp_path)
    initial = load_preferences(files.library)
    assert initial.revision == 0 and initial.settings == LibraryPreferences()
    desired = LibraryPreferences(
        title_preference="original_title", device_defaults=DeviceDefaults(direction="rtl", zoom=150)
    )
    saved = save_preferences(files.library, desired, expected_revision=0)
    assert saved.revision == 1
    assert load_preferences(Library(files.library.path)) == saved
    assert files.library.get_book(item.id) == item
    assert save_preferences(files.library, desired, expected_revision=1) == saved


def test_competing_initial_writes_have_one_winner_and_aba_is_rejected(files: FileLibrary):
    def save(mode):
        try:
            return save_preferences(
                files.library,
                LibraryPreferences(device_defaults=DeviceDefaults(reading_mode=mode)),
                expected_revision=0,
            )
        except RevisionConflict:
            return None

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(save, ["spread", "scroll"]))
    assert sum(x is not None for x in results) == 1
    save_preferences(files.library, LibraryPreferences(), expected_revision=1)
    with pytest.raises(RevisionConflict):
        save_preferences(
            files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
        )
    assert load_preferences(files.library).revision == 2


@pytest.mark.parametrize(
    "invalid",
    [
        {"title_preference": "language_guess"},
        {"favorite": True},
        {"device_defaults": {"reading_mode": "auto"}},
        {"device_defaults": {"zoom": 0}},
        {"device_defaults": {"autoplay_seconds": 1}},
        {"device_defaults": {"cover_solo": "false"}},
        {"device_defaults": {"playing": True}},
        {"device_defaults": {"direction": None}},
    ],
)
def test_preferences_reject_unknown_invalid_and_playing_state(invalid):
    with pytest.raises(ValidationError):
        LibraryPreferences.model_validate(invalid)


def test_failed_transaction_keeps_previous_preferences(files: FileLibrary):
    with files.library.transaction() as db:
        db.execute(
            "CREATE TRIGGER reject_preferences BEFORE INSERT ON library_preferences "
            "BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        save_preferences(
            files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
        )
    assert load_preferences(files.library).revision == 0


def test_snapshot_restores_global_preferences_and_defaults_while_root_is_offline(
    files: FileLibrary, tmp_path
):
    saved = save_preferences(
        files.library,
        LibraryPreferences(
            title_preference="original_title",
            device_defaults=DeviceDefaults(
                reading_mode="spread", direction="rtl", cover_solo=False, autoplay_seconds=12, zoom=125
            ),
        ),
        expected_revision=0,
    )
    (files.root.path / ROOT_MARKER).unlink()
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    save_preferences(files.library, LibraryPreferences(), expected_revision=1)
    restored = restore_snapshot(snapshot, tmp_path / "restored")
    assert load_preferences(restored.library) == saved


@pytest.mark.parametrize("damage", ["preferences", "schema7"])
def test_invalid_preferences_or_old_schema_snapshot_is_rejected_before_publication(
    files: FileLibrary, tmp_path, damage
):
    save_preferences(
        files.library, LibraryPreferences(title_preference="original_title"), expected_revision=0
    )
    snapshot = create_snapshot(files, tmp_path / "snapshot.zip")
    with zipfile.ZipFile(snapshot) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    database = tmp_path / "tampered.sqlite"
    database.write_bytes(members["library.sqlite"])
    with closing(sqlite3.connect(database)) as db:
        if damage == "schema7":
            db.execute("PRAGMA user_version=7")
        else:
            db.execute(
                "UPDATE library_preferences SET settings_json=?",
                (json.dumps({"device_defaults": {"playing": True}}),),
            )
        db.commit()
    members["library.sqlite"] = database.read_bytes()
    manifest = json.loads(members["manifest.json"])
    manifest["files"]["library.sqlite"] = {
        "size": len(members["library.sqlite"]),
        "sha256": hashlib.sha256(members["library.sqlite"]).hexdigest(),
    }
    members["manifest.json"] = json.dumps(manifest).encode()
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(corrupt, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    target = tmp_path / "should-not-exist"
    with pytest.raises(InvalidSnapshot):
        restore_snapshot(corrupt, target)
    assert not target.exists()


def test_preferences_http_auth_csrf_conflict_and_offline_retrieval(
    client: TestClient, files: FileLibrary, auth: Auth
):
    assert client.get("/api/preferences").status_code == 401
    login(client)
    initial = client.get("/api/preferences")
    assert initial.status_code == 200 and initial.headers["cache-control"] == "no-store"
    body = {"expected_revision": 0, "settings": {"title_preference": "original_title"}}
    assert client.put("/api/preferences", json=body).status_code == 403
    _, key = auth.create_key("readonly", "reader")
    headers = {"Authorization": f"Bearer {key}"}
    assert client.get("/api/preferences", headers=headers).status_code == 200
    assert client.put("/api/preferences", headers=headers, json=body).status_code == 403
    (files.root.path / ROOT_MARKER).unlink()
    result = client.put("/api/preferences", headers=CSRF, json=body)
    assert result.status_code == 200 and result.json()["revision"] == 1
    assert client.get("/api/preferences").json() == result.json()
    assert client.put("/api/preferences", headers=CSRF, json=body).status_code == 409
    assert (
        client.put(
            "/api/preferences", headers=CSRF, json={"expected_revision": 1, "settings": {"playing": True}}
        ).status_code
        == 422
    )
    assert client.get("/api/preferences").json() == result.json()
