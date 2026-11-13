from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

import anyio
from fastapi import APIRouter, FastAPI, Request, Response

from .archives import ThumbnailFormat, image_media_type
from .collections import Collections
from .cursors import encode_cursor
from .errors import InvalidInput, NotFound
from .exports import ArchiveResponse
from .ingest import FileLibrary
from .models import protocol_progress
from .preferences import load_preferences, preferred_title
from .services import CLIENT_IMAGE, image_response

ATOM = "http://www.w3.org/2005/Atom"
PSE = "http://vaemendis.net/opds-pse/ns"
SEARCH = "http://a9.com/-/spec/opensearch/1.1/"
NAV = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ = "application/atom+xml;profile=opds-catalog;kind=acquisition"
ET.register_namespace("", ATOM)
ET.register_namespace("pse", PSE)


def xml_text(value: str) -> str:
    return "".join(
        c
        for c in value
        if c in "\n\r\t"
        or 0x20 <= ord(c) <= 0xD7FF
        or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF
    )


def text_element(parent: ET.Element, name: str, text: str) -> ET.Element:
    item = ET.SubElement(parent, f"{{{ATOM}}}{name}")
    item.text = xml_text(text)
    return item


def link(parent: ET.Element, href: str, rel: str, mime: str, **attributes) -> ET.Element:
    return ET.SubElement(parent, f"{{{ATOM}}}link", {"href": href, "rel": rel, "type": mime, **attributes})


