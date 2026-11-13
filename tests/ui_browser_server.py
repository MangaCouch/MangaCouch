"""Isolated, synthetic library for real-browser UI acceptance."""

from __future__ import annotations

import argparse
import json
import signal
import socket
import tempfile
import zipfile
from pathlib import Path

import httpx
import pyvips
import ui_showcase
import uvicorn
from helpers import save_metadata
from source_fixtures import SourceFixture

import mangacouch.http as web
from mangacouch.auth import Auth
from mangacouch.collections import Collections
from mangacouch.configuration import HTTPSettings
from mangacouch.database import Library
from mangacouch.downloads import Downloads
from mangacouch.files import LibraryRoot
from mangacouch.ingest import FileLibrary
from mangacouch.models import Metadata, PersonalState, Source, SourceRating, Tag
from mangacouch.source_config import SourceSettings
from mangacouch.source_http import SourceHTTP
from mangacouch.translations import Translations


def seed_fixture(directory: Path, library: Library, files: FileLibrary) -> SourceFixture:
    """The default acceptance library: 31 numbered books the browser tests count on."""
    Translations(library).import_data(
        {
            "version": 7,
            "head": {"sha": "synthetic-browser-fixture"},
            "data": [{"namespace": "parody", "data": {"blue archive": {"name": "碧蓝档案"}}}],
        }
    )
    for index in range(31):
        archive = directory / f"Fixture {index + 1:02}.cbz"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zipped:
            for page in range(22 if index == 0 else 3):
                color = ["#849a8b", "#728b9c", "#bd9478", "#9f94b4", "#a8ac84"][index % 5]
                svg = (
                    f'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="500">'
                    f'<rect width="360" height="500" fill="{color}"/>'
                    f'<circle cx="260" cy="120" r="90" fill="#ffffff22"/>'
                    f'<path d="M0 370 L120 190 L240 380 L360 270 L360 500 L0 500Z" fill="#26394055"/>'
                    f'<text x="25" y="440" font-size="28" fill="white">STORY {index + 1:02}</text>'
                    f'<text x="25" y="470" font-size="16" fill="white">PAGE {page + 1:02}</text>'
                    f"</svg>"
                )
                image = pyvips.Image.svgload_buffer(svg.encode())
                zipped.writestr(f"{page:03}.png", image.write_to_buffer(".png"))
        book = files.import_file(archive).book
        metadata = Metadata(
            title=f"山间来信 {index + 1:02}",
            original_title=f"Letters from the Hills {index + 1:02}",
            language=["chinese", "japanese", "english"][index % 3],
            work_type=["Manga", "Doujinshi", "Artist CG"][index % 3],
            tags=[
                Tag(
                    namespace="parody",
                    value=[
                        "blue archive",
                        "blue angel",
                        "blue sky",
                        "blue moon",
                        "blue road",
                        "blue sea",
                    ][index % 6],
                ),
                Tag(namespace="artist", value="fixture artist"),
            ],
            source_rating=SourceRating(average=4.5),
            # The first books link a source; the second also has a newer version upstream.
            source=Source(site="ehentai", gallery_id=100 + index, gallery_token="abcdef")
            if index < 3
            else None,
            newer_versions=[Source(site="ehentai", gallery_id=900, gallery_token="fedcba")]
            if index == 1
            else [],
        )
        save_metadata(library, book.id, metadata, expected_revision=book.metadata_revision)
        library.save_personal(
            book.id,
            PersonalState(
                resume_page=1 if index < 4 else None,
                completed=index == 7,
                rating=5.0 if index % 4 == 0 else None,
            ),
            expected_revision=1,
        )
    collections = Collections(library)
    collections.create("稍后阅读")
    collections.create("中文漫画", {"query": "", "options": {"language": "Chinese"}})
    return SourceFixture((directory / "Fixture 31.cbz").read_bytes())


class BrowserServer(uvicorn.Server):
    def handle_exit(self, sig: int, frame) -> None:
        # A process-group interrupt can also be forwarded by uv. Both copies request
        # the same graceful shutdown rather than forcing the lifespan task to cancel.
        self.should_exit = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8793)
    parser.add_argument(
        "--showcase",
        action="store_true",
        help="Seed the varied sample library used for the documentation screenshots",
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mangacouch-browser-") as folder:
        directory = Path(folder)
        root = directory / "books"
        root.mkdir()
        library = Library.initialize(directory / "library.sqlite")
        LibraryRoot.bind(library, root)
        files = FileLibrary(library, root)
        Auth(library).initialize("browser-fixture-only")
        if args.showcase:
            remote = ui_showcase.seed(directory, library, files)
        else:
            remote = seed_fixture(directory, library, files)
        source_http = SourceHTTP(SourceSettings(), {}, transport=httpx.MockTransport(remote))
        downloads = Downloads(files, http=source_http)
        listener = socket.socket()
        listener.bind(("127.0.0.1", args.port))
        listener.listen()
        args.port = listener.getsockname()[1]
        print(json.dumps({"url": f"http://127.0.0.1:{args.port}", "fixture": str(directory)}), flush=True)
        server = BrowserServer(
            uvicorn.Config(
                web.create_app(
                    files, HTTPSettings(public_origin=f"http://127.0.0.1:{args.port}"), downloads=downloads
                ),
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                timeout_keep_alive=2,
            )
        )
        try:
            server.run(sockets=[listener])
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            listener.close()


if __name__ == "__main__":
    main()
