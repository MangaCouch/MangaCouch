from __future__ import annotations

import itertools
import random

import pytest
from helpers import archive, content, indexed, make_zip, register_book

from mangacouch.duplicates import Duplicates
from mangacouch.page_features import PageFeatures, bands, candidates, ordered_matching


def test_segment_index_covers_every_hamming_neighbor_and_order_matching_is_maximal():
    rng = random.Random(12)
    values = [rng.getrandbits(64) for _ in range(30)]
    for value in values:
        for distance in range(7):
            neighbor = value
            for bit in rng.sample(range(64), distance):
                neighbor ^= 1 << bit
            assert set(bands(value)) & set(bands(neighbor))
    for _ in range(30):
        edges = [(a, b, 0) for a, b in itertools.product(range(4), repeat=2) if rng.random() < 0.5]
        match = ordered_matching(edges)
        expected = 0
        for size in range(1, 5):
            if any(
                all(a[0] < b[0] and a[1] < b[1] for a, b in zip(choice, choice[1:], strict=False))
                for choice in itertools.combinations(sorted(edges), size)
            ):
                expected = size
        assert len(match) == expected
        assert len({a for a, _, _ in match}) == len(match) == len({b for _, b, _ in match})


def finish(duplicates: Duplicates) -> None:
    for _ in range(200):
        duplicates.poke()
        duplicates.run(budget=1.0)
        status = duplicates.features.status()
        with duplicates.library.connection() as db:
            cursor = db.execute("SELECT feature_id FROM duplicate_cursor").fetchone()[0]
            highest = db.execute("SELECT coalesce(max(id),0) FROM page_feature").fetchone()[0]
        if status["indexed_pages"] == status["total_pages"] and cursor > highest:
            return
    raise AssertionError("Duplicate detection did not converge")


def test_candidate_cursor_exhausts_dense_bucket(files, tmp_path):
    left = archive(files, tmp_path, "a.cbz", [0])
    right = archive(files, tmp_path, "b.cbz", [0] * 5)
    features = PageFeatures(files)
    indexed(features, left)
    indexed(features, right)
    assert features.status() == {"indexed_pages": 6, "total_pages": 6}
    with files.library.connection() as db:
        source = db.execute("SELECT * FROM page_feature WHERE book_id=?", (left.id,)).fetchone()
        after, found = 0, []
        while True:
            rows = candidates(db, source, after=after, limit=2)
            found.extend(rows[:2])
            if len(rows) <= 2:
                break
            after = rows[1]["id"]
    assert sorted(row["page"] for row in found) == list(range(5))
    assert all(row["book_id"] == right.id for row in found)


def test_duplicate_groups_need_shared_pages_in_reading_order_and_respect_ignores(files, tmp_path, api):
    left = archive(files, tmp_path, "a.cbz", [1, 2, 3, 4])
    right = archive(files, tmp_path, "b.cbz", [1, 2, 3, 4, 5])
    cover_only = archive(files, tmp_path, "c.cbz", [1, 7, 8])
    duplicates = Duplicates(PageFeatures(files))
    finish(duplicates)
    result = duplicates.groups()
    assert result["indexed_pages"] == result["total_pages"] == 12
    assert [(group["left_id"], group["right_id"]) for group in result["groups"]] == [
        tuple(sorted((left.id, right.id)))
    ]
    assert result["groups"][0]["matched_pages"] == 4 and result["groups"][0]["coverage"] == 1
    assert cover_only.id not in {identifier for group in result["groups"] for identifier in group.values()}
    response = api.get("/api/duplicates")
    assert response.status_code == 200
    assert [book["id"] for book in response.json()["groups"][0]["books"]] == sorted((left.id, right.id))
    ignore = api.post("/api/duplicates/ignore", json={"left_id": right.id, "right_id": left.id})
    assert ignore.status_code == 200
    assert api.get("/api/duplicates").json()["groups"] == []


def test_blank_pages_never_produce_duplicate_edges(files, tmp_path):
    books = [
        files.import_file(make_zip(tmp_path / f"blank{n}.cbz", [f"{n}a.png", f"{n}b.png", f"{n}c.png"])).book
        for n in range(2)
    ]
    duplicates = Duplicates(PageFeatures(files))
    finish(duplicates)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM page_feature").fetchone()[0] == 6
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 0
    assert duplicates.groups()["groups"] == [] and len(books) == 2


