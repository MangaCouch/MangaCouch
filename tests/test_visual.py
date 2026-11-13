from __future__ import annotations

import itertools
import random
import tracemalloc
from types import SimpleNamespace

import pytest
from helpers import (
    content,
    import_book,
    indexed,
    make_zip,
    register_book,
    titled,
)

from mangacouch.duplicates import Duplicates
from mangacouch.errors import InvalidInput
from mangacouch.page_features import PENDING, RADIUS, PageFeatures, bands, candidates, ordered_matching


def test_segment_index_covers_every_hamming_neighbor_and_order_matching_is_maximal():
    rng = random.Random(12)
    values = [rng.getrandbits(64) for _ in range(30)]
    for value in values:
        for distance in range(RADIUS + 1):
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
        comparing = duplicates.groups()["comparing"]
        if status["indexed_pages"] == status["total_pages"] and comparing["done"] == comparing["total"]:
            return
    raise AssertionError("Duplicate detection did not converge")


def dense_books(files, pages=128, other_pages=64):
    books = [
        register_book(files.library, content(name, name + ".cbz").model_copy(update={"page_count": count}))[0]
        for name, count in [("left", pages), ("right", other_pages)]
    ]
    with files.library.transaction() as db:
        for book in books:
            for page in range(book.content.page_count):
                row = db.execute(
                    "INSERT INTO page_feature(book_id,page,digest,dhash,gray,aspect) VALUES(?,?,?,?,?,?)",
                    (book.id, page, "d" * 64, "0123456789abcdef", bytes(range(256)), 1.0),
                )
                db.executemany(
                    "INSERT INTO page_band VALUES(?,?,?)",
                    ((segment, value, row.lastrowid) for segment, value in bands(0x0123456789ABCDEF)),
                )
        db.execute("INSERT INTO duplicate_progress VALUES(?)", (books[1].id,))
    return books


def test_duplicate_batches_survive_restart_and_hide_partial_results(files):
    left, right = dense_books(files)
    worker = Duplicates(PageFeatures(files))
    assert worker.step()
    with files.library.connection() as db:
        count = db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0]
    assert 0 < count < left.content.page_count * right.content.page_count
    assert worker.groups()["items"] == []
    restarted = Duplicates(PageFeatures(files))
    for _ in range(20):
        if not restarted.step():
            break
    else:
        pytest.fail("comparison never completed")
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 128 * 64
    result = restarted.groups()
    assert result["comparing"] == {"done": 2, "total": 2}
    assert len(result["items"]) == 1
    assert result["items"][0]["matched_pages"] == 64


@pytest.mark.perf
def test_dense_duplicate_comparison_and_grouping_have_bounded_memory(files):
    dense_books(files, pages=400, other_pages=200)
    worker = Duplicates(PageFeatures(files))
    tracemalloc.start()
    try:
        for _ in range(20):
            if not worker.step():
                break
        else:
            pytest.fail("comparison never completed")
        result = worker.groups()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result["items"][0]["matched_pages"] == 200
    assert peak < 12 * 1024**2, f"dense comparison retained {peak / 1024**2:.1f} MiB"


def test_duplicate_deadline_yields_between_pages_and_resumes(files, monkeypatch):
    from mangacouch import duplicates

    dense_books(files, pages=10, other_pages=2)
    worker = Duplicates(PageFeatures(files))
    now = 0
    original = duplicates.matches

    def compare(left, right):
        nonlocal now
        now += 1
        return original(left, right)

    monkeypatch.setattr(duplicates, "time", SimpleNamespace(monotonic=lambda: now))
    monkeypatch.setattr(duplicates, "matches", compare)
    assert worker.step(deadline=1)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 2
    assert worker.step()
    assert not worker.step()
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 20


def test_duplicate_cursor_restarts_after_features_are_rebuilt(files):
    left, _ = dense_books(files)
    worker = Duplicates(PageFeatures(files))
    assert worker.step()
    with files.library.transaction() as db:
        rows = db.execute("SELECT * FROM page_feature WHERE book_id=?", (left.id,)).fetchall()
        db.execute("DELETE FROM page_feature WHERE book_id=?", (left.id,))
        for row in rows:
            feature = db.execute(
                "INSERT INTO page_feature(book_id,page,digest,dhash,gray,aspect) VALUES(?,?,?,?,?,?)",
                tuple(row[key] for key in ("book_id", "page", "digest", "dhash", "gray", "aspect")),
            )
            db.executemany(
                "INSERT INTO page_band VALUES(?,?,?)",
                ((segment, value, feature.lastrowid) for segment, value in bands(int(row["dhash"], 16))),
            )
    for _ in range(20):
        if not worker.step():
            break
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 128 * 64