def install_opds(
    app: FastAPI,
    files: FileLibrary,
    origin: str,
    cache,
    limiter,
    cover_limiter,
    *,
    prefix: str = "/opds",
    komga: bool = False,
):
    router = APIRouter(prefix=prefix)
    collections = Collections(files.library)
    # Links are root-relative, so a client that reached the server by another allowed host (a LAN
    # address, say) keeps using it; only the Atom ids use the configured origin.

    def xml(element: ET.Element, mime: str, request: Request) -> Response:
        for item in element.iter():
            for key, value in item.attrib.items():
                item.set(key, xml_text(value))
        data = ET.tostring(element, encoding="utf-8", xml_declaration=True)
        return Response(
            data if request.method == "GET" else b"",
            media_type=mime,
            headers={"Content-Length": str(len(data)), "Vary": "Authorization, Cookie"},
        )

    def feed(title: str, request: Request, mime: str = NAV) -> ET.Element:
        result = ET.Element(f"{{{ATOM}}}feed")
        text_element(
            result, "id", origin + request.url.path + ("?" + request.url.query if request.url.query else "")
        )
        text_element(result, "title", title)
        text_element(result, "updated", datetime.now(UTC).isoformat())
        author = ET.SubElement(result, f"{{{ATOM}}}author")
        text_element(author, "name", "Komga" if komga else "MangaCouch")
        text_element(result, "generator", "MangaCouch Komga adapter" if komga else "MangaCouch")
        link(result, prefix, "start", NAV)
        link(result, request.url.path + ("?" + request.url.query if request.url.query else ""), "self", mime)
        link(result, prefix + "/search.xml", "search", "application/opensearchdescription+xml")
        return result

    def navigation(parent: ET.Element, title: str, path: str, mime: str):
        entry = ET.SubElement(parent, f"{{{ATOM}}}entry")
        text_element(entry, "id", origin + path)
        text_element(entry, "title", title)
        text_element(entry, "updated", datetime.now(UTC).isoformat())
        text_element(entry, "content", title)
        link(entry, path, "subsection", mime)

    def stream_type(identifier: str, db) -> str | None:
        row = db.execute("SELECT pages_json FROM archive_manifest WHERE book_id=?", (identifier,)).fetchone()
        pages = json.loads(row[0]) if row else []
        return manifest_type(pages)

    def manifest_type(pages) -> str | None:
        types = {page["media_type"] for page in pages}
        if len(types) == 1 and types <= {"image/jpeg", "image/png", "image/gif"}:
            return types.pop()
        return None

    def acquisitions(parent: ET.Element, ids: list[str]):
        preference = load_preferences(files.library).settings.title_preference
        with files.library.connection() as db:
            details = {
                row["id"]: (manifest_type(json.loads(row["pages_json"] or "[]")), row["updated_at"])
                for row in db.execute(
                    "SELECT b.id,m.pages_json,a.updated_at FROM book b "
                    "LEFT JOIN archive_manifest m ON m.book_id=b.id "
                    "LEFT JOIN reading_activity a ON a.book_id=b.id "
                    "WHERE b.id IN (SELECT value FROM json_each(?))",
                    (json.dumps(ids),),
                )
            }
            for book in files.library.get_books(ids, db):
                acquisition(parent, book, preference, *details[book.id])

    def acquisition(parent: ET.Element, book, preference, media_type, updated_at):
        identifier = book.id
        title = preferred_title(book, preference)
        entry = ET.SubElement(parent, f"{{{ATOM}}}entry")
        text_element(entry, "id", identifier if komga else f"urn:uuid:{identifier}")
        text_element(entry, "title", title)
        text_element(entry, "updated", book.metadata.uploaded_at or "1970-01-01T00:00:00Z")
        text_element(entry, "content", f"{book.content.page_count} pages")
        for tag in book.metadata.tags:
            ET.SubElement(entry, f"{{{ATOM}}}category", {"term": f"{tag.namespace}:{tag.value}"})
        for rel in ("http://opds-spec.org/image", "http://opds-spec.org/image/thumbnail"):
            link(entry, f"{prefix}/books/{identifier}/cover", rel, "image/jpeg")
        mime = "application/pdf" if book.content.format == "pdf" else "application/zip"
        link(entry, f"{prefix}/books/{identifier}/archive", "http://opds-spec.org/acquisition", mime)
        if media_type is None:
            return
        progress = {f"{{{PSE}}}count": str(book.content.page_count)}
        page, _ = protocol_progress(book.personal, book.content.page_count)
        if page is not None:
            progress[f"{{{PSE}}}lastRead"] = str(page)
            if updated_at is not None:
                progress[f"{{{PSE}}}lastReadDate"] = datetime.fromtimestamp(updated_at, UTC).isoformat()
        link(
            entry,
            f"{prefix}/books/{identifier}/pages/{{pageNumber}}",
            "http://vaemendis.net/opds-pse/stream",
            media_type,
            **progress,
        )

    @router.api_route("", methods=["GET", "HEAD"])
    @router.api_route("/catalog", methods=["GET", "HEAD"])
    @router.api_route("/", methods=["GET", "HEAD"])
    def root(request: Request) -> Response:
        result = feed("MangaCouch", request)
        navigation(result, "All books / 全部本子", prefix + "/books", ACQ)
        navigation(result, "Collections / 合集", prefix + "/collections", NAV)
        return xml(result, NAV, request)

    @router.api_route("/search.xml", methods=["GET", "HEAD"])
    def search(request: Request) -> Response:
        result = ET.Element("OpenSearchDescription", xmlns=SEARCH)
        ET.SubElement(result, "ShortName").text = "MangaCouch"
        ET.SubElement(result, "Description").text = "Search titles and tags"
        ET.SubElement(
            result,
            "Url",
            {"type": ACQ, "template": prefix + "/books?query={searchTerms}"},
        )
        return xml(result, "application/opensearchdescription+xml", request)

    @router.api_route("/books", methods=["GET", "HEAD"])
    def books(request: Request, query: str = "", after_id: str = "", limit: int = 30) -> Response:
        ids = files.library.search(query, after_id=after_id, limit=limit)
        result = feed("Books / 本子", request, ACQ)
        acquisitions(result, ids)
        if len(ids) == limit:
            link(
                result,
                prefix + "/books?" + urlencode({"query": query, "after_id": ids[-1], "limit": limit}),
                "next",
                ACQ,
            )
        return xml(result, ACQ, request)

    @router.api_route("/collections", methods=["GET", "HEAD"])
    def groups(request: Request, after: str = "", limit: int = 30) -> Response:
        page = collections.list(limit=limit, after=after)
        result = feed("Collections / 合集", request)
        for row in page["items"]:
            navigation(result, row["name"], f"{prefix}/collections/{row['id']}", ACQ)
        if page["after"]:
            link(
                result,
                f"{prefix}/collections?" + urlencode({"after": page["after"], "limit": limit}),
                "next",
                NAV,
            )
        return xml(result, NAV, request)

    @router.api_route("/collections/{identifier}", methods=["GET", "HEAD"])
    def group(
        request: Request,
        identifier: str,
        after_position: int = -1,
        expected_revision: int | None = None,
        expected_result: str | None = None,
        limit: int = 30,
    ) -> Response:
        params: dict = {"limit": limit}
        if after_position < -1:
            raise InvalidInput("Invalid collection page bounds")
        row = collections.get(
            identifier,
            limit=limit,
            after=encode_cursor(after_position) if after_position >= 0 else "",
            expected_revision=expected_revision,
            expected_result=expected_result,
        )
        ids = [item["book_id"] for item in row["items"]]
        if row["after"] is not None:
            params.update(after_position=row["items"][-1]["position"], expected_revision=row["revision"])
            if row["result_token"]:
                params["expected_result"] = row["result_token"]
        result = feed(row["name"], request, ACQ)
        acquisitions(result, ids)
        if len(params) > 1:
            link(result, f"{prefix}/collections/{identifier}?" + urlencode(params), "next", ACQ)
        return xml(result, ACQ, request)

    @router.api_route("/books/{identifier}/archive", methods=["GET", "HEAD"])
    def archive(identifier: str) -> Response:
        return ArchiveResponse(files, files.library.get_book(identifier), limiter)

    @router.api_route("/books/{identifier}/cover", methods=["GET", "HEAD"])
    async def cover(request: Request, identifier: str) -> Response:
        book = await anyio.to_thread.run_sync(lambda: files.library.get_book(identifier))
        format: ThumbnailFormat = "webp" if "image/webp" in request.headers.get("accept", "") else "jpeg"
        response = await image_response(
            request,
            lambda: files.cover_bytes(identifier, cache, format=format, book=book),
            media_type=f"image/{format}",
            cached=lambda: files.cached_cover(book, cache, format=format),
            limiter=cover_limiter,
            cache_control=CLIENT_IMAGE,
        )
        response.headers["Vary"] += ", Accept"
        return response

    @router.api_route("/books/{identifier}/pages/{index}", methods=["GET", "HEAD"])
    async def page(request: Request, identifier: str, index: int) -> Response:
        def render():
            with files.library.connection() as db:
                if stream_type(identifier, db) is None:
                    raise NotFound("This book does not support OPDS-PSE")
            return files.page_bytes(identifier, index)

        return await image_response(
            request, render, media_type=image_media_type, limiter=limiter, cache_control=CLIENT_IMAGE
        )

    app.include_router(router)
