from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from custom_components.album_slideshow import config_flow
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow import ugreen as ugr
from custom_components.album_slideshow.const import (
    CONF_UGREEN_ALBUM_TYPE,
    CONF_UGREEN_ALBUM_UUID,
    CONF_UGREEN_PASSWORD,
    CONF_UGREEN_URL,
    CONF_UGREEN_USERNAME,
    CONF_UGREEN_VERIFY_SSL,
    PROVIDER_UGREEN,
)

# A realistic item, shaped like a live ``album/picture/list`` response entry.
SAMPLE_PICTURE = {
    "picture_id": 2,
    "uid": 1000,
    "username": "testuser",
    "file_name": "IMG_2885.jpg",
    "real_ext_name": "jpg",
    "file_path": "/home/testuser/Photos/IMG_2885.jpg",
    "width": 3752,
    "height": 2843,
    "size": 3940828,
    "create_time_utc": 1750501533,
    "upload_time": 1791019939,
}

SAMPLE_ALBUM = {
    "album_uuid": "12345678-1234-1234-1234-123456789abc",
    "album_name": "Vacation Photos",
    "album_type": 1,
    "total": 4,
}


# ── normalize_base_url ──────────────────────────────────────────────────────

def test_normalize_base_url_strips_trailing_slash():
    assert ugr.normalize_base_url("https://nas:9443/") == "https://nas:9443"
    assert ugr.normalize_base_url("https://nas:9443") == "https://nas:9443"


# ── _rsa_encrypt_long (the web app's "encryptLong" scheme) ─────────────────

def _generate_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.PKCS1,
    )
    return private_key, pem


def _rsa_decrypt_long(private_key, b64_ciphertext: str) -> str:
    """Test-only inverse of ``_rsa_encrypt_long`` for round-tripping."""
    raw = base64.b64decode(b64_ciphertext)
    chunk_size = private_key.key_size // 8
    chunks = [raw[i : i + chunk_size] for i in range(0, len(raw), chunk_size)]
    return b"".join(
        private_key.decrypt(c, padding.PKCS1v15()) for c in chunks
    ).decode("utf-8")


def test_rsa_encrypt_long_round_trip_single_chunk():
    private_key, pem = _generate_keypair()
    encrypted = ugr._rsa_encrypt_long("a-short-password", pem)
    assert len(encrypted) == 344  # base64 of one 256-byte RSA-2048 ciphertext
    assert _rsa_decrypt_long(private_key, encrypted) == "a-short-password"


def test_rsa_encrypt_long_round_trip_multiple_chunks():
    private_key, pem = _generate_keypair()
    plaintext = "x" * 300  # spans 3 chunks of up to 117 chars each
    encrypted = ugr._rsa_encrypt_long(plaintext, pem)
    # 3 chunks * 256-byte RSA-2048 ciphertext = 768 bytes, base64'd *once*
    # over the concatenated raw bytes (not per chunk), so this divides
    # evenly with no padding - unlike the single-chunk case.
    assert len(encrypted) == 1024
    assert _rsa_decrypt_long(private_key, encrypted) == plaintext


def test_rsa_encrypt_long_empty_string():
    _, pem = _generate_keypair()
    # Must not crash on an empty chunk list.
    encrypted = ugr._rsa_encrypt_long("", pem)
    assert len(encrypted) == 344


# ── build_image_url ────────────────────────────────────────────────────────

def test_build_image_url_includes_required_params():
    url = ugr.build_image_url("https://nas:9443/", 2, "uuid-1", upload_time=1791019939)
    assert url.startswith("https://nas:9443/ugreen/v5/photo/picture/stream?")
    assert "id=2" in url
    assert "source_album_uuid=uuid-1" in url
    assert "source_album_type=1" in url
    assert "upload_time=1791019939" in url
    assert "ugk" not in url


def test_image_params_carry_static_token_only_after_login():
    c = ugr.UGreenClient(None, "https://nas:9443", "user", "pw")
    assert c.image_params == {}
    c._static_token = "STATICTOKEN"
    assert c.image_params == {"ugk": "STATICTOKEN"}


# ── describe_album_type ────────────────────────────────────────────────────

def test_describe_album_type_known_values():
    assert ugr.describe_album_type(ugr.ALBUM_TYPE_REGULAR) == "Regular"
    assert ugr.describe_album_type(ugr.ALBUM_TYPE_CONDITIONAL) == "Conditional"
    assert ugr.describe_album_type(ugr.ALBUM_TYPE_BABY) == "Baby"


def test_describe_album_type_unknown_value_falls_back():
    assert ugr.describe_album_type(99) == "Type 99"


