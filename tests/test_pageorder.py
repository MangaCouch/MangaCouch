"""Page-order heuristics + perceptual hash — regressions for substring/alpha bugs."""
# pyright: reportOptionalMemberAccess=false, reportAttributeAccessIssue=false

from __future__ import annotations

from mangacouch.core.naturalsort import natural_page_sort


def test_five_digit_pages_stay_in_order():
    names = ["9998.jpg", "9999.jpg", "10000.jpg", "10001.jpg", "1.jpg"]
    assert natural_page_sort(names) == [
        "1.jpg", "9998.jpg", "9999.jpg", "10000.jpg", "10001.jpg",
    ]  # "10000" must not match the old "0000" cover keyword


def test_keyword_substrings_do_not_bucket():
    # "backstage" ⊃ "back", "legend" ⊃ "end" — these are body pages, not credits.
    names = ["01.jpg", "02.jpg", "03_backstage.jpg", "04_legend.jpg", "05.jpg"]
    assert natural_page_sort(names) == names


def test_cover_sorts_first_and_credits_last():
    names = ["00001.jpg", "00002.jpg", "cover.jpg", "credits.jpg", "back_cover.jpg"]
    ordered = natural_page_sort(names)
    assert ordered[0] == "cover.jpg"
    assert set(ordered[-2:]) == {"credits.jpg", "back_cover.jpg"}
    assert ordered[1:3] == ["00001.jpg", "00002.jpg"]


def test_cjk_keywords_still_match():
    names = ["001.jpg", "表紙.jpg", "奥付.jpg"]
    ordered = natural_page_sort(names)
    assert ordered[0] == "表紙.jpg"
    assert ordered[-1] == "奥付.jpg"


def test_dhash_ignores_alpha_channel():
    from mangacouch.core.imaging import dhash
    from tests.conftest import make_image_bytes

    rgb = make_image_bytes((120, 60, 200), fmt=".png")
    # Same colour, but encoded with an alpha channel.
    import pyvips

    img = pyvips.Image.new_from_buffer(rgb, "").addalpha()
    rgba = img.write_to_buffer(".png")

    assert dhash(rgb) == dhash(rgba)


def test_tagdb_ingest_accepts_rendered_and_plain_fields():
    """db.full.json renders name/intro as {"raw","text","html"} objects; old dumps were strings."""
    from mangacouch.tags.translation import _text_of

    assert _text_of("plain") == "plain"
    assert _text_of({"raw": "萝莉", "text": "萝莉文", "html": "<p>x</p>"}) == "萝莉文"
    assert _text_of({"html": "<p>only html</p>"}) == "<p>only html</p>"
    assert _text_of(None) == ""
    assert _text_of(123) == ""
