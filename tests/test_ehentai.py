"""e(x)hentai protocol parsing (Appendix A) — no network."""

from __future__ import annotations

import pytest

from mangacouch.acquisition import ehentai
from mangacouch.acquisition.ehentai import EHentaiError, parse_archiver_gp, parse_gallery_url


def test_parse_gallery_url_ehentai():
    ref = parse_gallery_url("https://e-hentai.org/g/123456/0a1b2c3d4e/")
    assert ref.domain == "e-hentai"
    assert ref.gid == 123456
    assert ref.token == "0a1b2c3d4e"
    assert ref.host == "e-hentai.org"
    assert "archiver.php?gid=123456&token=0a1b2c3d4e" in ref.archiver_url


def test_parse_gallery_url_exhentai():
    ref = parse_gallery_url("https://exhentai.org/g/999/deadbeef00/")
    assert ref.domain == "exhentai"
    assert ref.host == "exhentai.org"


def test_parse_gallery_url_invalid():
    with pytest.raises(EHentaiError):
        parse_gallery_url("https://example.com/not/a/gallery")


def test_parse_archiver_gp():
    html = """
    <p>You currently have 4,200 GP and 15 Credits.</p>
    <div><input type="submit" value="Download Original Archive"> It will cost 1,337 GP.</div>
    <div><input type="submit" value="Download Resample Archive"> It will cost 50 GP.</div>
    """
    page = parse_archiver_gp(html)
    assert page.current_gp == 4200
    assert page.credits == 15
    assert page.original_cost == 1337
    assert page.resample_cost == 50


def test_redirect_regex():
    html = 'window.onload = function(){ document.location = "https://hath.example/archive/xyz/" }'
    m = ehentai._REDIRECT_RE.search(html)
    assert m is not None
    assert m.group(1) == "https://hath.example/archive/xyz/"