# ── parse_photo_meta ───────────────────────────────────────────────────────

def test_parse_photo_meta_full():
    meta = ugr.parse_photo_meta(SAMPLE_PICTURE)
    assert meta["captured_at"] == 1750501533 * 1000
    assert meta["byte_size"] == 3940828
    assert meta["width"] == 3752
    assert meta["height"] == 2843


def test_parse_photo_meta_missing_fields():
    assert not ugr.parse_photo_meta({})


# ── _float_or_none / location_label / parse_picture_location ───────────────
# Shaped like a live ``picture/info`` response (GPS and address fields are
# empty strings, not omitted, when the photo has no location data).

SAMPLE_PICTURE_INFO_NO_GPS = {
    "picture_id": 2,
    "altitude": "",
    "latitude": "",
    "longitude": "",
    "addr_detail": "",
    "country": "",
    "province": "",
    "city_name": "",
    "city_code": "",
    "district": "",
    "street": "",
    "town": "",
}

SAMPLE_PICTURE_INFO_WITH_GPS = {
    "picture_id": 3,
    "latitude": "52.5200",
    "longitude": "13.4050",
    "country": "Germany",
    "province": "Berlin",
    "city_name": "Berlin",
    "district": "Mitte",
}


def test_float_or_none_parses_numeric_strings():
    assert ugr._float_or_none("52.52") == 52.52


def test_float_or_none_blank_string_is_none():
    assert ugr._float_or_none("") is None
    assert ugr._float_or_none("   ") is None


def test_float_or_none_invalid_string_is_none():
    assert ugr._float_or_none("not-a-number") is None


def test_float_or_none_passes_through_numeric_types():
    assert ugr._float_or_none(52) == 52.0
    assert ugr._float_or_none(52.5) == 52.5


def test_location_label_prefers_city_name():
    assert ugr.location_label(SAMPLE_PICTURE_INFO_WITH_GPS) == "Berlin, Germany"


def test_location_label_falls_back_to_province():
    info = {"city_name": "", "province": "Berlin", "country": "Germany"}
    assert ugr.location_label(info) == "Berlin, Germany"


def test_location_label_none_when_empty():
    assert ugr.location_label(SAMPLE_PICTURE_INFO_NO_GPS) is None


def test_parse_picture_location_with_gps():
    meta = ugr.parse_picture_location(SAMPLE_PICTURE_INFO_WITH_GPS)
    assert meta["latitude"] == 52.52
    assert meta["longitude"] == 13.405
    assert meta["location"] == "Berlin, Germany"


def test_parse_picture_location_without_gps_returns_empty():
    assert not ugr.parse_picture_location(SAMPLE_PICTURE_INFO_NO_GPS)


def test_parse_picture_location_skips_null_island():
    info = {"latitude": "0", "longitude": "0"}
    meta = ugr.parse_picture_location(info)
    assert "latitude" not in meta
    assert "longitude" not in meta


# ── UGreenClient.async_list_albums / async_list_album_pictures ────────────
# These only exercise the pure pagination/response-unwrapping logic; the
# client's ``_post`` is replaced with a fake coroutine so no real aiohttp
# session or NAS is involved, mirroring ``test_synology.py``'s approach.

def _client_with_responses(responses):
    c = ugr.UGreenClient(None, "https://nas:9443", "user", "pw")
    c._plain_token = "token"
    c._security_key = "key"
    c._token_public_key = b"unused"
    c._uid = 1000
    calls = []

    async def fake_post(path, body):
        calls.append((path, body))
        return responses.pop(0)

    c._post = fake_post  # type: ignore[assignment]
    return c, calls


def test_async_list_albums_returns_result_list():
    c, calls = _client_with_responses(
        [{"code": 200, "msg": "success", "data": {"result": [SAMPLE_ALBUM]}}]
    )
    albums = asyncio.run(c.async_list_albums())
    assert albums == [SAMPLE_ALBUM]
    assert calls[0][0] == ugr._ALBUM_LIST_PATH


def test_async_list_albums_pages_past_the_first_100():
    def album(i):
        return dict(SAMPLE_ALBUM, album_uuid=f"uuid-{i}", album_name=f"Album {i}")

    c, calls = _client_with_responses([
        {"code": 200, "data": {"result": [album(i) for i in range(100)]}},
        {"code": 200, "data": {"result": [album(100), {"album_name": "no uuid"}]}},
    ])
    albums = asyncio.run(c.async_list_albums())
    assert [a["album_uuid"] for a in albums] == [f"uuid-{i}" for i in range(101)]
    assert [call[1]["offset"] for call in calls] == [0, 100]


