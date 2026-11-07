from __future__ import annotations

import contextlib
import re
import time
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import Metadata, Model, Source, SourceComment, SourceNotes, SourceRating, Tag, canonical_json
from .source_http import SourceError, SourceHTTP, gallery_url, public_url

COMMENT_LIMIT = 250
COMMENT_BYTES = 128 * 1024
"""One budget for stored comments per book: at most COMMENT_LIMIT items and COMMENT_BYTES of JSON."""
REUSABLE_ARCHIVE_TTL = 86400
"""An archive link already offered by the archiver page; EH does not say how long it lives."""
SUBMITTED_ARCHIVE_TTL = 6 * 86400
"""A freshly requested EH archive link (EH keeps prepared archives for about a week)."""
GDATA_BATCH = 25
"""The EH API accepts at most 25 gid/token pairs per gdata request."""
SEARCH_CANDIDATES = 12


def text(value) -> str | None:
    return unescape(str(value)).strip() if value is not None and str(value).strip() else None


def language(tags: list[Tag]) -> str | None:
    values = [t.value for t in tags if t.namespace == "language" and t.value not in {"translated", "rewrite"}]
    return values[0] if len(set(values)) == 1 else None


def archive_endpoint(source: Source) -> str:
    return f"https://{urlsplit(source.url).hostname}/archiver.php?" + urlencode(
        {"gid": source.gallery_id, "token": source.gallery_token}
    )


def archive_link(html: str, base: str) -> str | None:
    match = re.search(r"""(?:document\.location(?:\.href)?|window\.location)\s*=\s*["']([^"']+)["']""", html)
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one('a[href*="/archive/"]')
    value = match[1] if match else str(link.get("href")) if link else None
    return public_url(urljoin(base, unescape(value)), resolve=False) if value else None


def parse_quote(html: str) -> dict:
    plain = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    if "insufficient funds" in plain.casefold():
        raise SourceError("insufficient_gp")
    balance = re.search(r"Current Funds:.*?([\d,]+)\s*GP", plain, re.I)
    result = {
        "original_gp": None,
        "resample_gp": None,
        "balance_gp": int(balance[1].replace(",", "")) if balance else None,
    }
    for option in re.split(r"(?=Download Cost)", html, flags=re.I)[1:]:
        kind = re.search(r"""name=["']dltype["']\s+value=["'](org|res)["']""", option, re.I)
        label = (
            kind[1]
            if kind
            else "org"
            if "performOriginal" in option
            else "res"
            if "performResample" in option
            else None
        )
        cost = re.search(
            r"Download Cost\s*:?\s*(Free!?|[\d,]+\s*GP)",
            BeautifulSoup(option, "html.parser").get_text(" ", strip=True),
            re.I,
        )
        if label and cost:
            result["original_gp" if label == "org" else "resample_gp"] = (
                0 if cost[1].lower().startswith("free") else int(re.sub(r"\D", "", cost[1]))
            )
    return result


def parse_eh_comments(html: str, source: Source) -> list[SourceComment]:
    soup = BeautifulSoup(html, "html.parser")
    result = []
    for block in soup.select("#cdiv .c1")[:250]:
        body, header = block.select_one(".c6"), block.select_one(".c3")
        if body is None or header is None:
            continue
        identifier = re.search(
            r"(?:comment_|comment|c)(\d+)", str(block.get("id", "")) + " " + str(body.get("id", ""))
        )
        if identifier is None:
            for node in block.select("[id], [onclick], a[href]"):
                identifier = re.search(
                    r"(?:comment_|comment|commentid=|commentid\D+|votecomment\D+|c)(\d+)", str(node)
                )
                if identifier:
                    break
        if identifier is None:
            continue
        author = header.select_one("a")
        date = re.search(r"Posted on (.*?) by:", header.get_text(" ", strip=True))
        posted = None
        if date:
            with contextlib.suppress(ValueError):
                posted = int(
                    datetime.strptime(date[1].strip(), "%d %B %Y, %H:%M").replace(tzinfo=UTC).timestamp()
                )
        score = block.select_one(".c5")
        vote = re.search(r"(?:Score:)?\s*([+-]?\d+)", score.get_text(" ", strip=True)) if score else None
        result.append(
            SourceComment(
                source=source,
                comment_id=identifier[1],
                author=(author.get_text(" ", strip=True) if author else "")[:256],
                body=body.get_text("\n", strip=True)[:8192],
                posted_at=posted,
                score=int(vote[1]) if vote else None,
                is_uploader=any(
                    re.search(r"\buploader\s+comment\b", node.get_text(" ", strip=True), re.I)
                    for node in block.select(".c3, .c4, .c5")
                ),
            )
        )
    return result


