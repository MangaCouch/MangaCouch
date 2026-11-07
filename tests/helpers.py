from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pyvips
from fastapi.testclient import TestClient
from source_fixtures import EH

from mangacouch.auth import Auth
from mangacouch.database import Book, Library
from mangacouch.errors import InvalidInput
from mangacouch.http import HTTPSettings, create_app
from mangacouch.ingest import FileLibrary
from mangacouch.metadata_import import MetadataImport
from mangacouch.models import Content, Metadata, NativeSidecar, Source, Tag
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync

PASSCODE = "fixture-only-passcode"
ORIGIN = "http://testserver"
CSRF = {"Origin": ORIGIN, "X-MangaCouch-Request": "1"}


def native_file(archive: Path, metadata: Metadata | None = None) -> tuple[Path, NativeSidecar]:
    native = NativeSidecar(
        schema="mangacouch.native/1",
        content_sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
        revision=17,
        metadata=metadata
        or Metadata(
            title="本地标题",
            original_title="Original title",
            language="中文",
            translated=True,
            tags=[Tag(namespace="artist", value="Test artist")],
            source=Source(site="ehentai", gallery_id=123, gallery_token="abc123"),
            protected_fields=["title", "uploader"],
            field_sources={"title": "manual"},
        ),
    )
    path = archive.with_name(archive.name + ".mangacouch.json")
    path.write_bytes(native.canonical_bytes())
    return path, native


def confirmed(files: FileLibrary, tmp_path: Path, metadata: Metadata | None = None):
    identifier = book_id(files, tmp_path)
    service = Review(files)
    book = service.edit(
        identifier, metadata or Metadata(title="Known title", language="English"), expected_revision=1
    )
    preview = service.preview(identifier)
    book = service.complete(identifier, preview["token"], preview["issues"])
    return service, book


def sample(seed: int) -> bytes:
    values = np.random.default_rng(seed).integers(0, 256, size=(80, 64, 3), dtype=np.uint8)
    return pyvips.Image.new_from_memory(values.tobytes(), 64, 80, 3, "uchar").pngsave_buffer()


def archive(files, tmp_path, name, seeds):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as z:
        for index, seed in enumerate(seeds):
            z.writestr(f"{index:03}.png", sample(seed))
    return files.import_file(path).book


def indexed(visual, book):
    for _ in range(book.content.page_count):
        visual.index(book.id)


def book_id(files: FileLibrary, tmp_path: Path, *, eze: bool = False) -> str:
    source = make_zip(tmp_path / "sample.zip")
    if eze:
        source.with_suffix(".eze.json").write_bytes(
            (Path(__file__).parent / "fixtures/eze.json").read_bytes()
        )
    return files.import_file(source).book.id


def login(client: TestClient) -> str:
    response = client.post(
        "/api/auth/login", json={"passcode": PASSCODE, "label": "fixture device"}, headers=CSRF
    )
    assert response.status_code == 200
    assert PASSCODE not in response.text and "mcs_" not in response.text
    return response.json()["id"]


def png(red: int = 100) -> bytes:
    image: Any = pyvips.Image.black(12, 8).new_from_image([red, 50, 10])
    return image.write_to_buffer(".png")


def make_zip(path: Path, names: list[str] | None = None) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for index, name in enumerate(names or ["10.png", "2.png", "1.png"]):
            archive.writestr(name, png(20 + index))
    return path


def manager_client(files):
    auth = Auth(files.library)
    auth.initialize(PASSCODE)
    _, token = auth.create_key("management", "audit")
    return TestClient(
        create_app(files, HTTPSettings(public_origin="http://testserver"), auth=auth),
        headers={"Authorization": f"Bearer {token}"},
    )


def add(sync: SidecarSync, tmp_path: Path, name: str = "sample") -> str:
    return sync.files.import_file(make_zip(tmp_path / f"{name}.zip", [f"{name}.png"])).book.id


def edit(sync: SidecarSync, book_id: str, title: str, language: str = "en") -> None:
    book = sync.library.get_book(book_id)
    save_metadata(
        sync.library,
        book_id,
        Metadata(title=title, language=language),
        expected_revision=book.metadata_revision,
    )


def external(sync: SidecarSync, book_id: str, title: str, language: str = "ja") -> bytes:
    native = sync.library.native_sidecar(book_id)
    native.metadata = Metadata(title=title, language=language)
    data = native.canonical_bytes()
    sync.path(sync.library.get_book(book_id)).write_bytes(data)
    return data


def content(seed: str = "first", filename: str = "原名.zip") -> Content:
    return Content(
        sha256=hashlib.sha256(seed.encode()).hexdigest(),
        format="zip",
        relative_path=filename,
        original_filename=filename,
        byte_size=300,
        page_count=30,
    )


def action(queue, job, command, **kw):
    latest = queue.get(job["id"])
    return queue.action(latest["id"], command, latest["revision"], **kw)


def ready(queue, url=EH, entry="find_eh"):
    job = queue.create(url, entry)
    assert queue.step()
    job = queue.get(job["id"])
    assert job["state"] == "needs_approval", job
    return job


def finish_download(queue, job):
    action(queue, job, "approve")
    for _ in range(5):
        if not queue.step():
            break
    return queue.get(job["id"])


def book(files: FileLibrary, tmp_path: Path, name: str = "test"):
    return files.import_file(make_zip(tmp_path / f"{name}.zip", [f"{name}.png"])).book


def inbox(files):
    path = files.root.path / "import"
    path.mkdir(exist_ok=True)
    return path


def finish_scan(scanner, identifier):
    for _ in range(100):
        if scanner.step(identifier, limit=10)["state"] != "active":
            return scanner.get(identifier)
    raise AssertionError("Scan did not finish")


def parts(tmp_path, mode="both", name="作品😀.cbz"):
    archive = make_zip(tmp_path / "sample.cbz")
    sidecar, _ = native_file(archive)
    transport_name = "upload" + Path(name).suffix.lower()
    result: list[tuple[str, tuple[str | None, bytes, str]]] = [
        ("archive", (transport_name, archive.read_bytes(), "application/zip"))
    ]
    if mode in {"native", "both"}:
        result.append(
            ("native", (transport_name + ".mangacouch.json", sidecar.read_bytes(), "application/json"))
        )
    if mode in {"eze", "both"}:
        eze = json.dumps({"gallery_info": {"title": "Eze title"}, "archiver_key": "MUST_NOT_RETAIN"}).encode()
        result.append(("eze", (transport_name + ".eze.json", eze, "application/json")))
    return result


def register_book(
    library: Library,
    content: Content,
    metadata: Metadata | None = None,
    *,
    source_import: MetadataImport | None = None,
) -> tuple[Book, bool]:
    if source_import is not None:
        if metadata is not None:
            raise InvalidInput("Supply either metadata or an import report")
        metadata = source_import.metadata
    metadata = metadata or Metadata()
    with library.transaction() as db:
        book_id, created = library.register_record(db, content, metadata, source_import)
    return library.get_book(book_id), created


def save_metadata(library: Library, book_id: str, metadata: Metadata, *, expected_revision: int) -> Book:
    with library.transaction() as db:
        library.write_metadata(db, book_id, metadata, expected_revision)
    return library.get_book(book_id)


def pending_sidecars(library: Library) -> list[tuple[str, int]]:
    with library.connection() as db:
        rows = db.execute(
            "SELECT book_id,pending_revision FROM sidecar_sync "
            "WHERE pending_revision>synced_revision ORDER BY book_id"
        ).fetchall()
    return [(row["book_id"], row["pending_revision"]) for row in rows]