def test_async_list_albums_stops_when_offset_is_ignored():
    page = [dict(SAMPLE_ALBUM, album_uuid=f"uuid-{i}") for i in range(100)]
    c, calls = _client_with_responses([
        {"code": 200, "data": {"result": page}},
        {"code": 200, "data": {"result": page}},
    ])
    assert len(asyncio.run(c.async_list_albums())) == 100
    assert len(calls) == 2


def test_async_get_album_name_matches_by_uuid():
    renamed = dict(SAMPLE_ALBUM, album_name="Renamed")
    c, _ = _client_with_responses([
        {"code": 200, "data": {"result": [renamed]}},
        {"code": 200, "data": {"result": [renamed]}},
    ])
    assert asyncio.run(c.async_get_album_name(SAMPLE_ALBUM["album_uuid"])) == "Renamed"
    assert asyncio.run(c.async_get_album_name("some-other-uuid")) is None


def test_async_list_albums_raises_on_error_code():
    c, _ = _client_with_responses([{"code": 1024, "msg": "Login has expired"}])
    try:
        asyncio.run(c.async_list_albums())
        assert False, "expected UGreenApiError"
    except ugr.UGreenApiError:
        pass


def test_async_list_album_pictures_single_page():
    c, calls = _client_with_responses(
        [{"code": 200, "data": {"list": [SAMPLE_PICTURE]}}]
    )
    pictures = asyncio.run(c.async_list_album_pictures("uuid-1"))
    assert pictures == [SAMPLE_PICTURE]
    assert calls[0][1]["offset"] == 0
    assert calls[0][1]["album_uuid"] == "uuid-1"


def test_async_list_album_pictures_paginates_until_short_page():
    full_page = [dict(SAMPLE_PICTURE, picture_id=i) for i in range(1000)]
    short_page = [dict(SAMPLE_PICTURE, picture_id=1000)]
    c, calls = _client_with_responses(
        [
            {"code": 200, "data": {"list": full_page}},
            {"code": 200, "data": {"list": short_page}},
        ]
    )
    pictures = asyncio.run(c.async_list_album_pictures("uuid-1"))
    assert len(pictures) == 1001
    assert [call[1]["offset"] for call in calls] == [0, 1000]


def test_async_list_album_pictures_stops_on_empty_list():
    c, calls = _client_with_responses([{"code": 200, "data": {"list": []}}])
    pictures = asyncio.run(c.async_list_album_pictures("uuid-1"))
    assert pictures == []
    assert len(calls) == 1


# ── UGreenClient.async_get_picture_info ─────────────────────────────────────
# Unlike the other API calls, this one is a plain GET through ``_session()``
# rather than ``_post``, so it needs its own fake aiohttp-shaped session.

class _FakeGetResponse:
    def __init__(self, json_data):
        self._json = json_data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        return self._json


class _FakeGetSession:
    def __init__(self, json_data):
        self._json = json_data
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return _FakeGetResponse(self._json)


def test_async_get_picture_info_returns_data():
    c = ugr.UGreenClient(None, "https://nas:9443", "user", "pw")
    c._plain_token = "token"
    c._security_key = "key"
    c._token_public_key = b"unused"
    c._uid = 1000
    fake_session = _FakeGetSession(
        {"code": 200, "data": SAMPLE_PICTURE_INFO_WITH_GPS}
    )
    c._session = lambda: fake_session  # type: ignore[assignment]
    c._auth_token_header = lambda: "encrypted"  # type: ignore[assignment]

    info = asyncio.run(c.async_get_picture_info(3, "uuid-1"))
    assert info == SAMPLE_PICTURE_INFO_WITH_GPS
    url, kwargs = fake_session.calls[0]
    assert url.endswith(ugr._PICTURE_INFO_PATH)
    assert kwargs["params"]["picture_id"] == 3
    assert kwargs["params"]["source_album_uuid"] == "uuid-1"
    assert "ssl" not in kwargs


def test_session_follows_verify_ssl(monkeypatch):
    chosen = []
    monkeypatch.setattr(
        ugr, "async_get_clientsession",
        lambda _hass, verify_ssl=True: chosen.append(verify_ssl),
    )
    ugr.UGreenClient(None, "https://nas:9443", "user", "pw")._session()
    ugr.UGreenClient(None, "https://nas:9443", "user", "pw", verify_ssl=False)._session()
    assert chosen == [True, False]


