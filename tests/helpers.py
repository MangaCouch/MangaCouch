from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pyvips
from fastapi.testclient import TestClient
from source_fixtures import EH

from mangacouch.database import Book, Library
from mangacouch.errors import InvalidInput
from mangacouch.ingest import FileLibrary
from mangacouch.metadata_import import MetadataImport
from mangacouch.models import Content, Metadata, NativeSidecar, Source, Tag
from mangacouch.review import Review
from mangacouch.sidecars import SidecarSync

PASSCODE = "fixture-only-passcode"
# Tags as they may be typed or found in an old library, the canonical tags they are stored as, and
# queries (shown namespace names, stray whitespace) that must find the book carrying them.
TYPED_TAGS = [
    {"namespace": "作者", "value": "蓝天"},
    {"namespace": "artist", "value": " 蓝天 "},
    {"namespace": "社团", "value": "blue\n  sky"},
    {"namespace": "other", "value": "foo "},
    {"namespace": "other", "value": "foo"},
    {"namespace": "other", "value": "   "},
    {"namespace": "coser", "value": "x"},
    {"namespace": "Custom", "value": "kept"},
]
CANONICAL_TAGS = [
    ("artist", "蓝天"),
    ("cosplayer", "x"),
    ("Custom", "kept"),
    ("group", "blue sky"),
    ("other", "foo"),
]
TAG_QUERIES = [
    '作者:"蓝天"',
    "artist:蓝天",
    '社团:"blue sky"',
    'group:"blue  sky"',
    "coser:x",
    'custom:"kept"',
]
ORIGIN = "http://testserver"
CSRF = {"Origin": ORIGIN, "X-MangaCouch-Request": "1"}
SUBPROCESS_ENV = {
    **os.environ,
    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    "PYTHONDONTWRITEBYTECODE": "1",
}
# Prepended to a child script: crash_after(owner, "name", status) makes the process exit abruptly
# (no cleanup, no finally blocks) right after the nth call of owner.name that `ignore` does not skip.
CRASH_AFTER = """
import os


def crash_after(owner, name, status, *, nth=1, run_original=True, ignore=lambda *args: False):
    original = getattr(owner, name)
    calls = 0

    def crash(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs) if run_original else None
        if not ignore(*args):
            calls += 1
            if calls == nth:
                os._exit(status)
        return result

    setattr(owner, name, crash)
"""


def python(*arguments: object, **options: Any) -> subprocess.CompletedProcess[str]:
    """Run the source tree in a fresh interpreter, e.g. python("-c", code) or python("-m", "mangacouch")."""
    return subprocess.run(
        [sys.executable, "-B", *map(str, arguments)],
        env=SUBPROCESS_ENV,
        capture_output=True,
        text=True,
        timeout=20,
        **options,
    )


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def start_test_server(
    command,
    *,
    headers: dict[str, str] | Callable[[int], dict[str, str]] | None = None,
    path="/api/auth/me",
    **options,
):
    """Retry the bind race for CLI servers that require a concrete public port."""
    for attempt in range(3):
        port = free_port()
        process = subprocess.Popen(command(port), stderr=subprocess.PIPE, text=True, **options)
        try:
            response = wait_until_serving(
                process,
                f"http://127.0.0.1:{port}{path}",
                headers=headers(port) if callable(headers) else headers,
            )
            return process, port, response
        except AssertionError:
            if process.poll() is None:
                process.kill()
            stdout, stderr = process.communicate(timeout=10)
            detail = (stdout or "") + (stderr or "")
            if attempt == 2 or not any(
                word in detail.lower()
                for word in ("port_in_use", "address already in use", "errno 48", "errno 98")
            ):
                raise AssertionError(detail or "Server failed to start") from None
    raise AssertionError("Server could not bind after three attempts")


def wait_until_serving(
    process: subprocess.Popen, url: str, *, headers: dict[str, str] | None = None, timeout: float = 30
) -> httpx.Response:
    """The first response from a server process that is still starting; fails if the process exits."""
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=1) as client:
        while True:
            try:
                return client.get(url, headers=headers)
            except httpx.TransportError:
                if process.poll() is not None:
                    raise AssertionError(f"Server process exited with {process.returncode}") from None
                if time.monotonic() > deadline:
                    raise AssertionError(f"{url} did not start serving") from None
                time.sleep(0.05)


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
    identifier = import_book(files, tmp_path).id
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


def import_book(
    files: FileLibrary,
    tmp_path: Path,
    name: str = "sample",
    pages: list[str] | None = None,
    *,
    seeds: list[int] | None = None,
    eze: bool = False,
) -> Book:
    """Import a zip named after `name`: solid-colour `pages` (default one page) or noise pages from `seeds`.

    `eze` pairs the archive with the E-Hentai metadata fixture.
    """
    path = tmp_path / (name if Path(name).suffix else f"{name}.zip")
    if seeds is not None:
        with zipfile.ZipFile(path, "w") as z:
            for index, seed in enumerate(seeds):
                z.writestr(f"{index:03}.png", sample(seed))
    else:
        make_zip(path, pages or [f"{Path(name).stem}.png"])
    if eze:
        path.with_suffix(".eze.json").write_bytes((Path(__file__).parent / "fixtures/eze.json").read_bytes())
    return files.import_file(path).book


