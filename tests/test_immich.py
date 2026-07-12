from __future__ import annotations

from custom_components.album_slideshow import immich


# ── normalize_base_url ─────────────────────────────────────────────────────

def test_normalize_strips_trailing_slash_and_api():
    assert immich.normalize_base_url("http://x:2283/") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283/api") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283/api/") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283") == "http://x:2283"


# ── build_image_url ────────────────────────────────────────────────────────

def test_build_image_url_preview():
    u = immich.build_image_url("http://x:2283", "abc", "preview")
    assert u == "http://x:2283/api/assets/abc/thumbnail?size=preview"


def test_build_image_url_fullsize():
    u = immich.build_image_url("http://x:2283", "abc", "fullsize")
    assert u == "http://x:2283/api/assets/abc/thumbnail?size=fullsize"


def test_build_image_url_original():
    u = immich.build_image_url("http://x:2283/api", "abc", "original")
    assert u == "http://x:2283/api/assets/abc/original"


def test_build_image_url_never_includes_key():
    u = immich.build_image_url("http://x:2283", "abc", "preview")
    assert "apiKey" not in u and "x-api-key" not in u


# ── _to_epoch_ms ───────────────────────────────────────────────────────────

def test_to_epoch_ms_parses_z():
    assert immich._to_epoch_ms("2022-02-06T21:51:51.000Z") == 1644184311000


def test_to_epoch_ms_parses_offset():
    assert immich._to_epoch_ms("2022-02-06T21:51:51+00:00") == 1644184311000


def test_to_epoch_ms_bad_input():
    assert immich._to_epoch_ms(None) is None
    assert immich._to_epoch_ms("") is None
    assert immich._to_epoch_ms("not a date") is None


# ── location_label ─────────────────────────────────────────────────────────

def test_location_label_city_country():
    assert immich.location_label("Lisbon", None, "Portugal") == "Lisbon, Portugal"


def test_location_label_falls_back_to_state():
    assert immich.location_label(None, "California", "USA") == "California, USA"


def test_location_label_country_only():
    assert immich.location_label(None, None, "France") == "France"


def test_location_label_none():
    assert immich.location_label(None, None, None) is None
    assert immich.location_label("", "  ", "") is None


# ── parse_search_page ──────────────────────────────────────────────────────

def test_parse_search_page_filters_and_paginates():
    payload = {
        "assets": {
            "items": [
                {"id": "1", "type": "IMAGE"},
                {"id": "2", "type": "VIDEO"},
                {"id": "3", "type": "IMAGE", "isTrashed": True},
                {"id": "4", "type": "IMAGE", "isArchived": True},
                {"id": "5", "type": "IMAGE"},
                {"type": "IMAGE"},  # no id
            ],
            "nextPage": "2",
        }
    }
    items, nxt = immich.parse_search_page(payload)
    assert [i["id"] for i in items] == ["1", "5"]
    assert nxt == 2


def test_parse_search_page_no_next():
    payload = {"assets": {"items": [{"id": "1", "type": "IMAGE"}], "nextPage": None}}
    items, nxt = immich.parse_search_page(payload)
    assert len(items) == 1 and nxt is None


def test_parse_search_page_empty():
    assert immich.parse_search_page({}) == ([], None)
    assert immich.parse_search_page(None) == ([], None)


# ── parse_asset_exif ───────────────────────────────────────────────────────

def test_parse_asset_exif_full():
    asset = {
        "localDateTime": "2022-02-06T13:51:51.000Z",
        "exifInfo": {
            "dateTimeOriginal": "2022-02-06T21:51:51+00:00",
            "latitude": 38.7,
            "longitude": -9.1,
            "city": "Lisbon",
            "country": "Portugal",
            "description": "  Sunset  ",
        },
    }
    out = immich.parse_asset_exif(asset)
    assert out["captured_at"] == 1644184311000
    assert out["latitude"] == 38.7
    assert out["longitude"] == -9.1
    assert out["location"] == "Lisbon, Portugal"
    assert out["description"] == "Sunset"


def test_parse_asset_exif_null_island_dropped():
    asset = {"exifInfo": {"latitude": 0, "longitude": 0}}
    out = immich.parse_asset_exif(asset)
    assert "latitude" not in out


def test_parse_asset_exif_empty_description_ignored():
    asset = {"exifInfo": {"description": "   "}}
    out = immich.parse_asset_exif(asset)
    assert "description" not in out


def test_parse_asset_exif_no_exif():
    assert immich.parse_asset_exif({}) == {}
    assert immich.parse_asset_exif(None) == {}
