from __future__ import annotations

from custom_components.album_slideshow import synology as syn


# A realistic item, shaped like a live SYNO.Foto.Browse.Item response.
SAMPLE_ITEM = {
    "id": 10,
    "filename": "20260709_133425.jpg",
    "filesize": 4429191,
    "time": 1783604065,
    "type": "photo",
    "additional": {
        "description": "  Sunset walk  ",
        "resolution": {"width": 3000, "height": 4000},
        "orientation": 6,
        "thumbnail": {
            "m": "ready",
            "xl": "ready",
            "preview": "broken",
            "sm": "ready",
            "cache_key": "10_1783629314",
            "unit_id": 10,
        },
        "gps": {"latitude": 32.9504418, "longitude": -117.220068499722},
        "address": {
            "country": "United States",
            "state": "California",
            "county": "San Diego County",
            "city": "San Diego",
            "district": "Carmel Valley",
        },
    },
}


# ── normalize_base_url / api_url ───────────────────────────────────────────

def test_normalize_base_url_strips_suffixes():
    assert syn.normalize_base_url("http://nas:5000/") == "http://nas:5000"
    assert syn.normalize_base_url("http://nas:5000/webapi/entry.cgi") == "http://nas:5000"
    assert syn.normalize_base_url("https://nas.example.com/photos/") == "https://nas.example.com"


def test_api_url():
    assert syn.api_url("http://nas:5000") == "http://nas:5000/webapi/entry.cgi"


# ── namespace ──────────────────────────────────────────────────────────────

def test_namespace():
    assert syn.namespace(syn.SPACE_PERSONAL) == "SYNO.Foto"
    assert syn.namespace(syn.SPACE_SHARED) == "SYNO.FotoTeam"
    assert syn.namespace("anything_else") == "SYNO.Foto"


# ── build_thumbnail_url ────────────────────────────────────────────────────

def test_build_thumbnail_url_json_quotes_params():
    url = syn.build_thumbnail_url(
        "http://nas:5000", 10, "10_1783629314", "xl", syn.SPACE_PERSONAL
    )
    assert url.startswith("http://nas:5000/webapi/entry.cgi?")
    # The string params must be JSON-encoded (wrapped in literal quotes), which
    # url-encode to %22...%22. This is the exact quirk the live NAS requires.
    assert "type=%22unit%22" in url
    assert "size=%22xl%22" in url
    assert "cache_key=%2210_1783629314%22" in url
    assert "id=10" in url
    assert "api=SYNO.Foto.Thumbnail" in url


def test_build_thumbnail_url_shared_namespace():
    url = syn.build_thumbnail_url(
        "http://nas:5000", 5, "5_1", "m", syn.SPACE_SHARED
    )
    assert "api=SYNO.FotoTeam.Thumbnail" in url


def test_build_thumbnail_url_falls_back_on_bad_size():
    url = syn.build_thumbnail_url("http://nas:5000", 1, "1_1", "bogus")
    assert "size=%22xl%22" in url


# ── location_label ─────────────────────────────────────────────────────────

def test_location_label_prefers_city():
    assert syn.location_label(
        {"city": "San Diego", "state": "California", "country": "United States"}
    ) == "San Diego, United States"


def test_location_label_falls_back_to_state():
    assert syn.location_label(
        {"city": "", "state": "California", "country": "United States"}
    ) == "California, United States"


def test_location_label_handles_missing():
    assert syn.location_label(None) is None
    assert syn.location_label({}) is None


# ── is_image / thumbnail_ref ───────────────────────────────────────────────

def test_is_image_true_for_photo():
    assert syn.is_image(SAMPLE_ITEM) is True


def test_is_image_false_for_video():
    video = {"type": "video", "additional": {"thumbnail": {"cache_key": "x"}}}
    assert syn.is_image(video) is False


def test_is_image_false_without_thumbnail():
    assert syn.is_image({"type": "photo", "additional": {}}) is False


def test_thumbnail_ref_uses_unit_id():
    assert syn.thumbnail_ref(SAMPLE_ITEM) == (10, "10_1783629314")


def test_thumbnail_ref_falls_back_to_item_id():
    item = {
        "id": 42,
        "additional": {"thumbnail": {"cache_key": "42_1"}},
    }
    assert syn.thumbnail_ref(item) == (42, "42_1")


def test_thumbnail_ref_none_without_cache_key():
    assert syn.thumbnail_ref({"additional": {"thumbnail": {}}}) is None


# ── parse_photo_meta ───────────────────────────────────────────────────────

def test_parse_photo_meta_full():
    meta = syn.parse_photo_meta(SAMPLE_ITEM)
    assert meta["captured_at"] == 1783604065 * 1000
    assert meta["byte_size"] == 4429191
    assert meta["width"] == 3000
    assert meta["height"] == 4000
    assert meta["latitude"] == 32.9504418
    assert meta["longitude"] == -117.220068499722
    assert meta["location"] == "San Diego, United States"
    assert meta["description"] == "Sunset walk"


def test_parse_photo_meta_skips_zero_gps():
    item = {"time": 1, "additional": {"gps": {"latitude": 0, "longitude": 0}}}
    meta = syn.parse_photo_meta(item)
    assert "latitude" not in meta
    assert "longitude" not in meta