def archive_remote(link: str, ttl: int) -> dict:
    """The stored remote archive location: the link with start=1 and its expiry."""
    return {"url": link + ("&" if "?" in link else "?") + "start=1", "expires_at": int(time.time()) + ttl}


def bounded_comments(comments: list) -> tuple[list, bool]:
    """Keep the leading comments within the shared budget; the flag says nothing was dropped."""
    result, used = [], 0
    for comment in comments:
        value = comment.model_dump(mode="json") if isinstance(comment, Model) else comment
        size = len(canonical_json(value).encode())
        if len(result) >= COMMENT_LIMIT or used + size > COMMENT_BYTES:
            return result, False
        result.append(comment)
        used += size
    return result, True


def _eh_metadata(source: Source, value: dict) -> Metadata:
    if str(value.get("gid")) != str(source.gallery_id) or value.get("token") != source.gallery_token:
        raise SourceError("source_identity_mismatch")
    tags = [
        Tag(namespace=t.split(":", 1)[0] if ":" in t else "", value=t.split(":", 1)[-1])
        for t in value["tags"]
    ]
    parent = (
        Source(site=source.site, gallery_id=int(value["parent_gid"]), gallery_token=value["parent_key"])
        if value.get("parent_gid") and value.get("parent_key")
        else None
    )
    newer = (
        [Source(site=source.site, gallery_id=int(value["current_gid"]), gallery_token=value["current_key"])]
        if value.get("current_gid")
        and int(value["current_gid"]) != source.gallery_id
        and value.get("current_key")
        else []
    )
    return Metadata(
        title=text(value.get("title")),
        original_title=text(value.get("title_jpn")),
        language=language(tags),
        translated=any(t.namespace == "language" and t.value == "translated" for t in tags),
        work_type=text(value.get("category")),
        tags=tags,
        source=source,
        uploader=text(value.get("uploader")),
        uploaded_at=datetime.fromtimestamp(int(value["posted"]), UTC).isoformat(),
        source_rating=SourceRating(average=float(value["rating"])) if value.get("rating") else None,
        declared_page_count=int(value["filecount"]),
        declared_byte_size=int(value["filesize"]),
        parent=parent,
        newer_versions=newer,
    )


def observation(
    metadata: Metadata,
    notes: SourceNotes,
    comments: list[SourceComment],
    *,
    cover_url: str,
    comments_error: str | None = None,
    gdata: dict | None = None,
) -> dict:
    """One fetched gallery: curated-field candidates in `metadata`, volatile notes/comments beside it."""
    result = {
        "metadata": metadata.model_dump(mode="json"),
        "notes": notes.model_dump(mode="json"),
        "comments": [comment.model_dump(mode="json") for comment in comments],
        "cover_url": cover_url,
        "fetched_at": notes.fetched_at,
        "comments_error": comments_error,
    }
    if gdata is not None:
        result["gdata"] = gdata
    return result


PROTOCOL_ERRORS = (KeyError, TypeError, ValueError, OverflowError, IndexError, AttributeError, RecursionError)


