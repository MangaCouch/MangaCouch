from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from datetime import UTC, datetime
from html import unescape
from urllib.parse import urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from .models import (
    EH_SITES,
    Metadata,
    Model,
    Source,
    SourceComment,
    SourceNotes,
    SourceRating,
    Tag,
)
from .source_http import SourceError, SourceHTTP, public_url

COMMENT_LIMIT = 250
COMMENT_BYTES = 128 * 1024
"""One budget for stored comments per book: at most COMMENT_LIMIT items and COMMENT_BYTES of JSON."""
COMMENT_STATES = ("not_requested", "complete", "partial", "failed")
"""How far an observation's comments go: not fetched, all of them, a bounded prefix, or none (error)."""
REUSABLE_ARCHIVE_TTL = 86400
"""An archive link already offered by the archiver page; EH does not say how long it lives."""
SUBMITTED_ARCHIVE_TTL = 6 * 86400
"""A freshly requested EH archive link (EH keeps prepared archives for about a week)."""
GDATA_BATCH = 25
"""The EH API accepts at most 25 gid/token pairs per gdata request."""
PREFETCH_TTL = 600
"""Seconds a batch-fetched gdata entry may stand in for a fetch of its own."""
LOGGER = logging.getLogger(__name__)


def text(value) -> str | None:
    return unescape(str(value)).strip() if value is not None and str(value).strip() else None


def language(tags: list[Tag]) -> str | None:
    values = [t.value for t in tags if t.namespace == "language" and t.value not in {"translated", "rewrite"}]
    return values[0] if len(set(values)) == 1 else None


def archive_endpoint(source: Source) -> str:
    if source.site not in EH_SITES:
        raise SourceError("unsupported_source")
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


def unrecognized_archiver(quote: dict, link: str | None) -> bool:
    """An archiver page that offers neither a price nor a prepared link (blank, invalid key, ...)."""
    return link is None and quote["original_gp"] is None and quote["resample_gp"] is None


def parse_eh_comments(html: str | BeautifulSoup, source: Source) -> list[SourceComment]:
    soup = html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")
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
        size = len(repr(value).encode("utf-8", "replace"))
        if len(result) >= COMMENT_LIMIT or used + size > COMMENT_BYTES:
            return result, False
        result.append(comment)
        used += size
    return result, True


def _eh_metadata(source: Source, value: dict) -> Metadata:
    if (
        str(value.get("gid")) != str(source.gallery_id)
        or str(value.get("token", "")).lower() != source.gallery_token
    ):
        raise SourceError("source_identity_mismatch")
    if int(value["filecount"]) == 0:
        raise SourceError("gallery_unavailable")
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
    comments_state: str,
    comments_error: str | None = None,
    comments_retry_after: int = 0,
    gdata: dict | None = None,
) -> dict:
    """One fetched gallery: curated-field candidates in `metadata`, volatile notes/comments beside it.

    `comments_state` (one of COMMENT_STATES) tells storage whether `comments` may replace the
    comments kept for this source: only a complete fetch can prove that comments are gone.
    `comments_retry_after` is the wait a rate-limited comment fetch was given.
    """
    if comments_state not in COMMENT_STATES or (comments_error is not None) != (comments_state == "failed"):
        raise ValueError("Inconsistent comment state")
    result = {
        "metadata": metadata.model_dump(mode="json"),
        "notes": notes.model_dump(mode="json"),
        "comments": [comment.model_dump(mode="json") for comment in comments],
        "cover_url": cover_url,
        "fetched_at": notes.fetched_at,
        "comments_state": comments_state,
        "comments_error": comments_error,
    }
    if comments_retry_after:
        result["comments_retry_after"] = comments_retry_after
    if gdata is not None:
        result["gdata"] = gdata
    return result


PROTOCOL_ERRORS = (KeyError, TypeError, ValueError, OverflowError, IndexError, AttributeError, RecursionError)


@contextlib.contextmanager
def parsing():
    try:
        yield
    except PROTOCOL_ERRORS:
        LOGGER.warning("Invalid response from source", exc_info=True)
        raise SourceError("source_protocol") from None


