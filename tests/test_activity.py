from __future__ import annotations

import pytest
from helpers import confirmed, content, make_zip, register_book

from mangacouch.activity import Activity
from mangacouch.database import RevisionConflict
from mangacouch.models import Metadata, PersonalState
from mangacouch.removals import Removals


def test_restored_history_keeps_chronology_and_remaps_confirmation_evidence(files, tmp_path):
    review, older = confirmed(files, tmp_path)
    changed = review.edit(
        older.id,
        older.metadata.model_copy(update={"title": "Temporary"}),
        expected_revision=older.metadata_revision,
    )
    review.edit(older.id, older.metadata, expected_revision=changed.metadata_revision)
    newer = files.import_file(make_zip(tmp_path / "newer.zip", ["newer.png"])).book
    newer = review.edit(newer.id, Metadata(title="Newer", language="English"), expected_revision=1)
    preview = review.preview(newer.id)
    review.complete(newer.id, preview["token"], preview["issues"])
    for book in [older, newer]:
        files.library.save_personal(book.id, PersonalState(resume_page=0), expected_revision=1)
    with files.library.transaction() as db:
        db.execute("UPDATE reading_event SET created_at=100 WHERE book_id=?", (older.id,))
        db.execute("UPDATE reading_event SET created_at=86500 WHERE book_id=?", (newer.id,))
    removal = Removals(files)
    preview = removal.preview(older.id)
    removal.remove(preview["id"], older.id, preview["token"], confirmed=True)
    removal.restore(preview["id"])
    activity = Activity(files.library)
    first = activity.recent(limit=1, utc_offset=0)
    assert first["events"][0]["book_id"] == newer.id
    assert isinstance(first["before"], str)
    last = activity.recent(before=first["before"], limit=1, utc_offset=0)
    assert last["events"][0]["book_id"] == older.id and last["before"] is None
    proof = review.preview(older.id)
    evidence = proof["confirmation"]["evidence_event_id"]
    assert evidence == proof["automatic"]["evidence_event_id"]
    with files.library.connection() as db:
        row = db.execute("SELECT book_id,action FROM review_event WHERE id=?", (evidence,)).fetchone()
        assert tuple(row) == (older.id, "manual_confirm")
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_history_uses_latest_timestamp_per_day_with_equal_timestamp_tiebreak(files):
    one, _ = register_book(files.library, content("one"))
    two, _ = register_book(files.library, content("two"))
    with files.library.transaction() as db:
        for book_id, timestamp in [(one.id, 200), (two.id, 200), (one.id, 100)]:
            db.execute(
                "INSERT INTO reading_event(book_id,created_at,page,completed,kind) "
                "VALUES(?,?,0,0,'progress')",
                (book_id, timestamp),
            )
    activity = Activity(files.library)
    first = activity.recent(limit=1, utc_offset=0)
    last = activity.recent(limit=1, utc_offset=0, before=first["before"])
    assert [first["events"][0]["book_id"], last["events"][0]["book_id"]] == [two.id, one.id]
    assert last["events"][0]["created_at"] == 200 and last["before"] is None


@pytest.mark.parametrize("cursor", ["0", "-1:2", "1:0", "1:2:3", "1:9223372036854775808"])
def test_history_rejects_invalid_cursor_at_http_boundary(api, cursor):
    result = api.get("/api/reading-history", params={"before": cursor})
    assert result.status_code == 422 and result.json()["error"] == "invalid_input"


def test_activity_ignores_ratings_bookmarks_noops_and_failed_writes(library):
    book, _ = register_book(library, content("activity"))
    activity = Activity(library)
    book = library.save_personal(book.id, PersonalState(rating=4), expected_revision=1)
    assert activity.recent()["events"] == []
    state = book.personal.model_copy(update={"resume_page": 0})
    book = library.save_personal(book.id, state, expected_revision=book.personal_revision)
    book = library.save_personal(book.id, state, expected_revision=book.personal_revision)
    book = library.save_personal(
        book.id, state.model_copy(update={"bookmarks": [0]}), expected_revision=book.personal_revision
    )
    with pytest.raises(RevisionConflict):
        library.save_personal(book.id, state.model_copy(update={"completed": True}), expected_revision=1)
    assert len(activity.recent()["events"]) == 1
    book = library.save_personal(
        book.id,
        book.personal.model_copy(update={"completed": True}),
        expected_revision=book.personal_revision,
    )
    stats = activity.statistics()
    assert stats["period"]["books"] == 1 and stats["period"]["completed"] == 1
    assert stats["library"]["bytes"] == book.content.byte_size
    assert stats["period"]["active_days"] == 1
    assert len(stats["daily"]) == 30
    first = activity.recent(limit=1)
    assert first["events"][0]["kind"] == "completed"
    assert activity.recent(before=first["before"], limit=1)["events"][0]["kind"] == "progress"
    grouped = activity.recent(limit=1, utc_offset=0)
    assert grouped["before"] is None and grouped["events"][0]["kind"] == "completed"
    book = library.save_personal(book.id, PersonalState(), expected_revision=book.personal_revision)
    assert activity.recent()["events"][0]["kind"] == "reset"
    assert activity.statistics()["period"]["books"] == 1
    with library.transaction() as db:
        db.execute("DELETE FROM book WHERE id=?", (book.id,))
    assert activity.recent()["events"] == []


def test_history_paginates_distinct_book_days_before_applying_cursor(library):
    one, _ = register_book(library, content("history-one"))
    two, _ = register_book(library, content("history-two"))
    with library.transaction() as db:
        for identifier, timestamp in [(one.id, 1), (two.id, 1), (one.id, 2), (one.id, 86401)]:
            db.execute(
                "INSERT INTO reading_event(book_id,created_at,page,completed,kind) "
                "VALUES(?,?,0,0,'progress')",
                (identifier, timestamp),
            )
    activity = Activity(library)
    latest = activity.recent(limit=1, utc_offset=0)
    assert latest["events"][0]["created_at"] == 86401
    middle = activity.recent(before=latest["before"], limit=1, utc_offset=0)
    assert middle["events"][0]["book_id"] == one.id and middle["events"][0]["created_at"] == 2
    last = activity.recent(before=middle["before"], limit=1, utc_offset=0)
    assert last["events"][0]["book_id"] == two.id and last["before"] is None
    assert len(activity.recent(utc_offset=-60)["events"]) == 3
