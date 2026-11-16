from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

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


def test_fetch_album_skips_a_photo_listed_again_with_a_new_link(monkeypatch):
    def entry(key, path):
        return gs._parse_album_item([key, [f"https://lh3.googleusercontent.com/{path}", 100, 100]])

    pages = {
        None: ([entry("key-1", "a"), entry("key-2", "b")], "page-2"),
        "page-2": ([entry("key-1", "a-renewed"), entry("key-3", "c")], None),
    }

    async def fake_keys(session, share_url, *, timeout):
        return gs._AlbumKeys("album", "auth"), "Title"

    async def fake_page(session, keys, page_id, *, timeout, video_keys=None):
        return pages[page_id]

    monkeypatch.setattr(gs, "_fetch_album_keys", fake_keys)
    monkeypatch.setattr(gs, "_fetch_album_page", fake_page)

    _title, items, _videos = asyncio.run(gs.fetch_album(None, "https://photos.app.goo.gl/album"))

    assert [item.source_id for item in items] == ["key-1", "key-2", "key-3"]


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

# -- batchexecute / snAcKc parsing -----------------------------------------

import json as _json


def _make_batchexecute_response(items, next_page_id, title="Album"):
    inner = [None, items, next_page_id, [None, title]]
    inner_json = _json.dumps(inner)
    outer = [["wrb.fr", "snAcKc", inner_json, None, None, "generic"]]
    return ")]}'\n\n" + _json.dumps(outer)


def test_batchexecute_parses_items_and_next_page():
    items = [
        ["mk1", ["https://lh3.googleusercontent.com/aaa", 1920, 1080], 0, "d1"],
        ["mk2", ["https://lh3.googleusercontent.com/bbb", 800, 600], 0, "d2"],
    ]
    body = _make_batchexecute_response(items, "next-token-123")
    parsed_items, next_id = gs._parse_batchexecute_album_page(body)
    assert next_id == "next-token-123"
    assert len(parsed_items) == 2
    assert parsed_items[0].url.startswith("https://lh3.googleusercontent.com/aaa=")
    assert parsed_items[0].width == 1920


def test_batchexecute_empty_next_page_becomes_none():
    body = _make_batchexecute_response([], "")
    items, next_id = gs._parse_batchexecute_album_page(body)
    assert items == []
    assert next_id is None


def test_batchexecute_handles_garbage():
    items, next_id = gs._parse_batchexecute_album_page("not valid")
    assert items == []
    assert next_id is None