class Sources:
    def __init__(self, http: SourceHTTP):
        self.http = http
        self._prefetched: dict[tuple[str, int, str | None], tuple[float, dict]] = {}
        self._prefetch_lock = threading.Lock()

    def prefetch(self, sources: list[Source], *, cancelled=None) -> None:
        """Fetch the gdata of many EH galleries at once; the next fetch of each uses it.

        One API call covers up to GDATA_BATCH galleries, so a batch of source checks costs one
        request instead of one per book. Entries are consumed by the first fetch, and a gallery
        the API did not answer for is simply fetched on its own later.
        """
        with self._prefetch_lock:
            now = time.monotonic()
            self._prefetched = {
                key: value for key, value in self._prefetched.items() if now - value[0] < PREFETCH_TTL
            }
        for site in EH_SITES:
            unique = {(s.gallery_id, s.gallery_token): s for s in sources if s.site == site}
            group = list(unique.values())
            for start in range(0, len(group), GDATA_BATCH):
                batch = group[start : start + GDATA_BATCH]
                values = self._gdata(batch, cancelled)
                with self._prefetch_lock:
                    for source, value in zip(batch, values, strict=True):
                        if value is not None:
                            self._prefetched[(site, source.gallery_id, source.gallery_token)] = (
                                time.monotonic(),
                                value,
                            )

    def prefetched(self, source: Source) -> bool:
        """Whether a fetch of `source` would use a fresh batch-fetched gdata entry."""
        with self._prefetch_lock:
            entry = self._prefetched.get((source.site, source.gallery_id, source.gallery_token))
        return entry is not None and time.monotonic() - entry[0] < PREFETCH_TTL

    def forget_prefetched(self, sources: list[Source]) -> None:
        """Drop the entries of `sources` no fetch used, so a later check never sees metadata from
        an earlier batch; other callers' entries stay."""
        with self._prefetch_lock:
            for source in sources:
                self._prefetched.pop((source.site, source.gallery_id, source.gallery_token), None)

    def fetch(self, source: Source, *, comments: bool = True, cancelled=None) -> dict:
        """One gallery's observation. Only the code reading a response turns malformed data into
        source_protocol (`parsing`); a programming error elsewhere stays an error."""
        if source.site not in EH_SITES:
            raise SourceError("unsupported_source")
        return self._eh(source, comments, cancelled)

    def preferred(self, source: Source, *, comments: bool = True, cancelled=None) -> dict:
        return self.fetch(source, comments=comments, cancelled=cancelled)

    def _gdata(self, sources: list[Source], cancelled) -> list[dict | None]:
        """gdata entries for up to GDATA_BATCH galleries of one site, in input order."""
        endpoint = (
            "https://exhentai.org/api.php"
            if sources[0].site == "exhentai" and self.http.secrets.get("ex_cookie")
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
            self.http.protocol_result(endpoint, False, repr(value).encode("utf-8", "replace"))
            LOGGER.warning("Invalid response from source", exc_info=True)
            raise SourceError("source_protocol") from None
        self.http.protocol_result(endpoint, True)
        return [entries.get(str(source.gallery_id)) for source in sources]

    def _eh(self, source: Source, comments: bool, cancelled) -> dict:
        with self._prefetch_lock:
            entry = self._prefetched.pop((source.site, source.gallery_id, source.gallery_token), None)
        fresh = entry is not None and time.monotonic() - entry[0] < PREFETCH_TTL
        value = entry[1] if entry is not None and fresh else self._gdata([source], cancelled)[0]
        if value is None:
            raise SourceError("source_identity_mismatch")
        with parsing():
            if value.get("error"):
                raise SourceError("gallery_unavailable")
            metadata = _eh_metadata(source, value)
            notes = SourceNotes(source=source, tags=metadata.tags, fetched_at=int(time.time()))
        found: list[SourceComment] = []
        warning, state, retry_after = None, "not_requested", 0
        if comments:
            try:
                found, complete = self._comments(source, cancelled)
                notes.comments_pages = 1
                # EH exposes one all-comments document; never invent more pages.
                notes.comments_complete = complete and len(found) < COMMENT_LIMIT
                state = "complete" if notes.comments_complete else "partial"
            except SourceError as error:
                found, warning, state, retry_after = [], error.code, "failed", error.retry_after
        with parsing():
            return observation(
                metadata,
                notes,
                found,
                cover_url=str(value.get("thumb", "")),
                comments_state=state,
                comments_error=warning,
                comments_retry_after=retry_after,
                gdata=value,
            )

    def _comments(self, source: Source, cancelled) -> tuple[list[SourceComment], bool]:
        """A gallery's comments from its page. Without an ExHentai cookie, an ExHentai gallery's
        page is read on e-hentai.org (ExHentai would only answer its login page). When
        e-hentai.org does not have the gallery (404/410), it is ExHentai-only and the error is
        ExHentai's own, source_login_required; any other failure (rate limit, network, a failing
        site) is reported as it is."""
        if source.site == "exhentai" and not self.http.secrets.get("ex_cookie"):
            alternate = Source(
                site="ehentai", gallery_id=source.gallery_id, gallery_token=source.gallery_token
            )
            try:
                return self._comment_page(alternate.url, source, cancelled)
            except SourceError as error:
                if error.code == "gallery_unavailable" or (
                    error.code == "source_unavailable" and error.status in {404, 410}
                ):
                    raise SourceError("source_login_required") from None
                raise
        return self._comment_page(source.url, source, cancelled)

    def _comment_page(self, url: str, source: Source, cancelled) -> tuple[list[SourceComment], bool]:
        html = self.http.read("GET", url + "?hc=1", cancelled=cancelled).decode("utf-8", "replace")
        with parsing():
            soup = BeautifulSoup(html, "html.parser")
            if soup.select_one("#cdiv") is None:
                # Every gallery page has a comment section, even an empty one; a page
                # without it (blank, warning or error page) says nothing about the comments.
                self.http.protocol_result(url, False, html.encode())
                raise SourceError("source_protocol")
            self.http.protocol_result(url, True)
            return bounded_comments(parse_eh_comments(soup, source))

    def quote(self, source: Source, *, cancelled=None) -> tuple[dict, str | None]:
        """Archive prices plus an already prepared archive link, if the archiver page shows one."""
        url = archive_endpoint(source)
        html = self.http.read("GET", url, headers={"Referer": source.url}, cancelled=cancelled).decode(
            "utf-8", "replace"
        )
        with parsing():
            quote, link = parse_quote(html), archive_link(html, url)
        if unrecognized_archiver(quote, link):
            # Never offer an approval with no known price ("?" GP) for a page that was not understood.
            self.http.protocol_result(url, False, html.encode())
            raise SourceError("source_protocol")
        self.http.protocol_result(url, True)
        return quote, link

    def submit(self, source: Source, mode: str, *, cancelled=None) -> dict:
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
        with parsing():
            link = archive_link(html, url)
        if not link:
            self.http.protocol_result(url, False, html.encode())
            raise SourceError("source_protocol")
        self.http.protocol_result(url, True)
        return archive_remote(link, SUBMITTED_ARCHIVE_TTL)

    def verify(self, site: str, *, cancelled=None) -> dict:
        """Check the stored credentials of one site with an authenticated request."""
        if site not in EH_SITES:
            raise SourceError("unsupported_source")
        url = {
            "ehentai": "https://e-hentai.org/home.php",
            "exhentai": "https://exhentai.org/home.php",
        }[site]
        try:
            body = self.http.read("GET", url, cancelled=cancelled)
        except SourceError as error:
            if error.code == "source_rate_limited":
                raise
            reachable = error.code in {"source_login_required", "verification_required"}
            return {"reachable": reachable, "authenticated": False, "detail": error.code}
        page = BeautifulSoup(body.decode("utf-8", "replace"), "html.parser")
        authenticated = bool(re.search(r"Image Limits|GP\b", page.get_text(" ", strip=True)))
        return {
            "reachable": True,
            "authenticated": authenticated,
            "detail": None if authenticated else "source_login_required",
        }