def test_candidate_cursor_exhausts_dense_bucket(files, tmp_path):
    left = import_book(files, tmp_path, "a.cbz", seeds=[0])
    right = import_book(files, tmp_path, "b.cbz", seeds=[0] * 5)
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
    left = import_book(files, tmp_path, "a.cbz", seeds=[1, 2, 3, 4])
    right = import_book(files, tmp_path, "b.cbz", seeds=[1, 2, 3, 4, 5])
    cover_only = import_book(files, tmp_path, "c.cbz", seeds=[1, 7, 8])
    duplicates = Duplicates(PageFeatures(files))
    finish(duplicates)
    result = duplicates.groups()
    assert result["indexed_pages"] == result["total_pages"] == 12
    assert [(group["left_id"], group["right_id"]) for group in result["items"]] == [
        tuple(sorted((left.id, right.id)))
    ]
    assert result["items"][0]["matched_pages"] == 4 and result["items"][0]["coverage"] == 1
    assert cover_only.id not in {identifier for group in result["items"] for identifier in group.values()}
    response = api.get("/api/duplicates")
    assert response.status_code == 200
    assert [book["id"] for book in response.json()["items"][0]["books"]] == sorted((left.id, right.id))
    ignore = api.post("/api/duplicates/ignore", json={"left_id": right.id, "right_id": left.id})
    assert ignore.status_code == 204
    assert api.get("/api/duplicates").json()["items"] == []


def test_three_copies_pair_up_and_write_one_edge_per_shared_page_and_pair(files, tmp_path):
    books = [import_book(files, tmp_path, f"copy{n}.cbz", seeds=[1, 2, 3, 4, 10 + n]) for n in range(3)]
    duplicates = Duplicates(PageFeatures(files))
    finish(duplicates)
    pairs = {(group["left_id"], group["right_id"]) for group in duplicates.groups()["items"]}
    assert pairs == set(itertools.combinations(sorted(book.id for book in books), 2))
    with files.library.connection() as db:
        # Each book is compared only against books already finished, so every pair is matched once.
        assert db.execute("SELECT count(*) FROM duplicate_edge").fetchone()[0] == 3 * 4


def test_anthology_is_not_a_duplicate_of_a_chapter_it_contains(files, tmp_path):
    chapter = import_book(files, tmp_path, "chapter.cbz", seeds=[1, 2, 3, 4])
    anthology = import_book(files, tmp_path, "anthology.cbz", seeds=[20, 21, 1, 2, 3, 4, 22, 23, 24, 25])
    retranslated = import_book(files, tmp_path, "retranslated.cbz", seeds=[30, 1, 2, 3, 4])
    duplicates = Duplicates(PageFeatures(files))
    finish(duplicates)
    pairs = [(group["left_id"], group["right_id"]) for group in duplicates.groups()["items"]]
    assert pairs == [tuple(sorted((chapter.id, retranslated.id)))]
    assert anthology.id not in {identifier for pair in pairs for identifier in pair}


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
    assert duplicates.groups()["items"] == [] and len(books) == 2


def test_feature_ids_are_never_reused_after_a_book_is_removed(files, tmp_path):
    first = import_book(files, tmp_path, "first.cbz", seeds=[1, 2])
    features = PageFeatures(files)
    indexed(features, first)
    with files.library.transaction() as db:
        highest = db.execute("SELECT max(id) FROM page_feature").fetchone()[0]
        db.execute("DELETE FROM book WHERE id=?", (first.id,))
    second = import_book(files, tmp_path, "second.cbz", seeds=[3])
    indexed(features, second)
    with files.library.connection() as db:
        assert db.execute("SELECT count(*) FROM page_band").fetchone()[0] == RADIUS + 1
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
        assert result.status_code == 204 and result.content == b""
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
    book = import_book(files, tmp_path, "a.cbz", seeds=[1, 2])
    (files.root.path / book.content.relative_path).write_bytes(b"changed")
    features = PageFeatures(files)
    assert features.index(limit=1)["processed"] == 0
    assert files.library.get_book(book.id).availability == "changed"


