"""Synthetic source responses; no credentials, purchased archives or real gallery content."""

from __future__ import annotations

import io
import json
import zipfile

import httpx

EH = "https://e-hentai.org/g/100/abcdef/"
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
        self.balance = 5000
        self.purchased: set[str] = set()
        self.charge_before_error = False
        """EH shows an original archive already bought as free."""
        self.download_error = False
        self.comments_error = False
        # `reusable`: every archiver page offers a prepared link; `prepared`: galleries requested here.
        # A page that offers a link shows no prices unless `priced_link` is set.
        self.reusable = False
        self.prepared: set[str] = set()
        self.priced_link = False
        # How the archive server answers: a web page instead of the archive (`archive_page`), the
        # archive under another Content-Length (`declared_length`) or without one (`stream_archive`).
        self.archive_page: str | None = None
        self.declared_length: int | None = None
        self.stream_archive = False
        self.tags = ["language:english", "artist:Example"]
        self.title = "Synthetic source book"
        self.comment_body = "Fixture comment"
        self.removed: set[int] = set()

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
                gallery = request.url.params["gid"]
                mode = "res" if b"dltype=res" in request.content else "org"
                cost = 0 if gallery in self.purchased else self.cost if mode == "org" else 0
                if self.download_error and not self.charge_before_error:
                    raise httpx.ReadTimeout("fixture timeout before charge")
                if self.balance < cost:
                    return httpx.Response(200, text="Insufficient Funds")
                self.balance -= cost
                self.purchased.add(gallery)
                if self.download_error:
                    raise httpx.ReadTimeout("fixture timeout after charge")
                self.prepared.add(gallery)
                return httpx.Response(200, text=f'<a href="{ARCHIVE}">Download</a>')
            offered = self.reusable or request.url.params["gid"] in self.prepared
            link = f'<a href="{ARCHIVE}">Download</a>' if offered else ""
            if offered and not self.priced_link:
                return httpx.Response(200, text=link)
            original = "Free!" if request.url.params["gid"] in self.purchased else f"{self.cost} GP"
            return httpx.Response(
                200,
                text=f"""{link}<p>Current Funds: {self.balance:,} GP</p>
              <div>Download Cost: {original}<input name="dltype" value="org"></div>
              <div>Download Cost: Free!<input name="dltype" value="res"></div>""",
            )
        if request.url.host == "archive.example":
            if self.archive_page is not None:
                return httpx.Response(
                    200, text=self.archive_page, headers={"content-type": "text/html; charset=utf-8"}
                )
            headers = {"content-type": "application/zip", "etag": '"fixture-v1"'}
            if self.stream_archive:
                return httpx.Response(200, stream=httpx.ByteStream(self.payload), headers=headers)
            if self.declared_length is not None:
                headers["content-length"] = str(self.declared_length)
            return httpx.Response(200, content=self.payload, headers=headers)
        if request.url.host == "covers.example":
            from helpers import png

            return httpx.Response(200, content=png())
        if path.startswith("/g/"):
            if self.comments_error:
                return httpx.Response(403)
            if "nw=1" not in request.headers.get("cookie", ""):
                return httpx.Response(200, text=CONTENT_WARNING)
            return httpx.Response(
                200,
                text=f"""<div id="cdiv"><div class="c1">
                <div class="c3">Posted on 14 November 2023, 22:13 by: <a>Example</a></div>
                <div class="c5">Score: +3</div>
                <div class="c6" id="comment_1">{self.comment_body}</div></div></div>""",
            )
        raise AssertionError(f"Unexpected source request: {request.method} {url}")


EX = EH.replace("e-hentai.org", "exhentai.org")


CONTENT_WARNING = """<html><body><div class="d"><strong>Content Warning</strong>
<p>This gallery has been flagged as <strong>Offensive For Everyone</strong>.</p>
<p><a href="?nw=session">View Gallery</a> <a href="?nw=always">Never Warn Me Again</a></p>
</div></body></html>"""
"""What E(x)Hentai shows instead of a flagged gallery's page until the nw cookie is sent."""


TOO_FAST = (
    b"<html><body>You are opening pages too fast, thus placing a heavy load on the server. "
    b"Back down, or your IP address will be automatically banned.</body></html>"
)


def archiver_posts(remote) -> list[httpx.Request]:
    """E-Hentai archive requests: the only requests that may spend GP."""
    return [r for r in remote.requests if r.method == "POST" and r.url.path == "/archiver.php"]


def end_halts(http) -> None:
    """Time passes: every halt ends and the usage bucket the rate limits filled drains again."""
    with http.lock:
        http.halts.clear()
        http.paused_until = 0.0
        http.bucket.level = 0.0