def test_feature_ids_are_never_reused_after_a_book_is_removed(files, tmp_path):
    first = archive(files, tmp_path, "first.cbz", [1, 2])
    features = PageFeatures(files)
    indexed(features, first)
    with files.library.transaction() as db:
        highest = db.execute("SELECT max(id) FROM page_feature").fetchone()[0]
        db.execute("DELETE FROM book WHERE id=?", (first.id,))
    second = archive(files, tmp_path, "second.cbz", [3])
    indexed(features, second)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM page_band").fetchone()[0] == 7
        assert db.execute("SELECT min(id) FROM page_feature").fetchone()[0] > highest


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"left_id": [], "right_id": []},
        {"left_id": None, "right_id": "x"},
        {"left_id": "", "right_id": "x"},
        {"left_id": "x", "right_id": "x"},
    ],
)
def test_duplicate_ignore_validates_json_before_database(api, payload):
    result = api.post("/api/duplicates/ignore", json=payload)
    assert result.status_code == 422 and result.json()["error"] == "invalid_input"


def test_duplicate_ignore_missing_and_replay_have_explicit_contract(api, files):
    missing = api.post("/api/duplicates/ignore", json={"left_id": "missing", "right_id": "other"})
    assert missing.status_code == 404
    one, _ = register_book(files.library, content("duplicate-one"))
    two, _ = register_book(files.library, content("duplicate-two"))
    for left, right in [(one.id, two.id), (two.id, one.id)]:
        result = api.post("/api/duplicates/ignore", json={"left_id": left, "right_id": right})
        assert result.status_code == 200 and result.json() == {"ignored": True}
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_decision").fetchone()[0] == 1


def test_background_index_skips_missing_book_and_continues(files, tmp_path):
    first = files.import_file(make_zip(tmp_path / "first.cbz")).book
    second = files.import_file(make_zip(tmp_path / "second.cbz", ["a.png", "b.png"])).book
    missing, remaining = sorted([first, second], key=lambda b: b.id)
    files.root.resolve(missing.content.relative_path).unlink()
    features = PageFeatures(files)
    features.index(limit=1)
    assert files.library.get_book(missing.id).availability == "missing"
    assert features.index(limit=1)["processed"] == 1
    with files.library.connection() as db:
        assert db.execute("SELECT book_id FROM page_feature").fetchone()[0] == remaining.id


def test_changed_archive_is_not_indexed(files, tmp_path):
    book = archive(files, tmp_path, "a.cbz", [1, 2])
    (files.root.path / book.content.relative_path).write_bytes(b"changed")
    features = PageFeatures(files)
    with pytest.raises(OSError):
        features.index(book.id)
    assert features.index(limit=1)["processed"] == 0
    assert files.library.get_book(book.id).availability == "changed"


def test_undecodable_page_is_recorded_and_indexing_moves_on(files, tmp_path, monkeypatch):
    import pyvips

    from mangacouch import page_features

    book = archive(files, tmp_path, "a.cbz", [1, 2, 3])
    preview = page_features._preview
    calls = []

    def flaky(data, limits):
        calls.append(1)
        if len(calls) == 2:
            raise pyvips.Error("injected libvips failure")
        return preview(data, limits)

    monkeypatch.setattr(page_features, "_preview", flaky)
    features = PageFeatures(files)
    assert features.index(limit=10)["processed"] == 3
    assert files.library.get_book(book.id).availability == "available"
    with files.library.connection() as db:
        rows = db.execute("SELECT page,failed,gray FROM page_feature ORDER BY page").fetchall()
        bands = db.execute("SELECT count(*) FROM page_band").fetchone()[0]
    assert [(row["page"], row["failed"]) for row in rows] == [(0, 0), (1, 1), (2, 0)]
    assert bands == 2 * 7 and not page_features.has_contrast(rows[1])
    assert features.status() == {"indexed_pages": 3, "total_pages": 3}
    assert features.index(limit=10)["processed"] == 0


def test_index_batches_by_limit_and_time_budget_and_status_counts_available_books(files, tmp_path):
    first = archive(files, tmp_path, "a.cbz", [1, 2, 3, 4])
    archive(files, tmp_path, "b.cbz", [5, 6])
    features = PageFeatures(files)
    assert features.index(limit=0 + 5)["processed"] == 5
    assert features.index(limit=100, time_budget=0)["processed"] == 0
    assert features.index(limit=100, time_budget=5)["processed"] == 1
    files.root.resolve(first.content.relative_path).unlink()
    with files.library.transaction() as db:
        db.execute("UPDATE book SET availability='missing' WHERE id=?", (first.id,))
    assert features.status() == {"indexed_pages": 2, "total_pages": 2}
