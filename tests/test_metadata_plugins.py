"""nhentai / hitomi metadata plugins — ID resolution, tag mapping, offline fetch flows."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from mangacouch.plugins.base import MetadataContext
from mangacouch.plugins.builtin import hitomi_metadata, nhentai_metadata

NH_GALLERY = {
    "id": 177013,
    "title": {
        "english": "[ShindoLA] METAMORPHOSIS (Complete) [English]",
        "japanese": None,
        "pretty": "METAMORPHOSIS",
    },
    "upload_date": 1476793729,
    "tags": [
        {"type": "artist", "name": "shindol"},
        {"type": "tag", "name": "dark skin"},
        {"type": "language", "name": "english"},
        {"type": "category", "name": "doujinshi"},
    ],
}

HITOMI_JS = (
    "var galleryinfo = "
    + json.dumps(
        {
            "id": "123456",
            "title": "Sample Gallery",
            "type": "doujinshi",
            "language": "japanese",
            "tags": [
                {"tag": "glasses", "male": "", "female": "1"},
                {"tag": "crossdressing", "male": 1, "female": ""},
                {"tag": "full color", "male": "", "female": ""},
            ],
            "artists": [{"artist": "someone"}],
            "groups": [{"group": "circle"}],
            "parodys": [{"parody": "original"}],
            "characters": [{"character": "protagonist"}],
        }
    )
)


def _ctx(**kwargs) -> MetadataContext:
    defaults = {"archive_id": "x" * 32, "title": "", "source_url": None, "config": {}}
    defaults.update(kwargs)
    return MetadataContext(**defaults)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# -- nhentai ------------------------------------------------------------------------------------


def test_nh_gallery_id_from_url():
    assert nhentai_metadata.gallery_id_from_url("https://nhentai.net/g/177013/") == 177013
    assert nhentai_metadata.gallery_id_from_url("nhentai.net/g/9/") == 9
    assert nhentai_metadata.gallery_id_from_url("https://e-hentai.org/g/1/ab/") is None
    assert nhentai_metadata.gallery_id_from_url(None) is None


def test_nh_gallery_id_from_tags():
    tags = ["artist:x", "source:nhentai.net/g/42", "language:english"]
    assert nhentai_metadata.gallery_id_from_tags(tags) == 42
    assert nhentai_metadata.gallery_id_from_tags(["source:https://nhentai.net/g/7/"]) == 7
    assert nhentai_metadata.gallery_id_from_tags(["source:e-hentai.org/g/1/ab"]) is None


def test_nh_tag_mapping():
    tags = nhentai_metadata.tags_from_gallery(NH_GALLERY)
    assert "artist:shindol" in tags
    assert "dark skin" in tags  # plain "tag" type stays un-namespaced
    assert "language:english" in tags
    assert "category:doujinshi" in tags


def test_nh_full_run_from_source_url():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/galleries/177013"
        return httpx.Response(200, json=NH_GALLERY)

    plugin = nhentai_metadata.NHentaiMetadataPlugin()
    with _client(handler) as session:
        result = plugin.get_tags(
            _ctx(source_url="https://nhentai.net/g/177013/", session=session)
        )
    assert result.error is None
    assert result.title == "METAMORPHOSIS"
    assert "source:nhentai.net/g/177013" in result.tags
    assert "timestamp:1476793729" not in "".join(result.tags)


def test_nh_timestamp_param():
    plugin = nhentai_metadata.NHentaiMetadataPlugin()
    with _client(lambda r: httpx.Response(200, json=NH_GALLERY)) as session:
        result = plugin.get_tags(
            _ctx(
                source_url="https://nhentai.net/g/177013/",
                session=session,
                config={"add_timestamp": "true"},
            )
        )
    assert "timestamp:1476793729" in result.tags


def test_nh_title_search_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/search":
            assert request.url.params["query"] == "METAMORPHOSIS"
            return httpx.Response(200, json={"result": [{"id": 177013}]})
        return httpx.Response(200, json=NH_GALLERY)

    plugin = nhentai_metadata.NHentaiMetadataPlugin()
    with _client(handler) as session:
        result = plugin.get_tags(_ctx(title="METAMORPHOSIS", session=session))
    assert result.error is None
    assert "artist:shindol" in result.tags


def test_nh_title_search_disabled():
    plugin = nhentai_metadata.NHentaiMetadataPlugin()
    with _client(lambda r: httpx.Response(500)) as session:
        result = plugin.get_tags(
            _ctx(title="METAMORPHOSIS", session=session, config={"title_search": "false"})
        )
    assert result.error is not None  # no network call, no gallery


def test_nh_http_error_is_clean():
    plugin = nhentai_metadata.NHentaiMetadataPlugin()
    with _client(lambda r: httpx.Response(403)) as session:
        result = plugin.get_tags(
            _ctx(source_url="https://nhentai.net/g/1/", session=session)
        )
    assert result.error is not None
    assert "403" in result.error


# -- hitomi -------------------------------------------------------------------------------------


def test_hitomi_gallery_id_from_url():
    f = hitomi_metadata.gallery_id_from_url
    assert f("https://hitomi.la/galleries/123456.html") == 123456
    assert f("https://hitomi.la/doujinshi/some-title-japanese-987654.html#p1") == 987654
    assert f("https://hitomi.la/reader/555.html") == 555
    assert f("https://nhentai.net/g/1/") is None


def test_hitomi_gallery_id_from_tags():
    f = hitomi_metadata.gallery_id_from_tags
    assert f(["source:hitomi.la/galleries/42.html"]) == 42
    assert f(["source:https://hitomi.la/cg/title-777.html"]) == 777
    assert f(["source:nhentai.net/g/1"]) is None


def test_hitomi_gallery_id_from_filename():
    f = hitomi_metadata.gallery_id_from_filename
    assert f("{123456} Some Title") == 123456
    assert f("123456 Some Title") == 123456
    assert f("Some Title (2024)") is None  # trailing digits are not an ID


def test_hitomi_parse_galleryinfo():
    data = hitomi_metadata.parse_galleryinfo(HITOMI_JS)
    assert data["title"] == "Sample Gallery"


def test_hitomi_tag_mapping():
    data = hitomi_metadata.parse_galleryinfo(HITOMI_JS)
    tags = hitomi_metadata.tags_from_galleryinfo(data)
    assert "female:glasses" in tags
    assert "male:crossdressing" in tags
    assert "full color" in tags
    assert "artist:someone" in tags
    assert "group:circle" in tags
    assert "parody:original" in tags
    assert "character:protagonist" in tags
    assert "type:doujinshi" in tags
    assert "language:japanese" in tags


def test_hitomi_full_run_from_filename():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/galleries/123456.js"
        return httpx.Response(200, text=HITOMI_JS)

    plugin = hitomi_metadata.HitomiMetadataPlugin()
    with _client(handler) as session:
        result = plugin.get_tags(
            _ctx(session=session, file_path=Path("/x/{123456} Sample Gallery.zip"))
        )
    assert result.error is None
    assert result.title == "Sample Gallery"
    assert "source:hitomi.la/galleries/123456.html" in result.tags


def test_hitomi_no_id_found():
    plugin = hitomi_metadata.HitomiMetadataPlugin()
    result = plugin.get_tags(_ctx(title="whatever", file_path=Path("/x/Some Title.zip")))
    assert result.error is not None