def test_async_get_picture_info_raises_on_error_code():
    c = ugr.UGreenClient(None, "https://nas:9443", "user", "pw")
    c._plain_token = "token"
    c._security_key = "key"
    c._token_public_key = b"unused"
    c._uid = 1000
    fake_session = _FakeGetSession({"code": 1024, "msg": "Login has expired"})
    c._session = lambda: fake_session  # type: ignore[assignment]
    c._auth_token_header = lambda: "encrypted"  # type: ignore[assignment]

    try:
        asyncio.run(c.async_get_picture_info(3, "uuid-1"))
        assert False, "expected UGreenApiError"
    except ugr.UGreenApiError:
        pass


# ── AlbumCoordinator._update_ugreen ────────────────────────────────────────

class _FakeUGreenClient:
    """Stands in for a logged-in client; every login gets its own ``ugk``."""

    album_name = "Renamed in UGOS"

    def __init__(self, hass, url, username, password, *, verify_ssl=True):
        self.base_url = ugr.normalize_base_url(url)
        self.verify_ssl = verify_ssl
        self.listed = []
        self.image_headers = {"Cookie": "token_uid=1000; token=enc"}
        self.image_params = {}

    async def async_login(self):
        self.image_params = {"ugk": f"SESSIONKEY-{id(self)}"}

    async def async_list_album_pictures(self, album_uuid, album_type):
        self.listed.append((album_uuid, album_type))
        return [
            SAMPLE_PICTURE,
            dict(SAMPLE_PICTURE, picture_id=3, file_name="clip.MOV", real_ext_name="mov"),
            dict(SAMPLE_PICTURE, picture_id=4, file_name="IMG_0004.mp4", real_ext_name=""),
        ]

    async def async_get_album_name(self, album_uuid):
        if isinstance(self.album_name, Exception):
            raise self.album_name
        return self.album_name


def _ugreen_coordinator(monkeypatch, **data):
    monkeypatch.setattr(ugr, "UGreenClient", _FakeUGreenClient)
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = None
    coord.entry = SimpleNamespace(title="Vacation Photos", data={
        CONF_UGREEN_URL: "https://nas:9443/",
        CONF_UGREEN_USERNAME: "user",
        CONF_UGREEN_PASSWORD: "pw",
        CONF_UGREEN_ALBUM_UUID: "uuid-1",
        CONF_UGREEN_ALBUM_TYPE: ugr.ALBUM_TYPE_CONDITIONAL,
        **data,
    })
    return coord


def test_update_ugreen_builds_token_free_photo_items(monkeypatch):
    coord = _ugreen_coordinator(monkeypatch, **{CONF_UGREEN_VERIFY_SSL: False})
    data = asyncio.run(coord._update_ugreen())
    items = data["items"]

    assert data["title"] == "Renamed in UGOS"
    assert [item.source_id for item in items] == ["2"]
    assert "ugk" not in items[0].url
    assert "source_album_uuid=uuid-1" in items[0].url
    assert "source_album_type=2" in items[0].url
    assert coord._ugreen_client.listed == [("uuid-1", ugr.ALBUM_TYPE_CONDITIONAL)]
    assert coord.image_request_params == coord._ugreen_client.image_params
    assert coord.image_request_headers == {"Cookie": "token_uid=1000; token=enc"}
    assert coord._ugreen_client.verify_ssl is False
    assert coord.image_request_verify_ssl is False


def test_update_ugreen_urls_survive_a_new_login(monkeypatch):
    coord = _ugreen_coordinator(monkeypatch)
    first = asyncio.run(coord._update_ugreen())["items"]
    first_params = coord.image_request_params
    first[0].latitude, first[0].longitude, first[0].exif_scanned = 52.52, 13.405, True

    second = asyncio.run(coord._update_ugreen())["items"]
    coordinator_module._merge_prior_enrichment(second, first)

    assert coord.image_request_params != first_params
    assert second[0].url == first[0].url
    assert second[0].exif_scanned and second[0].latitude == 52.52
    assert coord.image_request_verify_ssl is True


def test_update_ugreen_title_falls_back_to_entry_title(monkeypatch):
    coord = _ugreen_coordinator(monkeypatch)
    monkeypatch.setattr(_FakeUGreenClient, "album_name", ugr.UGreenApiError("album/list failed"))
    assert asyncio.run(coord._update_ugreen())["title"] == "Vacation Photos"


def test_ugreen_entries_can_turn_off_reverse_geocoding():
    entry = SimpleNamespace(data={"provider": PROVIDER_UGREEN}, options={})
    flow = config_flow.ConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, config_flow.LocalFolderOptionsFlow)
    flow.config_entry = entry
    flow.async_show_form = lambda **kwargs: kwargs
    form = asyncio.run(flow.async_step_init())
    assert form["data_schema"]({}) == {"reverse_geocode": True, "hide_home_country": False}