def indexed(visual, book):
    for _ in range(book.content.page_count):
        visual.index()


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


def synced(result: dict) -> dict[str, dict]:
    """The results of a sidecar sync batch by book id."""
    return {item["book_id"]: item for item in result["items"]}


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
        format="cbz",
        relative_path=filename,
        original_filename=filename,
        byte_size=300,
        page_count=30,
    )


def action(queue, job, command, **kw):
    latest = queue.get(job["id"])
    return queue.action(latest["id"], command, latest["revision"], **kw)


def ready(queue, url=EH):
    job = queue.create(url)
    assert download_step(queue)
    job = queue.get(job["id"])
    assert job["state"] == "needs_approval", job
    return job


def finish_download(queue, job):
    action(queue, job, "approve")
    drive(queue)
    return queue.get(job["id"])


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


def register_books(
    library: Library, contents: Iterable[Content], metadata: Metadata | None = None
) -> list[Book]:
    """Register many books in one transaction. A connection per book is slow on Windows: with no
    other connection open, each close checkpoints and removes the WAL files."""
    metadata = metadata or Metadata()
    with library.transaction() as db:
        ids = [library.register_record(db, item, metadata, None)[0] for item in contents]
    with library.connection() as db:
        return [library.get_book(book_id, db) for book_id in ids]


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


def titled(library: Library, title: str, *tags: Tag):
    return register_book(library, content(title, f"{title}.zip"), Metadata(title=title, tags=list(tags)))[0]


def raw_zip(path: Path, members: list[tuple[bytes, bytes]], *, extra: dict[int, bytes] | None = None) -> Path:
    """A ZIP whose member names are exactly the given bytes: ASCII placeholders of equal length
    are written by zipfile and then replaced in the local and central headers. A non-ASCII
    placeholder (UTF-8 flag set) is kept when the raw name starts with b"\\xff"."""
    placeholders = []
    with zipfile.ZipFile(path, "w") as archive:
        for index, (raw, data) in enumerate(members):
            placeholder = f"~{index:03}~".encode().ljust(len(raw), b"~")
            assert len(placeholder) == len(raw)
            info = zipfile.ZipInfo(placeholder.decode())
            if extra and index in extra:
                info.extra = extra[index]
            archive.writestr(info, data)
            placeholders.append(placeholder)
    blob = path.read_bytes()
    for placeholder, (raw, _) in zip(placeholders, members, strict=True):
        assert blob.count(placeholder) == 2
        blob = blob.replace(placeholder, raw)
    path.write_bytes(blob)
    return path


def jpeg(width: int = 40, height: int = 80, *, orientation: int | None = None, seed: int = 1) -> bytes:
    values = np.random.default_rng(seed).integers(0, 256, size=(height // 8, width // 8, 3), dtype=np.uint8)
    image: Any = pyvips.Image.new_from_memory(values.tobytes(), width // 8, height // 8, 3, "uchar")
    image = image.resize(8, kernel="nearest")
    if orientation is not None:
        image = image.copy()
        image.set_type(pyvips.GValue.gint_type, "orientation", orientation)
    return image.jpegsave_buffer(Q=95)


def oriented_book(files, tmp_path, name="rotated.cbz", orientation=6):
    path = tmp_path / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("001.jpg", jpeg(orientation=orientation, seed=7))
        archive.writestr("002.jpg", jpeg(seed=8))
        archive.writestr("003.png", png(3))
        archive.comment = name.encode()  # distinct content per book
    return files.import_file(path).book


def manifest(files, book_id) -> list[dict]:
    with files.library.connection() as db:
        return json.loads(
            db.execute("SELECT pages_json FROM archive_manifest WHERE book_id=?", (book_id,)).fetchone()[0]
        )


def download_step(service) -> bool:
    return service.step("meta") or service.step("transfer")


def drive(service) -> None:
    """Alternate the two production pools until neither has runnable work."""
    while True:
        meta = service.step("meta")
        transfer = service.step("transfer")
        if not meta and not transfer:
            return


def hash_outside_operation_lock(files, monkeypatch) -> list:
    """Make every archive's manifest stamp stale (forcing a rehash) and fail if any archive is
    hashed while the operation lock is held. Returns the list of hashed paths."""
    from contextlib import contextmanager

    from mangacouch import ingest

    with files.library.transaction() as db:
        db.execute("UPDATE archive_manifest SET file_stamp='[0,0]'")
    held, hashed = [], []
    mutation, original = files.root.mutation, ingest.hash_file

    @contextmanager
    def tracked(*args, **kwargs):
        with mutation(*args, **kwargs) as value:
            held.append(True)
            try:
                yield value
            finally:
                held.pop()

    def hash_file(path, *args, **kwargs):
        assert not held, "archive hashed under the operation lock"
        hashed.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(files.root, "mutation", tracked)
    monkeypatch.setattr(ingest, "hash_file", hash_file)
    return hashed
