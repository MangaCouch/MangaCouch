from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from helpers import make_zip

from mangacouch.archives import Limits
from mangacouch.auth import Auth
from mangacouch.configuration import HTTPSettings
from mangacouch.http import create_app
from mangacouch.ingest import FileLibrary
from mangacouch.lifecycle import initialize_library


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="mangacouch-web-integration-") as directory:
        path = Path(directory)
        root = path / "books"
        root.mkdir()
        library = initialize_library(path / "library.sqlite", root, Limits())
        Auth(library).initialize("integration-fixture-only")
        sample = make_zip(path / "sample.cbz").read_bytes()
        app = create_app(FileLibrary(library, root), HTTPSettings(public_origin="http://integration.test"))
        with TestClient(app, base_url="http://integration.test") as client:
            print(json.dumps({"ready": True, "archive": base64.b64encode(sample).decode()}), flush=True)
            for line in sys.stdin:
                data = json.loads(line)
                response = client.request(
                    data["method"],
                    data["path"],
                    headers=data["headers"],
                    content=base64.b64decode(data["body"]),
                )
                print(
                    json.dumps(
                        {
                            "id": data["id"],
                            "status": response.status_code,
                            "headers": dict(response.headers),
                            "body": base64.b64encode(response.content).decode(),
                        }
                    ),
                    flush=True,
                )


if __name__ == "__main__":
    main()
