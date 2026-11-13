"""Read marks from the web menu, bulk actions and Komga clients."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from helpers import make_zip
from test_client_protocols import basic

from mangacouch.activity import Activity
from mangacouch.models import PersonalState


def events(files) -> list[tuple[str, int]]:
    with files.library.connection() as db:
        return [tuple(row) for row in db.execute("SELECT kind,is_reading FROM reading_event ORDER BY id")]


def recently_read(files, book_id: str) -> bool:
    with files.library.connection() as db:
        return db.execute("SELECT 1 FROM reading_activity WHERE book_id=?", (book_id,)).fetchone() is not None


def komga_book(client, auth, files, tmp_path, name="book", pages=("1.png", "2.png", "3.png")):
    book = files.import_file(make_zip(tmp_path / f"{name}.zip", list(pages))).book
    headers = basic(auth)
    path = f"/komga/api/v1/books/{book.id}"
    assert client.get(path, headers=headers).status_code == 200
    return book, headers, path


def test_komga_page_update_never_undoes_finished_even_with_completed_false(client, auth, files, tmp_path):
    book, headers, path = komga_book(client, auth, files, tmp_path)
    files.library.save_personal(book.id, PersonalState(completed=True), expected_revision=1)
    assert client.get(path, headers=headers).status_code == 200
    response = client.patch(path + "/read-progress", headers=headers, json={"page": 1, "completed": False})
    assert response.status_code == 204
    assert files.library.get_book(book.id).personal.completed


def test_komga_completed_false_without_page_and_delete_mark_unread(client, auth, files, tmp_path):
    book, headers, path = komga_book(client, auth, files, tmp_path)
    assert client.patch(path + "/read-progress", headers=headers, json={"page": 2}).status_code == 204
    files.library.save_personal(
        book.id, PersonalState(resume_page=1, completed=True), expected_revision=2, record_activity=False
    )
    assert client.get(path, headers=headers).status_code == 200
    assert (
        client.patch(path + "/read-progress", headers=headers, json={"completed": False}).status_code == 204
    )
    personal = files.library.get_book(book.id).personal
    assert not personal.completed and personal.resume_page is None
    assert client.get(path, headers=headers).json()["readProgress"] is None

    assert client.patch(path + "/read-progress", headers=headers, json={"completed": True}).status_code == 204
    assert files.library.get_book(book.id).personal.completed
    other = basic(auth)
    assert client.delete(path + "/read-progress", headers=other).status_code == 409  # never shown it
    assert client.delete(path + "/read-progress", headers=headers).status_code == 204
    personal = files.library.get_book(book.id).personal
    assert not personal.completed and personal.resume_page is None
    readonly = basic(auth, "readonly")
    assert client.delete(path + "/read-progress", headers=readonly).status_code == 403


def test_komga_mark_read_counts_like_the_web_menu(client, auth, files, tmp_path):
    marked, headers, path = komga_book(client, auth, files, tmp_path, "komga")
    web = files.import_file(make_zip(tmp_path / "web.zip", ["4.png", "5.png"])).book
    assert client.patch(path + "/read-progress", headers=headers, json={"completed": True}).status_code == 204
    files.library.save_personal(
        web.id, PersonalState(completed=True), expected_revision=1, record_activity=False
    )
    yesterday = int(time.time()) - 86400
    with files.library.transaction() as db:
        db.execute("UPDATE reading_event SET created_at=?", (yesterday,))
    marked_at = datetime.fromtimestamp(yesterday, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert events(files) == [("completed", 0), ("completed", 0)]
    activity = Activity(files.library)
    period = activity.statistics()["period"]
    assert period["completed"] == 2
    assert period["books"] == period["updates"] == period["active_days"] == 0
    assert activity.recent()["items"] == []
    # The read date of a book marked read is when it was marked, not 1970.
    for book_id in (marked.id, web.id):
        progress = client.get(f"/komga/api/v1/books/{book_id}", headers=headers).json()["readProgress"]
        assert progress["completed"] and progress["readDate"] == marked_at
    listed = client.get("/komga/api/v1/books?sort=readProgress.readDate,desc", headers=headers).json()
    assert {item["id"] for item in listed["content"]} == {marked.id, web.id}
    assert all(item["readProgress"]["readDate"] == marked_at for item in listed["content"])


def test_komga_reading_to_the_last_page_is_reading(client, auth, files, tmp_path):
    book, headers, path = komga_book(client, auth, files, tmp_path)
    assert client.patch(path + "/read-progress", headers=headers, json={"page": 3}).status_code == 204
    assert files.library.get_book(book.id).personal.completed
    assert events(files) == [("completed", 1)]
    assert Activity(files.library).statistics()["period"]["books"] == 1


def test_mark_unread_takes_back_a_manual_completion_and_leaves_recent(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip", ["1.png", "2.png", "3.png"])).book
    library = files.library
    book = library.save_personal(book.id, PersonalState(resume_page=1), expected_revision=1)
    assert recently_read(files, book.id)
    book = library.save_personal(
        book.id, PersonalState(resume_page=1, completed=True), expected_revision=2, record_activity=False
    )
    activity = Activity(library)
    assert activity.statistics()["period"]["completed"] == 1
    library.save_personal(book.id, PersonalState(), expected_revision=3, record_activity=False)
    period = activity.statistics()["period"]
    assert period["completed"] == 0
    # The real reading event stays in the history; the book leaves the recently read list.
    assert events(files) == [("progress", 1)]
    assert period["books"] == 1 and len(activity.recent()["items"]) == 1
    assert not recently_read(files, book.id)


def test_mark_unread_keeps_a_completion_reached_by_reading(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.zip", ["1.png", "2.png"])).book
    library = files.library
    library.save_personal(book.id, PersonalState(resume_page=1, completed=True), expected_revision=1)
    library.save_personal(book.id, PersonalState(), expected_revision=2, record_activity=False)
    assert events(files) == [("completed", 1)]
    assert Activity(library).statistics()["period"]["completed"] == 1


def test_bulk_mark_unread_takes_back_the_bulk_completion(files, tmp_path):
    from test_organization import request_for

    from mangacouch.organization import BulkAction, Organization

    book = files.import_file(make_zip(tmp_path / "book.zip")).book
    organization = Organization(files.library)
    for completed in (True, False):
        organization.create_bulk(request_for([book], BulkAction(kind="completed", completed=completed)))
        assert organization.advance(budget=60) is False
        assert files.library.get_book(book.id).personal.completed is completed
    assert events(files) == []
    assert Activity(files.library).statistics()["period"]["completed"] == 0