def test_undecodable_page_is_recorded_and_indexing_moves_on(files, tmp_path, monkeypatch):
    import pyvips

    from mangacouch import page_features

    book = import_book(files, tmp_path, "a.cbz", seeds=[1, 2, 3])
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
    assert bands == 2 * (RADIUS + 1) and not page_features.has_contrast(rows[1])
    assert features.status() == {"indexed_pages": 3, "total_pages": 3}
    assert features.index(limit=10)["processed"] == 0


def test_index_batches_by_limit_and_time_budget_and_status_counts_available_books(files, tmp_path):
    first = import_book(files, tmp_path, "a.cbz", seeds=[1, 2, 3, 4])
    import_book(files, tmp_path, "b.cbz", seeds=[5, 6])
    features = PageFeatures(files)
    assert features.index(limit=0 + 5)["processed"] == 5
    assert features.index(limit=100, time_budget=0)["processed"] == 0
    assert features.index(limit=100, time_budget=5)["processed"] == 1
    files.root.resolve(first.content.relative_path).unlink()
    with files.library.transaction() as db:
        db.execute("UPDATE book SET availability='missing' WHERE id=?", (first.id,))
    assert features.status() == {"indexed_pages": 2, "total_pages": 2}


@pytest.mark.perf
def test_feature_progress_is_indexed_and_rebuilt_after_delete(files, tmp_path):
    book = files.import_file(make_zip(tmp_path / "book.cbz")).book
    worker = PageFeatures(files)
    worker.index(limit=book.content.page_count)
    with files.library.transaction() as db:
        assert db.execute(PENDING, ("",)).fetchone() is None
        plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + PENDING, ("",)))
        assert "book_pending_features" in plan
        db.execute("DELETE FROM page_feature WHERE book_id=? AND page=0", (book.id,))
        assert db.execute(PENDING, ("",)).fetchone()[0] == book.id
    assert worker.index(limit=10)["processed"] == 1


def test_duplicate_groups_page_past_low_coverage_pairs(files, tmp_path, monkeypatch):
    left = import_book(files, tmp_path, "a.cbz", seeds=[1, 2, 3, 4])
    right = import_book(files, tmp_path, "b.cbz", seeds=[1, 2, 3, 4, 5])
    duplicates = Duplicates(PageFeatures(files))
    for _ in range(100):
        duplicates.poke()
        if not duplicates.run(budget=1.0):
            break
    # Weak pairs (3 shared pages of 30) fail the coverage filter and must not hide the real pair.
    weak = sorted(titled(files.library, f"weak{index}").id for index in range(3))
    with files.library.transaction() as db:
        for low, high in [(weak[0], weak[1]), (weak[0], weak[2]), (weak[1], weak[2])]:
            db.executemany(
                "INSERT INTO duplicate_edge VALUES(?,?,?,?)", ((low, high, page, page) for page in range(3))
            )
    monkeypatch.setattr("mangacouch.duplicates.PAIR_BATCH", 1)
    real = tuple(sorted((left.id, right.id)))
    first = duplicates.groups(limit=1)
    assert [(group["left_id"], group["right_id"]) for group in first["items"]] == [real]
    after, seen = first["after"], 1
    while after:
        page = duplicates.groups(limit=1, after=after)
        seen += len(page["items"])
        after = page["after"]
    assert seen == 1
    with pytest.raises(InvalidInput):
        duplicates.groups(after="no-tab")


def test_page_fingerprints_of_a_batch_are_stored_under_one_lock(files, tmp_path, monkeypatch):
    from mangacouch.page_features import PageFeatures

    files.import_file(make_zip(tmp_path / "pages.cbz", ["1.png", "2.png", "3.png"]))
    mutations = []
    mutation = files.root.mutation

    def counted(*args, **kwargs):
        mutations.append(1)
        return mutation(*args, **kwargs)

    monkeypatch.setattr(files.root, "mutation", counted)
    result = PageFeatures(files).index(limit=3)
    assert result == {"processed": 3}
    assert len(mutations) == 1
    assert PageFeatures(files).index()["processed"] == 0
