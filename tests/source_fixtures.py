"""Synthetic source responses; no credentials, purchased archives or real gallery content."""

from __future__ import annotations

import io
import json
import time
import zipfile

import httpx

EH = "https://e-hentai.org/g/100/abcdef/"
NH = "https://nhentai.net/g/200/"
ARCHIVE = "https://archive.example/archive/example?token=fixture"


class SourceFixture:
    def __init__(self, payload: bytes):
        self.payload = payload
        if zipfile.is_zipfile(io.BytesIO(payload)):
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                self.gallery_bytes = sum(
                    entry.file_size for entry in archive.infolist() if entry.filename.endswith(".png")
                )
        else:
            self.gallery_bytes = len(payload)
        self.requests: list[httpx.Request] = []
        self.cost = 25
        self.download_error = False
        self.comments_error = False
        self.search_error = False
        self.reusable = False
        self.tags = ["language:english", "artist:Example"]
        self.title = "Synthetic source book"
        self.comment_body = "Fixture comment"
        self.removed: set[int] = set()
        self.search_links = [EH]

    def __call__(self, request: httpx.Request):
        self.requests.append(request)
        url, path = str(request.url), request.url.path
        if path == "/api.php":
            return httpx.Response(
                200,
                json={
                    "gmetadata": [
                        {
                            "gid": gallery_id,
                            "token": token,
                            "title": self.title,
                            "title_jpn": "",
                            "tags": self.tags,
                            "category": "Manga",
                            "uploader": "Example",
                            "posted": 1700000000,
                            "rating": "4.5",
                            "filecount": "3",
                            "filesize": str(self.gallery_bytes),
                            "thumb": "https://covers.example/cover.png",
                        }
                        if gallery_id not in self.removed
                        else {"gid": gallery_id, "error": "Key missing, or incorrect key provided."}
                        for gallery_id, token in json.loads(request.content)["gidlist"]
                    ]
                },
            )
        if path == "/archiver.php":
            if request.method == "POST":
                if self.download_error:
                    raise httpx.ReadTimeout("fixture timeout; token must not escape")
                self.reusable = True
                return httpx.Response(200, text=f'<a href="{ARCHIVE}">Download</a>')
            if self.reusable:
                return httpx.Response(200, text=f'<a href="{ARCHIVE}">Download</a>')
            return httpx.Response(
                200,
                text=f"""<p>Current Funds: 5,000 GP</p>
              <div>Download Cost: {self.cost} GP<input name="dltype" value="org"></div>
              <div>Download Cost: Free!<input name="dltype" value="res"></div>""",
            )
        if path == "/api/v2/config":
            return httpx.Response(
                200, json={"thumb_servers": ["t.nhentai.net"], "image_servers": ["i.nhentai.net"]}
            )
        if path == "/api/v2/galleries/200":
            return httpx.Response(
                200,
                json={
                    "id": 200,
                    "media_id": "1",
                    "title": {"english": self.title},
                    "tags": [{"type": "language", "name": "english"}],
                    "num_pages": 3,
                    "upload_date": 1700000000,
                    "cover": {"path": "galleries/1/cover.jpg", "width": 600, "height": 900},
                },
            )
        if path == "/api/v2/galleries/200/comments":
            if self.comments_error:
                return httpx.Response(429, headers={"retry-after": "5"})
            return httpx.Response(
                200,
                json={
                    "result": [
                        {
                            "id": 1,
                            "gallery_id": 200,
                            "poster": {"username": "Example"},
                            "body": self.comment_body,
                            "post_date": 1700000001,
                        }
                    ],
                    "num_pages": 1,
                },
            )
        if path == "/api/v2/galleries/200/download":
            if self.download_error:
                raise httpx.ReadTimeout("fixture timeout")
            return httpx.Response(200, json={"url": ARCHIVE, "expires_at": int(time.time()) + 3600})
        if request.url.host == "archive.example":
            return httpx.Response(
                200, content=self.payload, headers={"content-type": "application/zip", "etag": '"fixture-v1"'}
            )
        if request.url.host == "covers.example":
            from helpers import png

            return httpx.Response(200, content=png())
        if path == "/" and request.url.host == "e-hentai.org":
            if self.search_error:
                return httpx.Response(503)
            links = "".join(f'<a href="{link}">Fixture</a>' for link in self.search_links)
            return httpx.Response(200, text=f'<table class="itg">{links}</table>')
        if path.startswith("/g/"):
            if self.comments_error:
                return httpx.Response(403)
            return httpx.Response(
                200,
                text=f"""<div id="cdiv"><div class="c1">
                <div class="c3">Posted on 14 November 2023, 22:13 by: <a>Example</a></div>
                <div class="c5">Score: +3</div>
                <div class="c6" id="comment_1">{self.comment_body}</div></div></div>""",
            )
        raise AssertionError(f"Unexpected source request: {request.method} {url}")
