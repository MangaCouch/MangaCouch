from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from helpers import content, make_zip, register_book

from mangacouch.activity import Activity
from mangacouch.errors import InvalidInput
from mangacouch.models import PersonalState
from mangacouch.removals import Removals
from mangacouch.source_config import SourceVault


def test_manual_completion_counts_without_reading_history_or_days(library):
    book, _ = register_book(library, content())
    book = library.save_personal(
        book.id, PersonalState(completed=True), expected_revision=1, record_activity=False
    )
    activity = Activity(library)
    stats = activity.statistics()
    assert stats["period"]["completed"] == 1
    assert stats["period"]["books"] == stats["period"]["updates"] == stats["period"]["active_days"] == 0
    assert activity.recent()["items"] == []
    library.save_personal(
        book.id, PersonalState(completed=True, rating=4), expected_revision=2, record_activity=False
    )
    assert activity.statistics()["period"]["completed"] == 1
    with library.connection() as db:
        assert db.execute("SELECT count(*) FROM reading_event").fetchone()[0] == 1


def test_missing_file_check_returns_time_bounded_cursor_and_continues(files, tmp_path, monkeypatch):
    for i in range(4):
        files.import_file(make_zip(tmp_path / f"{i}.cbz", [f"{i}.png"]))
    clock = iter([0, 11, 20, 31, 40, 51, 60, 71])
    monkeypatch.setattr(
        "mangacouch.removals.time", SimpleNamespace(time=time.time, monotonic=lambda: next(clock))
    )
    removals = Removals(files)
    after = ""
    through = None
    checked = 0
    for _ in range(4):
        page = removals.check_page(after=after, through_id=through)
        assert page["checked"] == 1
        checked += page["checked"]
        after, through = page["after"], page["through_id"]
    assert checked == 4 and after is None


@pytest.mark.parametrize(
    ("key", "value", "code"),
    [
        ("eh_cookie", "   ", "invalid_credential"),
        ("ex_cookie", "a\nb", "invalid_credential"),
        ("proxy", "http://host:invalid", "invalid_proxy"),
        ("proxy", "http://[", "invalid_proxy"),
        ("proxy", "proxy.example", "invalid_proxy"),
    ],
)
def test_credential_errors_are_specific_and_empty_clear_remains_explicit(key, value, code):
    with pytest.raises(InvalidInput) as error:
        SourceVault.validate({key: value})
    assert error.value.code == code
    SourceVault.validate({key: ""})