class Sources:
    def __init__(self, http: SourceHTTP):
        self.http = http
        self.nh_thumb_server: str | None = None
        self.nh_thumb_fetched = 0.0

    def fetch(self, source: Source, *, comments: bool = True, cancelled=None) -> dict:
        try:
            return (
                self._nh(source, comments, cancelled)
                if source.site == "nhentai"
                else self._eh(source, comments, cancelled)
            )
        except PROTOCOL_ERRORS:
            raise SourceError("source_protocol") from None

    def preferred(self, source: Source, *, comments: bool = True, cancelled=None) -> dict:
        if source.site == "nhentai":
            return self.fetch(source, comments=comments, cancelled=cancelled)
        preferred = Source(site="ehentai", gallery_id=source.gallery_id, gallery_token=source.gallery_token)
        try:
            return self.fetch(preferred, comments=comments, cancelled=cancelled)
        except SourceError as error:
            if error.code not in {"gallery_unavailable", "source_login_required", "source_unavailable"}:
                raise
            alternate = Source(
                site="exhentai", gallery_id=source.gallery_id, gallery_token=source.gallery_token
            )
            return self.fetch(alternate, comments=comments, cancelled=cancelled)

    def _gdata(self, sources: list[Source], cancelled) -> list[dict | None]:
        """gdata entries for up to GDATA_BATCH galleries of one site, in input order."""
        endpoint = (
            "https://exhentai.org/api.php"
            if sources[0].site == "exhentai"
            else "https://api.e-hentai.org/api.php"
        )
        value = self.http.json(
            "POST",
            endpoint,
            json={
                "method": "gdata",
                "gidlist": [[source.gallery_id, source.gallery_token] for source in sources],
                "namespace": 1,
            },
            cancelled=cancelled,
        )
        try:
            entries = {
                str(entry.get("gid")): entry for entry in value["gmetadata"] if isinstance(entry, dict)
            }
        except PROTOCOL_ERRORS:
            raise SourceError("source_protocol") from None
        return [entries.get(str(source.gallery_id)) for source in sources]

    def _eh(self, source: Source, comments: bool, cancelled) -> dict:
        value = self._gdata([source], cancelled)[0]
        if value is None:
            raise SourceError("source_identity_mismatch")
        if value.get("error"):
            raise SourceError("gallery_unavailable")
        metadata = _eh_metadata(source, value)
        notes = SourceNotes(source=source, tags=metadata.tags, fetched_at=int(time.time()))
        found: list[SourceComment] = []
        warning = None
        if comments:
            try:
                html = self.http.read("GET", source.url + "?hc=1&nw=always", cancelled=cancelled).decode(
                    "utf-8", "replace"
                )
                found, complete = bounded_comments(parse_eh_comments(html, source))
                notes.comments_pages = 1
                # EH exposes one all-comments document; never invent more pages.
                notes.comments_complete = (
                    bool(BeautifulSoup(html, "html.parser").select_one("#cdiv"))
                    and complete
                    and len(found) < COMMENT_LIMIT
                )
            except SourceError as error:
                warning = error.code
        return observation(
            metadata, notes, found, cover_url=str(value.get("thumb", "")), comments_error=warning, gdata=value
        )

    def _nh(self, source: Source, comments: bool, cancelled) -> dict:
        endpoint = f"https://nhentai.net/api/v2/galleries/{source.gallery_id}"
        value = self.http.json("GET", endpoint, cancelled=cancelled)
        if value["id"] != source.gallery_id:
            raise SourceError("source_identity_mismatch")
        tags = [
            Tag(namespace=str(t["type"]) if t["type"] != "tag" else "", value=t["name"])
            for t in value["tags"]
        ]
        metadata = Metadata(
            title=text(value["title"].get("english") or value["title"].get("pretty")),
            original_title=text(value["title"].get("japanese")),
            tags=tags,
            language=language(tags),
            translated=any(t.namespace == "language" and t.value == "translated" for t in tags),
            source=source,
            declared_page_count=int(value["num_pages"]),
            uploaded_at=datetime.fromtimestamp(int(value["upload_date"]), UTC).isoformat(),
        )
        notes = SourceNotes(source=source, tags=tags, fetched_at=int(time.time()), comments_sort="newest")
        warning = None
        by_id: dict[str, SourceComment] = {}
        if comments:
            try:
                for page in range(1, 6):
                    data = self.http.json(
                        "GET",
                        endpoint + "/comments?" + urlencode({"page": page, "per_page": 50}),
                        cancelled=cancelled,
                        limit=COMMENT_BYTES,
                    )
                    for comment in data["result"]:
                        if comment["gallery_id"] != source.gallery_id:
                            raise SourceError("source_identity_mismatch")
                        entry = SourceComment(
                            source=source,
                            comment_id=str(comment["id"]),
                            author=str(comment["poster"]["username"])[:256],
                            body=str(comment["body"])[:8192],
                            posted_at=int(comment["post_date"]),
                        )
                        by_id[entry.comment_id] = entry
                    notes.comments_pages = page
                    if page >= data["num_pages"]:
                        notes.comments_complete = True
                        break
                    if not bounded_comments(list(by_id.values()))[1]:
                        break
            except SourceError as error:
                warning = error.code
        found, complete = bounded_comments(list(by_id.values()))
        notes.comments_complete = notes.comments_complete and complete
        cover = value.get("cover", {})
        cover_url = ""
        if cover.get("path"):
            try:
                if self.nh_thumb_server is None or time.monotonic() - self.nh_thumb_fetched > 3600:
                    config = self.http.json("GET", "https://nhentai.net/api/v2/config", cancelled=cancelled)
                    server = config["thumb_servers"][0]
                    self.nh_thumb_server = (
                        public_url(
                            server if server.startswith("https://") else "https://" + server, resolve=False
                        ).rstrip("/")
                        + "/"
                    )
                    self.nh_thumb_fetched = time.monotonic()
                cover_url = public_url(urljoin(self.nh_thumb_server, cover["path"]), resolve=False)
            except (SourceError, KeyError, TypeError, IndexError, ValueError):
                cover_url = ""
        return observation(metadata, notes, found, cover_url=cover_url, comments_error=warning)

    def search_eh(self, original: dict, *, cancelled=None) -> list[dict]:
        """EH galleries matching the original title; unusable hits are skipped, not fatal."""
        metadata = Metadata.model_validate(original["metadata"])
        query = metadata.original_title or metadata.title
        if not query:
            raise SourceError("source_title_missing")
        html = self.http.read(
            "GET", "https://e-hentai.org/?" + urlencode({"f_search": query[:300]}), cancelled=cancelled
        ).decode("utf-8", "replace")
        soup = BeautifulSoup(html, "html.parser")
        hits: list[Source] = []
        for node in soup.select('a[href*="/g/"]'):
            try:
                source = gallery_url(str(node.get("href")))
            except ValueError:
                continue
            if source.site != "nhentai" and source not in hits:
                hits.append(source)
            if len(hits) >= SEARCH_CANDIDATES:
                break
        if (
            not hits
            and not soup.select(".itg")
            and not re.search(r"No (?:hits|results)|No unfiltered results", html, re.I)
        ):
            raise SourceError("source_search_failed")
        found: dict[str, dict] = {}
        for site in ("ehentai", "exhentai"):
            group = [source for source in hits if source.site == site]
            for start in range(0, len(group), GDATA_BATCH):
                batch = group[start : start + GDATA_BATCH]
                for source, value in zip(batch, self._gdata(batch, cancelled), strict=True):
                    if not value or value.get("error"):
                        continue
                    try:
                        metadata = _eh_metadata(source, value)
                    except (SourceError, *PROTOCOL_ERRORS):
                        continue
                    notes = SourceNotes(source=source, tags=metadata.tags, fetched_at=int(time.time()))
                    found[source.url] = observation(
                        metadata, notes, [], cover_url=str(value.get("thumb", "")), gdata=value
                    )
        return [found[source.url] for source in hits if source.url in found]

    def quote(self, source: Source, *, cancelled=None) -> tuple[dict, str | None]:
        """Archive prices plus an already prepared archive link, if the archiver page shows one."""
        if source.site == "nhentai":
            if not self.http.secrets.get("nh_key"):
                raise SourceError("nh_key_required")
            return {"original_gp": 0, "resample_gp": None, "balance_gp": None}, None
        url = archive_endpoint(source)
        html = self.http.read("GET", url, headers={"Referer": source.url}, cancelled=cancelled).decode(
            "utf-8", "replace"
        )
        return parse_quote(html), archive_link(html, url)

    def submit(self, source: Source, mode: str, *, cancelled=None) -> dict:
        if source.site == "nhentai":
            value = self.http.json(
                "POST",
                f"https://nhentai.net/api/v2/galleries/{source.gallery_id}/download?format=zip",
                cancelled=cancelled,
            )
            if int(value["expires_at"]) <= time.time():
                raise SourceError("source_link_expired")
            return {"url": public_url(value["url"], resolve=False), "expires_at": int(value["expires_at"])}
        url = archive_endpoint(source)
        html = self.http.read(
            "POST",
            url,
            data={
                "dltype": mode,
                "dlcheck": "Download Original Archive" if mode == "org" else "Download Resample Archive",
            },
            headers={"Referer": url},
            cancelled=cancelled,
        ).decode("utf-8", "replace")
        if "insufficient funds" in html.casefold():
            raise SourceError("insufficient_gp")
        link = archive_link(html, url)
        if not link:
            raise SourceError("remote_result_unknown")
        return archive_remote(link, SUBMITTED_ARCHIVE_TTL)

    def verify(self, site: str) -> dict:
        """Check the stored credentials of one site with an authenticated request."""
        if site == "nhentai" and not self.http.secrets.get("nh_key"):
            return {"reachable": True, "authenticated": False, "detail": "nh_key_required"}
        url = {
            "ehentai": "https://e-hentai.org/home.php",
            "exhentai": "https://exhentai.org/",
            "nhentai": "https://nhentai.net/api/v2/favorites",
        }[site]
        try:
            body = self.http.read("GET", url)
        except SourceError as error:
            if error.code == "source_rate_limited":
                raise
            reachable = error.code in {"source_login_required", "verification_required"}
            return {"reachable": reachable, "authenticated": False, "detail": error.code}
        if site == "nhentai":
            authenticated = True
        else:
            page = BeautifulSoup(body.decode("utf-8", "replace"), "html.parser")
            authenticated = (
                page.select_one(".itg") is not None
                if site == "exhentai"
                else bool(re.search(r"Image Limits|GP\b", page.get_text(" ", strip=True)))
            )
        return {
            "reachable": True,
            "authenticated": authenticated,
            "detail": None if authenticated else "source_login_required",
        }