def test_batchexecute_filters_videos():
    # A video has a duration dict (key 76647426) as its last element.
    photo = ["mk1", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = [
        "mk2",
        ["https://lh3.googleusercontent.com/v", 100, 100],
        0,
        "d2",
        None,
        None,
        {"76647426": [12345]},
    ]
    body = _make_batchexecute_response([photo, video], None)
    items, _ = gs._parse_batchexecute_album_page(body)
    assert len(items) == 1
    assert items[0].url.startswith("https://lh3.googleusercontent.com/p=")


def test_batchexecute_filters_videos_when_duration_is_not_last():
    # The duration dict does not always sit at the end of the item (see #26).
    photo = ["mk1", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = [
        "mk2",
        ["https://lh3.googleusercontent.com/v", 100, 100],
        0,
        "d2",
        {"76647426": [12345]},
        None,
        ["trailing", 1],
    ]
    body = _make_batchexecute_response([photo, video], None)
    items, _ = gs._parse_batchexecute_album_page(body)
    assert len(items) == 1
    assert items[0].url.startswith("https://lh3.googleusercontent.com/p=")


def test_batchexecute_filters_videos_with_nested_duration():
    photo = ["mk1", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = [
        "mk2",
        ["https://lh3.googleusercontent.com/v", 100, 100],
        0,
        "d2",
        [None, {"76647426": [9000]}],
    ]
    body = _make_batchexecute_response([photo, video], None)
    items, _ = gs._parse_batchexecute_album_page(body)
    assert len(items) == 1


def test_batchexecute_accepts_integer_duration_key():
    photo = ["mk1", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = ["mk2", ["https://lh3.googleusercontent.com/v", 100, 100], 0, "d2"]
    assert gs._is_video_item(photo) is False
    video.append({gs._VIDEO_DURATION_KEY: [1]})
    assert gs._is_video_item(video) is True


def test_af_block_with_videos_is_still_recognised():
    # A video in the sample must not disqualify the whole AF item list.
    photo = ["mk1", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = [
        "mk2",
        ["https://lh3.googleusercontent.com/v", 100, 100],
        0,
        "d2",
        {"76647426": [12345]},
    ]
    assert gs._list_looks_like_album_items([photo, video]) is True


# -- _extract_keys ----------------------------------------------------------

def test_extract_keys_finds_request_payload():
    html = '''
    <script>some unrelated stuff</script>
    <script>
    "snAcKc",ext:foo,request:["AF1QipOTestKey-12345_-",null,null,"AuthKey-67890_-"]
    </script>
    '''
    keys = gs._extract_keys(html)
    assert keys is not None
    assert keys.album_key == "AF1QipOTestKey-12345_-"
    assert keys.auth_key == "AuthKey-67890_-"


def test_extract_keys_returns_none_when_absent():
    assert gs._extract_keys("<html>nothing here</html>") is None


# -- _extract_title ---------------------------------------------------------

def test_extract_title_strips_google_photos_suffix():
    html = "<title>My Holiday - Google Photos</title>"
    assert gs._extract_title(html) == "My Holiday"


def test_extract_title_returns_none_when_missing():
    assert gs._extract_title("<html></html>") is None
# -- timestamps + byte_size -------------------------------------------------

def test_parse_album_item_extracts_timestamps_and_size():
    raw = [
        "AF1QipMediaKey",
        ["https://lh3.googleusercontent.com/x", 1920, 1080, None, None, None, None, None, None, [None, None, 1], [2700088]],
        997560567000,   # captured_at: 2001-08
        "dedup",
        0,
        1616254072047, # uploaded_at: 2021-03
    ]
    item = gs._parse_album_item(raw)
    assert item is not None
    assert item.captured_at == 997560567000
    assert item.uploaded_at == 1616254072047
    assert item.byte_size == 2700088


@pytest.mark.parametrize("value", [
    gs._MIN_TS_MS - 1, gs._MAX_TS_MS + 1, True, False, "946684800000",
    946684800000.0, None, [], {},
])
def test_parse_album_item_rejects_implausible_timestamps(value):
    raw = [
        "mk", ["https://lh3.googleusercontent.com/x", 100, 100], value,
        "dedup", 0, value,
    ]
    item = gs._parse_album_item(raw)
    assert item is not None
    assert item.captured_at is None
    assert item.uploaded_at is None


@pytest.mark.parametrize("year", [1800, 1900, 1950, 1969, 1970, 1980, 1995, 1999, 2000, 2026])
@pytest.mark.parametrize("source", ["html", "batchexecute"])
def test_album_pages_preserve_historical_capture_dates(year, source):
    from custom_components.album_slideshow.camera import _ts_to_iso

    captured = datetime(year, 1, 1, tzinfo=timezone.utc)
    timestamp = int(captured.timestamp() * 1000)
    raw = [
        "old-photo", ["https://lh3.googleusercontent.com/historical", 1000, 800],
        timestamp, "dedup", 0, 1700000000000,
    ]
    if source == "html":
        items = gs.parse_album_html(_make_html([raw]))
    else:
        items, _ = gs._parse_batchexecute_album_page(
            _make_batchexecute_response([raw], None)
        )

    assert len(items) == 1
    assert items[0].captured_at == timestamp
    assert items[0].uploaded_at == 1700000000000
    assert _ts_to_iso(items[0].captured_at) == captured.isoformat()


def test_parse_album_item_handles_missing_size():
    raw = [
        "mk", ["https://lh3.googleusercontent.com/x", 100, 100], None, "dedup",
    ]
    item = gs._parse_album_item(raw)
    assert item is not None
    assert item.byte_size is None
    assert item.captured_at is None

# -- video media keys shared with the publicalbum.org source (#26) ----------
# publicalbum.org returns mimetype/mediaMetadata as null, so it cannot spot a
# video on its own. It reuses the keys the scraper collected here.

def test_batchexecute_records_media_keys_of_skipped_videos():
    photo = ["mk_photo", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    video = [
        "mk_video",
        ["https://lh3.googleusercontent.com/v", 100, 100],
        0,
        "d2",
        None,
        None,
        {"76647426": [12345]},
    ]
    body = _make_batchexecute_response([photo, video], None)

    keys: set[str] = set()
    items, _ = gs._parse_batchexecute_album_page(body, keys)

    assert len(items) == 1
    assert keys == {"mk_video"}


def test_record_video_key_populates_the_sink():
    keys: set[str] = set()
    gs._record_video_key(["mk_video", ["url", 1, 1]], keys)
    assert keys == {"mk_video"}
    # Items with no usable key must not poison the set.
    gs._record_video_key([], keys)
    gs._record_video_key([123], keys)
    assert keys == {"mk_video"}


def test_record_video_key_tolerates_no_sink():
    gs._record_video_key(["mk_video", ["url", 1, 1]], None)


def test_media_key_reads_the_leading_element():
    assert gs._media_key(["AF1Qip_abc", ["url", 1, 1]]) == "AF1Qip_abc"
    assert gs._media_key([]) is None
    assert gs._media_key("nope") is None
    assert gs._media_key([123, ["url"]]) is None


def test_video_key_sink_is_optional():
    # The parsers must stay usable without a sink (existing callers pass none).
    photo = ["mk", ["https://lh3.googleusercontent.com/p", 100, 100], 0, "d1"]
    body = _make_batchexecute_response([photo], None)
    items, _ = gs._parse_batchexecute_album_page(body)
    assert len(items) == 1


def _metadata_detail(camera):
    visual = ["https://example.test/photo", 1000, 800, None, None, None, None, None,
              [None, None, None, None, camera]]
    return [["photo-key", visual], None, [], [], {}]


def test_google_camera_metadata_fields_match_verified_response():
    detail = _metadata_detail([" Apple ", "iPhone 7", None, 3.99, 1.8, 20, 0.0011376564])
    assert gs.parse_photo_metadata(detail) == {
        "camera_make": "Apple", "camera_model": "iPhone 7", "focal_length_mm": 3.99,
        "aperture_f_number": 1.8, "iso": 20, "exposure_time_seconds": 0.0011376564,
    }


@pytest.mark.parametrize("key", ["396644657", 396644657])
@pytest.mark.parametrize("position", [2, 10, 15])
def test_google_description_uses_photo_metadata_key(key, position):
    detail = _metadata_detail(None)
    detail[0].extend([None] * (position + 1 - len(detail[0])))
    detail[0][position] = {key: ["  Summer by the lake  "]}

    assert gs.parse_photo_metadata(detail) == {"description": "Summer by the lake"}


@pytest.mark.parametrize("description", [None, [], [None], [42], [False], [""], ["  "], "not a list"])
def test_google_description_ignores_empty_or_malformed_values(description):
    detail = _metadata_detail(None)
    detail[0].append({"396644657": description})

    assert gs.parse_photo_metadata(detail) == {}


def test_google_description_does_not_use_album_or_comment_text():
    detail = _metadata_detail(None)
    detail[0].append({"other": ["unrelated text"]})
    detail.append({"396644657": ["not this photo's description"]})

    assert gs.parse_photo_metadata(detail) == {}


@pytest.mark.parametrize("camera", [None, [], [None] * 9, "unexpected"])
def test_google_missing_camera_metadata_is_successfully_empty(camera):
    assert gs.parse_photo_metadata(_metadata_detail(camera)) == {}


@pytest.mark.parametrize("invalid", [True, False, float("nan"), float("inf"), -1, 0, "20"])
def test_google_camera_metadata_rejects_invalid_numbers(invalid):
    assert gs.parse_photo_metadata(_metadata_detail([None, None, None] + [invalid] * 4)) == {}


class _MetadataResponse:
    status = 200

    def __init__(self, body):
        self.body = body.encode()
        self.content = self

    def raise_for_status(self):
        return None

    async def iter_chunked(self, size):
        for offset in range(0, len(self.body), size):
            yield self.body[offset:offset + size]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def test_google_metadata_request_is_read_only_and_uses_share_keys():
    detail = _metadata_detail(["Apple", "iPhone", None, 4.0, 1.8, 20, 0.01])
    response = _MetadataResponse(")]}'\n123\n" + json.dumps([
        ["wrb.fr", "VrseUb", json.dumps(detail)],
    ]))
    requests = []

    def post(url, **kwargs):
        requests.append((url, kwargs))
        return response

    result = asyncio.run(gs.fetch_photo_metadata(
        SimpleNamespace(post=post), gs._AlbumKeys("album-key", "share-key"), "photo-key",
    ))

    assert result["camera_model"] == "iPhone"
    url, request = requests[0]
    assert url.endswith("/PhotosUi/data/batchexecute")
    assert request["params"]["rpcids"] == "VrseUb"
    assert not {"Cookie", "Authorization"}.intersection(request["headers"])
    encoded = json.loads(parse_qs(request["data"])["f.req"][0])
    assert json.loads(encoded[0][0][1]) == ["photo-key", None, "share-key", None, "album-key"]
    assert request["allow_redirects"] is False


@pytest.mark.parametrize("body", ["", "not JSON", '[["wrb.fr", "VrseUb", "null"]]'])
def test_google_bad_metadata_is_retryable(body):
    session = SimpleNamespace(post=lambda *args, **kwargs: _MetadataResponse(body))
    with pytest.raises(ValueError):
        asyncio.run(gs.fetch_photo_metadata(session, gs._AlbumKeys("album", "key"), "photo-key"))


def test_google_metadata_response_is_size_limited():
    with pytest.raises(ValueError, match="size limit"):
        asyncio.run(gs._read_metadata_body(_MetadataResponse("x" * 1025), 1024))


@pytest.mark.parametrize("status", [200, 206])
def test_google_location_header_is_bounded_and_uses_original_image(status):
    response = _MetadataResponse("x" * (gs._LOCATION_HEADER_BYTES + 65536))
    response.status = status
    response.headers = {
        "Content-Type": "image/jpeg",
        "Content-Range": f"bytes 0-{gs._LOCATION_HEADER_BYTES - 1}/4000000",
    }
    requests = []

    def get(url, **kwargs):
        requests.append((url, kwargs))
        return response

    result = asyncio.run(gs.fetch_photo_location_header(
        SimpleNamespace(get=get), "https://lh3.googleusercontent.com/photo=w3840-h2880?key=retained",
    ))

    assert len(result) == gs._LOCATION_HEADER_BYTES
    url, request = requests[0]
    assert url == "https://lh3.googleusercontent.com/photo=d?key=retained"
    assert request["headers"] == {"Range": "bytes=0-262143"}
    assert request["allow_redirects"] is False
    assert request["timeout"] == 15.0


@pytest.mark.parametrize("url", [
    "http://lh3.googleusercontent.com/photo", "https://example.test/photo",
    "https://googleusercontent.com.example.test/photo", "https://googleusercontent.com/photo",
    "https://user:password@lh3.googleusercontent.com/photo",
    "https://lh3.googleusercontent.com:8123/photo",
])
def test_google_location_header_rejects_unexpected_hosts_without_fetching(url):
    with pytest.raises(ValueError, match="Unexpected Google image URL"):
        asyncio.run(gs.fetch_photo_location_header(SimpleNamespace(), url))


@pytest.mark.parametrize("status,content_type,content_range", [
    (302, "image/jpeg", ""), (200, "text/html", ""),
    (206, "image/jpeg", ""), (206, "image/jpeg", "bytes 100-200/300"),
])
def test_google_location_header_rejects_invalid_responses(status, content_type, content_range):
    response = _MetadataResponse("not an image")
    response.status = status
    response.headers = {"Content-Type": content_type, "Content-Range": content_range}
    session = SimpleNamespace(get=lambda *args, **kwargs: response)
    with pytest.raises(ValueError):
        asyncio.run(gs.fetch_photo_location_header(session, "https://lh3.googleusercontent.com/photo"))