def test_parse_photo_meta_empty_description_dropped():
    item = {"additional": {"description": "   "}}
    assert "description" not in syn.parse_photo_meta(item)


# ── _is_otp_error ──────────────────────────────────────────────────────────

def test_is_otp_error_via_types():
    assert syn._is_otp_error({"code": 403, "types": [{"type": "otp"}]}) is True


def test_is_otp_error_via_token_payload():
    assert syn._is_otp_error({"code": 406, "token": "eyJ..."}) is True


def test_is_otp_error_false_for_plain_failure():
    assert syn._is_otp_error({"code": 400}) is False
    assert syn._is_otp_error(None) is False


# ── async_collect_assets: API selection + permission errors ────────────────

import asyncio


def _client_with_responses(responses):
    """Build a client whose ``_get`` returns queued responses and records calls."""
    c = syn.SynologyClient(None, "http://nas:5000", "u", "p", space=syn.SPACE_PERSONAL)
    c._sid = "SID"
    calls = []

    async def fake_get(params):
        calls.append(params)
        return responses.pop(0)

    c._get = fake_get  # type: ignore[assignment]
    return c, calls


def test_collect_assets_all_personal_uses_foto_item_api():
    c, calls = _client_with_responses([{"success": True, "data": {"list": []}}])
    asyncio.run(c.async_collect_assets(None))
    assert calls[0]["api"] == "SYNO.Foto.Browse.Item"
    assert "album_id" not in calls[0]


def test_collect_assets_all_shared_uses_fototeam_item_api():
    c, calls = _client_with_responses([{"success": True, "data": {"list": []}}])
    c.space = syn.SPACE_SHARED
    asyncio.run(c.async_collect_assets(None))
    assert calls[0]["api"] == "SYNO.FotoTeam.Browse.Item"


def test_collect_assets_album_always_uses_foto_item_api_even_in_shared():
    c, calls = _client_with_responses([{"success": True, "data": {"list": []}}])
    c.space = syn.SPACE_SHARED
    asyncio.run(c.async_collect_assets(5))
    # Albums live in the personal space, so an album id must use SYNO.Foto.
    assert calls[0]["api"] == "SYNO.Foto.Browse.Item"
    assert calls[0]["album_id"] == 5


def test_collect_assets_permission_error_raises_specific_exception():
    c, _ = _client_with_responses([{"success": False, "error": {"code": 801}}])
    c.space = syn.SPACE_SHARED
    try:
        asyncio.run(c.async_collect_assets(None))
    except syn.SynologyPermissionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected SynologyPermissionError")


def test_collect_assets_other_error_raises_auth_error():
    c, _ = _client_with_responses([{"success": False, "error": {"code": 400}}])
    try:
        asyncio.run(c.async_collect_assets(None))
    except syn.SynologyAuthError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected SynologyAuthError")


def test_list_albums_queries_own_then_shared_with_me():
    # Own albums come from SYNO.Foto.Browse.Album; shared-with-me albums come
    # from SYNO.Foto.Sharing.Misc and are merged in with their passphrase.
    c, calls = _client_with_responses([
        {"success": True, "data": {"list": [
            {"id": 1, "name": "Mine", "shared": False},
        ]}},
        {"success": True, "data": {"list": [
            {"id": 2, "name": "backyard", "passphrase": "vTaz7kAka"},
        ]}},
    ])
    c.space = syn.SPACE_SHARED
    albums = asyncio.run(c.async_list_albums())
    assert calls[0]["api"] == "SYNO.Foto.Browse.Album"
    assert calls[1]["api"] == "SYNO.Foto.Sharing.Misc"
    assert calls[1]["method"] == "list_shared_with_me_album"
    names = {a["name"]: a for a in albums}
    assert names["Mine"].get("shared") is not True
    assert names["backyard"]["shared"] is True
    assert names["backyard"]["passphrase"] == "vTaz7kAka"


def test_list_albums_drops_shared_without_passphrase():
    c, _ = _client_with_responses([
        {"success": True, "data": {"list": []}},
        {"success": True, "data": {"list": [
            {"id": 9, "name": "no-pass"},  # missing passphrase -> skipped
        ]}},
    ])
    albums = asyncio.run(c.async_list_albums())
    assert albums == []


def test_collect_assets_passphrase_uses_foto_item_with_passphrase():
    c, calls = _client_with_responses([{"success": True, "data": {"list": []}}])
    c.space = syn.SPACE_SHARED
    asyncio.run(c.async_collect_assets(None, passphrase="vTaz7kAka"))
    assert calls[0]["api"] == "SYNO.Foto.Browse.Item"
    assert calls[0]["passphrase"] == "vTaz7kAka"
    assert "album_id" not in calls[0]


def test_build_thumbnail_url_includes_passphrase():
    url = syn.build_thumbnail_url(
        "http://nas:5000", 43, "43_1", "xl", syn.SPACE_PERSONAL, passphrase="vTaz7kAka"
    )
    assert "passphrase=vTaz7kAka" in url


def test_build_thumbnail_url_no_passphrase_by_default():
    url = syn.build_thumbnail_url("http://nas:5000", 43, "43_1", "xl")
    assert "passphrase" not in url


