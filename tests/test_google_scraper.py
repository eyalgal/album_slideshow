from __future__ import annotations

import json

from custom_components.album_slideshow import google_scraper as gs


# -- Fixture: a minimal but realistic AF_initDataCallback page --------------

def _make_html(photo_entries: list[list]) -> str:
    """Build an HTML page with an AF_initDataCallback block carrying the
    given photo entries. Each entry is a raw photo array.
    """
    # The structure mirrors what Google emits today: data is a deeply nested
    # array, with the photo list one level deep. We add some siblings to make
    # sure the parser picks the right list.
    data = [
        None,
        photo_entries,  # the album item list
        "next-token-or-null",
        [
            "album-media-key",
            "Album Title",
            None,
            None,
            None,
            ["actor-id", "owner@example.com"],
        ],
    ]
    blob = json.dumps(data)
    return f"""<!doctype html>
<html><head><title>My Vacation - Google Photos</title></head><body>
<script>
AF_initDataCallback({{key: 'ds:0', hash: '1', data:{blob}, sideChannel: {{}}}});
</script>
</body></html>
"""


def _photo_entry(url: str, width: int, height: int) -> list:
    """A photo entry similar to Google's shared album shape:
    [mediaKey, [url, width, height], timestamp, dedupKey, ...].
    """
    return [
        "AF1Q-mediakey-" + url[-8:],
        [url, width, height],
        1700000000,
        "dedup",
    ]


# -- parse_album_html -------------------------------------------------------

def test_parse_extracts_all_photos():
    photos = [
        _photo_entry(f"https://lh3.googleusercontent.com/photo-{i}", 4032, 3024)
        for i in range(500)
    ]
    html = _make_html(photos)
    items = gs.parse_album_html(html)
    assert len(items) == 500
    assert all(it.url.startswith("https://lh3.googleusercontent.com/photo-") for it in items)
    # MediaItem keeps the original dimensions.
    assert items[0].width == 4032
    assert items[0].height == 3024
    # The URL hint is capped at 4K on the long edge, aspect preserved.
    assert items[0].url.endswith("=w3840-h2880")


def test_parse_normalises_existing_size_suffix():
    photos = [_photo_entry("https://lh3.googleusercontent.com/abc=w800-h600-no", 1920, 1080)]
    html = _make_html(photos)
    items = gs.parse_album_html(html)
    assert len(items) == 1
    # Old suffix stripped, new one appended based on dimensions.
    assert items[0].url == "https://lh3.googleusercontent.com/abc=w1920-h1080"


def test_parse_dedupes_repeated_urls():
    url = "https://lh3.googleusercontent.com/dup"
    photos = [_photo_entry(url, 100, 100), _photo_entry(url, 100, 100), _photo_entry(url, 100, 100)]
    html = _make_html(photos)
    items = gs.parse_album_html(html)
    assert len(items) == 1


def test_parse_returns_empty_on_missing_data():
    html = "<html><body>Nothing to see here.</body></html>"
    assert gs.parse_album_html(html) == []


def test_parse_returns_empty_on_malformed_data():
    html = "<script>AF_initDataCallback({key: 'ds:0', data:[unclosed</script>"
    assert gs.parse_album_html(html) == []


def test_parse_picks_largest_photo_list_over_member_list():
    # Make a member list that has a few stray googleusercontent URLs (profile
    # photos). The parser should still pick the much longer real photo list.
    photos = [
        _photo_entry(f"https://lh3.googleusercontent.com/photo-{i}", 1920, 1080)
        for i in range(100)
    ]
    members = [
        ["actor-1", "name", ["https://lh3.googleusercontent.com/profile-1", 64, 64]],
        ["actor-2", "name", ["https://lh3.googleusercontent.com/profile-2", 64, 64]],
    ]
    data = [None, photos, None, ["album-key", "Title", None, None, None, members]]
    blob = json.dumps(data)
    html = (
        "<html><head><title>X - Google Photos</title></head>"
        f"<body><script>AF_initDataCallback({{key:'ds:0', data:{blob}}});</script></body></html>"
    )
    items = gs.parse_album_html(html)
    assert len(items) == 100
    assert "profile" not in items[0].url


def test_parse_handles_tricky_strings_in_blob():
    # Apostrophes inside string values must not unbalance bracket-matching.
    photos = [
        ["mk1", ["https://lh3.googleusercontent.com/p1", 100, 100], 0, "Mike's photo"],
        ["mk2", ["https://lh3.googleusercontent.com/p2", 100, 100], 0, 'has "quotes" too'],
        ["mk3", ["https://lh3.googleusercontent.com/p3", 100, 100], 0, "ends with ]"],
    ]
    html = _make_html(photos)
    items = gs.parse_album_html(html)
    assert len(items) == 3


# -- _balanced_close --------------------------------------------------------

def test_balanced_close_simple_array():
    s = "[1, 2, 3]"
    assert gs._balanced_close(s, 0, "[", "]") == len(s) - 1


def test_balanced_close_nested():
    s = "[[1, 2], [3, [4, 5]]]"
    assert gs._balanced_close(s, 0, "[", "]") == len(s) - 1


def test_balanced_close_ignores_brackets_in_strings():
    s = '["]", "][", "x"]'
    assert gs._balanced_close(s, 0, "[", "]") == len(s) - 1


def test_balanced_close_handles_escapes():
    s = '["a\\"b]", "c"]'
    assert gs._balanced_close(s, 0, "[", "]") == len(s) - 1


def test_balanced_close_returns_none_when_unbalanced():
    s = "[1, 2, 3"
    assert gs._balanced_close(s, 0, "[", "]") is None


# -- _is_dimension ----------------------------------------------------------

def test_is_dimension_accepts_image_sized_ints():
    assert gs._is_dimension(100) is True
    assert gs._is_dimension(4032) is True


def test_is_dimension_rejects_implausible_values():
    assert gs._is_dimension(5) is False
    assert gs._is_dimension(50_000) is False
    assert gs._is_dimension("100") is False
    assert gs._is_dimension(None) is False


# -- _normalise_size --------------------------------------------------------

def test_normalise_size_strips_existing_suffix():
    assert gs._normalise_size(
        "https://lh3.googleusercontent.com/x=w800-h600-no", 1920, 1080
    ) == "https://lh3.googleusercontent.com/x=w1920-h1080"


def test_normalise_size_caps_at_4k():
    # 8000x6000 (4:3) -> long edge capped to 3840, height scales proportionally.
    assert gs._normalise_size(
        "https://lh3.googleusercontent.com/x", 8000, 6000
    ) == "https://lh3.googleusercontent.com/x=w3840-h2880"


def test_normalise_size_preserves_smaller_than_cap():
    assert gs._normalise_size(
        "https://lh3.googleusercontent.com/x", 1024, 768
    ) == "https://lh3.googleusercontent.com/x=w1024-h768"


def test_normalise_size_falls_back_when_dimensions_missing():
    assert gs._normalise_size(
        "https://lh3.googleusercontent.com/x", None, None
    ) == "https://lh3.googleusercontent.com/x=w1920-h1080"
