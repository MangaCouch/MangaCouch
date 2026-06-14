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
    # Mirrors the real archiver.php layout: the cost appears BEFORE each form's submit button, so
    # costs must be mapped to options via the form's dltype (org/res), not by proximity to the text.
    html = """
    <p>Current Funds: 156,509 GP [?] &nbsp; 424,447 Credits [?]</p>
    <div>Download Cost: &nbsp; 726 GP &nbsp; Estimated Size: 15.37 MiB
      <form method="post"><input type="hidden" name="dltype" value="org" />
      <input type="submit" name="dlcheck" value="Download Original Archive" /></form>
    </div>
    <div>Download Cost: &nbsp; 427 GP &nbsp; Estimated Size: 9.04 MiB
      <form method="post"><input type="hidden" name="dltype" value="res" />
      <input type="submit" name="dlcheck" value="Download Resample Archive" /></form>
    </div>
    """
    page = parse_archiver_gp(html)
    assert page.current_gp == 156509
    assert page.credits == 424447
    assert page.original_cost == 726  # the Original cost — NOT the resample (the old bug)
    assert page.resample_cost == 427


def test_redirect_regex():
    html = 'window.onload = function(){ document.location = "https://hath.example/archive/xyz/" }'
    m = ehentai._REDIRECT_RE.search(html)
    assert m is not None
    assert m.group(1) == "https://hath.example/archive/xyz/"
