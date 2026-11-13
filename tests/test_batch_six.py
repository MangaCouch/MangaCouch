import json
import uuid

import pytest
from helpers import CSRF, content, import_book, login, register_book

from mangacouch.collections import Collections
from mangacouch.errors import InvalidInput
from mangacouch.models import PersonalState


def test_finished_rereading_does_not_change_progress_or_activity(library):
    book, _ = register_book(library, content())
    book = library.save_personal(book.id, PersonalState(completed=True, resume_page=29), expected_revision=1)
    with library.connection() as db:
        before = db.execute("SELECT * FROM reading_event").fetchall()
    reread = library.save_personal(book.id, PersonalState(completed=True, resume_page=5), expected_revision=2)
    assert reread.personal.resume_page == 29
    with library.connection() as db:
        assert db.execute("SELECT * FROM reading_event").fetchall() == before


def test_manual_completed_state_records_no_reading(library):
    book, _ = register_book(library, content())
    library.save_personal(book.id, PersonalState(completed=True), expected_revision=1, record_activity=False)
    with library.connection() as db:
        assert db.execute("SELECT count(*) FROM reading_event WHERE is_reading=1").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM reading_activity").fetchone()[0] == 0


def test_bad_collection_rule_is_rejected_without_poisoning_list(library):
    collections = Collections(library)
    rule = {"query": " ".join(f"term{i}" for i in range(33)), "options": {}}
    with pytest.raises(InvalidInput):
        collections.create("bad", rule)
    assert collections.list()["items"] == []
    item = collections.create("existing")
    with library.transaction() as db:
        db.execute("INSERT INTO collection_rule VALUES(?,?)", (item["id"], json.dumps(rule)))
    listed = collections.list()["items"][0]
    assert listed["count"] is None and listed["error"] == "invalid_input"
    assert collections.list(kind="static", counts=False)["items"] == []


@pytest.mark.usefixtures("without_maintenance")
def test_small_bulk_finishes_without_maintenance(client, files, tmp_path):
    book = import_book(files, tmp_path)
    login(client)
    response = client.post(
        "/api/bulk-selection",
        json={
            "id": str(uuid.uuid4()),
            "book_ids": [book.id],
            "action": {"kind": "completed", "completed": True},
        },
        headers=CSRF,
    )
    assert response.status_code == 200
    assert response.json()["state"] == "done"
    assert files.library.get_book(book.id).personal.completed
    with files.library.connection() as db:
        request = json.loads(db.execute("SELECT request_json FROM bulk_job").fetchone()[0])
        assert set(request) == {"action"}
        assert db.execute("SELECT count(*) FROM reading_event WHERE is_reading=1").fetchone()[0] == 0


def test_corrupt_stored_personal_is_internal_error(client, files, tmp_path, caplog):
    book = import_book(files, tmp_path)
    login(client)
    with files.library.transaction() as db:
        db.execute("UPDATE personal SET state_json=? WHERE book_id=?", ('{"rating":99}', book.id))
    response = client.get(f"/api/books/{book.id}")
    assert response.status_code == 500 and response.json()["error"] == "internal_error"
    assert "ValidationError" in caplog.text


def test_unavailable_requires_two_observations_and_recovers(files, tmp_path):
    book = import_book(files, tmp_path)
    path = files.root.resolve(book.content.relative_path)
    saved = path.with_suffix(".away")
    path.rename(saved)
    files.observe_unavailable(book, "missing")
    assert files.library.get_book(book.id).availability == "available"
    files.observe_unavailable(book, "missing")
    assert files.library.get_book(book.id).availability == "missing"
    saved.rename(path)
    assert files.recheck_unavailable() == 1
    assert files.library.get_book(book.id).availability == "available"


def test_new_source_candidate_reopens_confirmed_book(files, tmp_path, sources, remote):
    from source_fixtures import EH

    from mangacouch.online import OnlineMetadata
    from mangacouch.source_http import gallery_url

    book = import_book(files, tmp_path)
    online = OnlineMetadata(files, sources)
    online.refresh(book.id, gallery_url(EH), book.metadata_revision)
    with files.library.transaction() as db:
        db.execute("UPDATE book SET review_state='confirmed' WHERE id=?", (book.id,))
    current = files.library.get_book(book.id)
    online.refresh(book.id, gallery_url(EH), current.metadata_revision)
    assert files.library.get_book(book.id).review_state == "confirmed"
    remote.title = "New candidate title"
    online.refresh(book.id, gallery_url(EH), current.metadata_revision)
    assert files.library.get_book(book.id).review_state == "needs_review"
    with files.library.connection() as db:
        state, reasons = db.execute(
            "SELECT state,reasons_json FROM source_check WHERE book_id=?", (book.id,)
        ).fetchone()
    assert state == "needs_review" and "candidates_unresolved" in json.loads(reasons)


def test_unchanged_sidecar_opens_no_write_transaction(sync, tmp_path, monkeypatch):
    book = import_book(sync.files, tmp_path)
    sync.sync_one(book.id)
    monkeypatch.setattr(sync.library, "transaction", lambda **kw: pytest.fail("unchanged sidecar wrote"))
    assert sync.sync_one(book.id)["state"] == "unchanged"


def test_stale_sidecar_choice_returns_new_conflict(sync, tmp_path):
    from helpers import edit, external

    from mangacouch.errors import RevisionConflict

    book = import_book(sync.files, tmp_path)
    edit(sync, book.id, "baseline")
    sync.sync_one(book.id)
    external(sync, book.id, "first external")
    sync.sync_one(book.id)
    first = sync.conflicts(book_id=book.id)[0]
    external(sync, book.id, "second external")
    with pytest.raises(RevisionConflict) as caught:
        sync.resolve(first["id"], choice="file")
    fresh = caught.value.conflict
    assert fresh is not None and fresh["token"] != first["token"]
    assert fresh["file"]["metadata"]["title"] == "second external"
    external(sync, book.id, "third external")
    sync.sync_pending()
    assert sync.conflicts(book_id=book.id)[0]["file"]["metadata"]["title"] == "third external"
