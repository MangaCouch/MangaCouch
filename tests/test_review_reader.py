from __future__ import annotations

import pytest
from helpers import CSRF, content, login, register_book

from mangacouch.errors import RevisionConflict
from mangacouch.models import Metadata, PersonalState


def test_unload_snapshot_rebases_after_older_progress_without_overwriting_rating(library):
    book, _ = register_book(library, content(), Metadata())
    base = book.personal
    first = base.model_copy(update={"resume_page": 0})
    library.save_personal(book.id, first, expected_revision=1)
    library.save_personal(book.id, first.model_copy(update={"rating": 4}), expected_revision=2)
    result = library.save_personal(
        book.id, base.model_copy(update={"resume_page": 1}), expected_revision=1, base_state=base
    )
    assert result.personal.resume_page == 1
    assert result.personal.rating == 4
    assert result.personal_revision == 4


def test_unload_snapshot_arriving_first_blocks_older_request(library):
    book, _ = register_book(library, content(), Metadata())
    library.save_personal(
        book.id, PersonalState(resume_page=1), expected_revision=1, base_state=book.personal
    )
    with pytest.raises(RevisionConflict):
        library.save_personal(book.id, PersonalState(resume_page=0), expected_revision=1)
    assert library.get_book(book.id).personal.resume_page == 1


def test_unload_snapshot_cannot_overwrite_another_rating(library):
    book, _ = register_book(library, content(), Metadata())
    library.save_personal(book.id, PersonalState(rating=5), expected_revision=1)
    with pytest.raises(RevisionConflict):
        library.save_personal(
            book.id, PersonalState(rating=3, resume_page=1), expected_revision=1, base_state=book.personal
        )
    assert library.get_book(book.id).personal.rating == 5


def test_unload_snapshot_api_accepts_base_and_preserves_concurrent_completion(client, files):
    login(client)
    book, _ = register_book(files.library, content(), Metadata())
    files.library.save_personal(book.id, PersonalState(completed=True, resume_page=29), expected_revision=1)
    response = client.put(
        f"/api/books/{book.id}/personal?expected_revision=1",
        headers=CSRF,
        json={
            "state": PersonalState(resume_page=5).model_dump(),
            "base_state": book.personal.model_dump(),
            "record_activity": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["personal"] == {"completed": True, "resume_page": 29, "rating": None}


def test_unload_snapshot_refuses_a_revision_the_server_never_had(client, files):
    login(client)
    book, _ = register_book(files.library, content(), Metadata())
    files.library.save_personal(book.id, PersonalState(rating=5), expected_revision=1)
    snapshot = PersonalState(rating=5, resume_page=1)
    with pytest.raises(RevisionConflict):
        files.library.save_personal(
            book.id, snapshot, expected_revision=999, base_state=PersonalState(rating=5)
        )
    for revision in (3, 999):
        response = client.put(
            f"/api/books/{book.id}/personal?expected_revision={revision}",
            headers=CSRF,
            json={"state": snapshot.model_dump(), "base_state": PersonalState(rating=5).model_dump()},
        )
        assert response.status_code == 409 and response.json()["error"] == "revision_conflict"
    assert files.library.get_book(book.id).personal.resume_page is None
    # Revision 1 is older than the current one: the snapshot rebases.
    current = files.library.save_personal(
        book.id, snapshot, expected_revision=1, base_state=PersonalState(rating=5)
    )
    assert current.personal == snapshot and current.personal_revision == 3
