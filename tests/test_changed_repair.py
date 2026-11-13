"""Importing a book's original file again repairs a library file that changed on disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from helpers import CSRF, finish_scan, inbox, login, make_zip, python

from mangacouch.files import ContentChanged
from mangacouch.ingest import FileLibrary
from mangacouch.models import PersonalState
from mangacouch.scanning import Scanner


def changed_book(files: FileLibrary, tmp_path: Path) -> tuple[str, Path, bytes]:
    source = make_zip(tmp_path / "sample.zip")
    book = files.import_file(source).book
    files.library.save_personal(book.id, PersonalState(rating=4.0, resume_page=1), expected_revision=1)
    path = files.root.path / book.content.relative_path
    path.write_bytes(b"edited by another program")
    assert files.refresh_availability() == {book.id: "changed"}
    return book.id, source, source.read_bytes()


def test_reimporting_the_original_restores_it_and_keeps_the_changed_file(files: FileLibrary, tmp_path):
    book_id, source, original = changed_book(files, tmp_path)
    path = files.root.path / files.library.get_book(book_id).content.relative_path
    result = files.import_file(source)
    assert not result.created and result.book.id == book_id
    assert "changed_file_set_aside" in result.warnings
    assert path.read_bytes() == original
    aside = path.with_name(path.name + ".changed")
    assert aside.read_bytes() == b"edited by another program"
    book = files.library.get_book(book_id)
    assert book.personal.rating == 4.0 and book.content.relative_path == result.book.content.relative_path
    assert files.refresh_availability() == {book_id: "available"}
    assert files.page_bytes(book_id, 0)

    path.write_bytes(b"edited again")
    files.import_file(source)
    assert path.read_bytes() == original and aside.read_bytes() == b"edited by another program"
    assert path.with_name(path.name + ".2.changed").read_bytes() == b"edited again"
    files.recover()
    assert aside.exists() and path.with_name(path.name + ".2.changed").exists()


def test_indexing_a_duplicate_of_a_changed_book_is_still_refused(files: FileLibrary, tmp_path):
    book_id, _, original = changed_book(files, tmp_path)
    copy = files.root.path / "copy.zip"
    copy.write_bytes(original)
    with pytest.raises(ContentChanged):
        files.import_file(copy, index_existing=True)
    path = files.root.path / files.library.get_book(book_id).content.relative_path
    assert path.read_bytes() == b"edited by another program"
    assert not path.with_name(path.name + ".changed").exists()


def test_import_folder_scan_repairs_and_cleans_up_the_source(files: FileLibrary, tmp_path):
    book_id, _, original = changed_book(files, tmp_path)
    (inbox(files) / "again.zip").write_bytes(original)
    scanner = Scanner(files)
    result = finish_scan(scanner, scanner.start()["id"])
    [item] = result["items"]
    assert item["book_id"] == book_id and item["warnings"] == ["changed_file_set_aside"]
    assert not (files.root.path / "import/again.zip").exists()
    path = files.root.path / files.library.get_book(book_id).content.relative_path
    assert path.read_bytes() == original
    assert path.with_name(path.name + ".changed").exists()


def test_upload_repairs_a_changed_book(client, files: FileLibrary, tmp_path):
    book_id, _, original = changed_book(files, tmp_path)
    login(client)
    response = client.post(
        "/api/import",
        params={"filename": "sample.zip"},
        files=[("archive", ("upload.zip", original, "application/zip"))],
        headers=CSRF,
    )
    assert response.status_code == 200, response.text
    assert response.json()["book"]["id"] == book_id
    assert "changed_file_set_aside" in response.json()["warnings"]
    assert files.refresh_availability() == {book_id: "available"}


def test_cli_import_repairs_a_changed_book(tmp_path: Path):
    root = tmp_path / "books"
    root.mkdir()
    source = make_zip(tmp_path / "sample.zip")
    db = tmp_path / "library.sqlite"

    def run(*arguments: str) -> dict:
        completed = python("-m", "mangacouch", "--db", db, "--root", root, *arguments)
        assert completed.returncode == 0, completed.stdout
        return json.loads(completed.stdout)

    run("init")
    book = run("import", str(source))["book"]
    [path] = (path for path in root.rglob("*.cbz"))
    path.write_bytes(b"edited")
    repaired = run("import", str(source))
    assert repaired["book"]["id"] == book["id"] and "changed_file_set_aside" in repaired["warnings"]
    assert path.read_bytes() == source.read_bytes()
    assert path.with_name(path.name + ".changed").read_bytes() == b"edited"
