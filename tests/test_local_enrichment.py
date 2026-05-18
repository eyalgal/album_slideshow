"""Tests for the local-folder EXIF + reverse-geocode pipeline.

These cover the pieces that don't need a running HA event loop:
- ``_gps_to_decimal`` sign + range handling
- ``_geocode_cache_key`` rounding + ``-0.0`` normalisation
- ``_read_local_exif`` end-to-end against Pillow-generated JPEGs
- ``_format_nominatim_location`` field preferences
- ``_nominatim_lookup`` against a fake aiohttp session
- ``MediaItem`` round-trip through ``_save_cached_items`` /
  ``_load_cached_items`` (including the new GPS/location fields and
  the ``exif_scanned`` skip flag)
- ``_merge_prior_enrichment`` URL-keyed carry-over
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image
from PIL.TiffImagePlugin import IFDRational

from custom_components.album_slideshow import coordinator as c
from custom_components.album_slideshow.const import (
    CONF_REVERSE_GEOCODE,
    DEFAULT_REVERSE_GEOCODE,
)


# ── _gps_to_decimal ────────────────────────────────────────────

def test_gps_to_decimal_north_is_positive():
    # 37° 25' 19.07" N -> ~37.4220
    assert c._gps_to_decimal((37, 25, 19.07), "N") == pytest.approx(37.4220, abs=1e-3)


def test_gps_to_decimal_south_is_negative():
    assert c._gps_to_decimal((22, 54, 30), "S") == pytest.approx(-22.9083, abs=1e-3)


def test_gps_to_decimal_east_is_positive():
    assert c._gps_to_decimal((122, 5, 6), "E") == pytest.approx(122.085, abs=1e-3)


def test_gps_to_decimal_west_is_negative():
    assert c._gps_to_decimal((73, 56, 6), "W") == pytest.approx(-73.935, abs=1e-3)


def test_gps_to_decimal_handles_bytes_ref():
    # Some Pillow versions surface ASCII refs as bytes.
    assert c._gps_to_decimal((10, 0, 0), b"S") == pytest.approx(-10.0)


def test_gps_to_decimal_handles_ifdrational():
    dms = (IFDRational(37), IFDRational(25, 1), IFDRational(1907, 100))
    assert c._gps_to_decimal(dms, "N") == pytest.approx(37.4220, abs=1e-3)


def test_gps_to_decimal_rejects_malformed():
    assert c._gps_to_decimal(None, "N") is None
    assert c._gps_to_decimal((1, 2), "N") is None  # wrong arity
    assert c._gps_to_decimal(("bad", 0, 0), "N") is None


def test_gps_to_decimal_rejects_out_of_range():
    # 200° is past the 180° wraparound; reject rather than silently fold.
    assert c._gps_to_decimal((200, 0, 0), "N") is None


# ── _geocode_cache_key ──────────────────────────────────────────

def test_geocode_cache_key_rounds_to_three_dp():
    assert c._geocode_cache_key(37.42201234, -122.08491111) == "37.422,-122.085"


def test_geocode_cache_key_normalises_negative_zero():
    # Tiny negative inputs round to ``-0.0`` in IEEE 754; normalise to
    # the unsigned form so cache keys are stable.
    key = c._geocode_cache_key(-1e-5, -1e-5)
    assert "-0.000" not in key
    assert key == "0.000,0.000"


def test_geocode_cache_key_is_stable_across_call():
    assert c._geocode_cache_key(48.8566, 2.3522) == c._geocode_cache_key(
        48.8566, 2.3522
    )


# ── _parse_exif_datetime ────────────────────────────────────────

def test_parse_exif_datetime_with_offset_is_offset_aware():
    # Pin the offset so the result doesn't depend on the test host's TZ.
    ms = c._parse_exif_datetime("2024:05:18 12:00:00", "+00:00")
    assert ms == 1716033600000  # 2024-05-18T12:00:00Z in ms


def test_parse_exif_datetime_with_negative_offset():
    ms = c._parse_exif_datetime("2024:05:18 08:00:00", "-04:00")
    # 08:00 UTC-4 == 12:00 UTC, same instant as the test above.
    assert ms == 1716033600000


def test_parse_exif_datetime_without_offset_uses_local_time():
    # We can't pin the timezone in pure stdlib without zoneinfo + freezegun;
    # just assert we got *some* sensible ms and that it differs predictably
    # from the offset-aware case if the host isn't UTC.
    raw = "2024:05:18 12:00:00"
    naive_local = c._parse_exif_datetime(raw, None)
    assert isinstance(naive_local, int)
    assert naive_local > 0


def test_parse_exif_datetime_rejects_garbage():
    assert c._parse_exif_datetime("not a date", None) is None
    assert c._parse_exif_datetime("0000:00:00 00:00:00", None) is None
    assert c._parse_exif_datetime(None, None) is None
    assert c._parse_exif_datetime("    ", None) is None


# ── _read_local_exif ─────────────────────────────────────────────

def _make_jpeg_with_exif(
    tmp_path: Path,
    *,
    dt_original: str | None = None,
    offset: str | None = None,
    gps_lat: tuple | None = None,
    gps_lat_ref: str | None = None,
    gps_lon: tuple | None = None,
    gps_lon_ref: str | None = None,
    name: str = "photo.jpg",
) -> Path:
    img = Image.new("RGB", (32, 32), color=(0, 64, 128))
    exif = img.getexif()
    if dt_original:
        exif[c._EXIF_TAG_DATETIME_ORIGINAL] = dt_original
    if offset:
        exif[c._EXIF_TAG_OFFSET_TIME_ORIGINAL] = offset
    if gps_lat and gps_lat_ref and gps_lon and gps_lon_ref:
        gps = exif.get_ifd(c._EXIF_TAG_GPS_IFD)
        gps[c._EXIF_GPS_LAT] = gps_lat
        gps[c._EXIF_GPS_LAT_REF] = gps_lat_ref
        gps[c._EXIF_GPS_LON] = gps_lon
        gps[c._EXIF_GPS_LON_REF] = gps_lon_ref
    target = tmp_path / name
    img.save(target, format="JPEG", exif=exif.tobytes())
    return target


def test_read_local_exif_picks_up_date_and_gps(tmp_path: Path):
    p = _make_jpeg_with_exif(
        tmp_path,
        dt_original="2024:05:18 12:00:00",
        offset="+00:00",
        gps_lat=(37, 25, 19.07),
        gps_lat_ref="N",
        gps_lon=(122, 5, 6.0),
        gps_lon_ref="W",
    )
    info = c._read_local_exif(p)
    assert info["captured_at"] == 1716033600000
    assert info["latitude"] == pytest.approx(37.4220, abs=1e-3)
    assert info["longitude"] == pytest.approx(-122.085, abs=1e-3)


def test_read_local_exif_falls_back_to_mtime(tmp_path: Path):
    # JPEG with no EXIF date at all: we want a captured_at from mtime
    # so date-based sorting still works.
    p = tmp_path / "screenshot.jpg"
    Image.new("RGB", (10, 10)).save(p, format="JPEG")
    info = c._read_local_exif(p)
    assert "captured_at" in info
    assert info["captured_at"] > 0
    assert "latitude" not in info


def test_read_local_exif_null_island_is_dropped(tmp_path: Path):
    # (0, 0) is the most common bogus GPS stamp; treat as "no GPS".
    p = _make_jpeg_with_exif(
        tmp_path,
        gps_lat=(0, 0, 0),
        gps_lat_ref="N",
        gps_lon=(0, 0, 0),
        gps_lon_ref="E",
    )
    info = c._read_local_exif(p)
    assert "latitude" not in info
    assert "longitude" not in info


def test_read_local_exif_handles_non_image(tmp_path: Path):
    p = tmp_path / "garbage.jpg"
    p.write_bytes(b"this is not a JPEG")
    info = c._read_local_exif(p)
    # mtime fallback should still populate captured_at.
    assert "captured_at" in info
    assert "latitude" not in info


# ── _format_nominatim_location ─────────────────────────────────────────

def test_format_location_prefers_city():
    payload = {
        "address": {
            "city": "Lisbon",
            "town": "Should not appear",
            "country": "Portugal",
        }
    }
    assert c._format_nominatim_location(payload) == "Lisbon, Portugal"


def test_format_location_falls_back_through_town_to_municipality():
    payload = {
        "address": {"municipality": "Cascais", "country": "Portugal"}
    }
    assert c._format_nominatim_location(payload) == "Cascais, Portugal"


def test_format_location_uses_display_name_as_last_resort():
    payload = {
        "address": {},
        "display_name": "Mid Atlantic Ridge, Atlantic Ocean, Nowhere",
    }
    assert c._format_nominatim_location(payload) == "Mid Atlantic Ridge, Atlantic Ocean"


def test_format_location_returns_none_for_empty_payload():
    assert c._format_nominatim_location({}) is None
    assert c._format_nominatim_location(None) is None  # type: ignore[arg-type]


# ── _nominatim_lookup ─────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _FakeSession:
    def __init__(self, *responses):
        self._responses = list(responses)
        self.calls: list[tuple[str, dict, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if not self._responses:
            raise AssertionError("Unexpected extra call to fake session")
        return self._responses.pop(0)


def test_nominatim_lookup_success_returns_label():
    session = _FakeSession(
        _FakeResp(200, {"address": {"city": "Paris", "country": "France"}})
    )
    label = asyncio.run(
        c._nominatim_lookup(session, 48.8566, 2.3522, "album_slideshow/test")
    )
    assert label == "Paris, France"
    assert len(session.calls) == 1
    url, params, headers = session.calls[0]
    assert url == c._NOMINATIM_ENDPOINT
    assert params["format"] == "jsonv2"
    assert params["lat"] == "48.85660"
    assert params["lon"] == "2.35220"
    assert headers["User-Agent"] == "album_slideshow/test"


def test_nominatim_lookup_429_returns_none():
    session = _FakeSession(_FakeResp(429, None))
    label = asyncio.run(
        c._nominatim_lookup(session, 1.0, 2.0, "ua")
    )
    assert label is None


def test_nominatim_lookup_5xx_returns_none():
    session = _FakeSession(_FakeResp(503, None))
    label = asyncio.run(
        c._nominatim_lookup(session, 1.0, 2.0, "ua")
    )
    assert label is None


def test_nominatim_lookup_bad_json_returns_none():
    session = _FakeSession(_FakeResp(200, ValueError("bad json")))
    label = asyncio.run(
        c._nominatim_lookup(session, 1.0, 2.0, "ua")
    )
    assert label is None


def test_nominatim_lookup_session_exception_returns_none():
    class _RaisingSession:
        async def get(self, *a, **kw):
            raise RuntimeError("network down")

    label = asyncio.run(
        c._nominatim_lookup(_RaisingSession(), 1.0, 2.0, "ua")
    )
    assert label is None


# ── _merge_prior_enrichment ─────────────────────────────────────────

def _item(url: str, **kw) -> c.MediaItem:
    return c.MediaItem(
        url=url, width=None, height=None, mime_type=None, filename=None, **kw
    )


def test_merge_prior_enrichment_carries_metadata_by_url():
    prior = [
        _item(
            "file:///a.jpg",
            captured_at=111,
            latitude=1.0,
            longitude=2.0,
            location="Somewhere",
            exif_scanned=True,
        )
    ]
    new = [_item("file:///a.jpg"), _item("file:///b.jpg")]
    c._merge_prior_enrichment(new, prior)
    assert new[0].captured_at == 111
    assert new[0].latitude == 1.0
    assert new[0].location == "Somewhere"
    assert new[0].exif_scanned is True
    # New file untouched.
    assert new[1].captured_at is None
    assert new[1].exif_scanned is False


def test_merge_prior_enrichment_does_not_overwrite_fresh_values():
    prior = [_item("file:///a.jpg", captured_at=111, location="Old")]
    new = [_item("file:///a.jpg", captured_at=222, location="New")]
    c._merge_prior_enrichment(new, prior)
    # Fresh value wins; merge is purely a backfill.
    assert new[0].captured_at == 222
    assert new[0].location == "New"


# ── items-cache round trip ──────────────────────────────────────────

class _RecordingStore:
    def __init__(self) -> None:
        self.saved = None

    async def async_load(self):
        return self.saved

    async def async_save(self, payload):
        self.saved = payload


def _stub_coordinator():
    """Build the smallest coordinator-like object that supports the cache
    round-trip methods. We bypass ``AlbumCoordinator.__init__`` entirely so
    we don't need a real ``hass``/``entry``/``store``.
    """
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord._items_cache_store = _RecordingStore()
    coord._items_cache_loaded = False
    return coord


def test_save_and_load_round_trips_gps_and_scanned_flag():
    coord = _stub_coordinator()
    items = [
        _item(
            "file:///vacation.jpg",
            captured_at=1716033600000,
            latitude=37.4220,
            longitude=-122.0850,
            location="Mountain View, USA",
            exif_scanned=True,
            byte_size=4567,
        ),
        _item("file:///unscanned.jpg"),
    ]
    asyncio.run(
        coord._save_cached_items({"title": "Vacation", "items": items})
    )
    loaded = asyncio.run(coord._load_cached_items())
    assert loaded is not None
    assert loaded["title"] == "Vacation"
    out = loaded["items"]
    assert len(out) == 2
    assert out[0].latitude == pytest.approx(37.4220)
    assert out[0].longitude == pytest.approx(-122.0850)
    assert out[0].location == "Mountain View, USA"
    assert out[0].exif_scanned is True
    assert out[0].byte_size == 4567
    # Unscanned item keeps its defaults.
    assert out[1].latitude is None
    assert out[1].exif_scanned is False


# ── geocode opt-out via entry.options ──────────────────────────────────

def test_geocode_phase_respects_opt_out():
    """When ``reverse_geocode`` is False in entry.options, the geocode
    pass must be a no-op even if items have GPS coordinates."""
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord.entry = SimpleNamespace(options={CONF_REVERSE_GEOCODE: False})
    coord._enrich_progress = {
        "phase": "geocoding",
        "exif_total": 0,
        "exif_done": 0,
        "geocode_total": 0,
        "geocode_done": 0,
    }
    items = [_item("file:///x.jpg", latitude=1.0, longitude=2.0)]
    data = {"items": items}

    # Should return immediately without touching ``location``.
    asyncio.run(
        coord._geocode_items_background(data)
    )
    assert items[0].location is None


# ── _read_manifest_version ──────────────────────────────────────────

def test_read_manifest_version_returns_string_or_empty(tmp_path: Path):
    (tmp_path / "manifest.json").write_text(json.dumps({"version": "9.9.9"}))
    assert c._read_manifest_version(tmp_path) == "9.9.9"


def test_read_manifest_version_missing_returns_empty(tmp_path: Path):
    # No manifest at all -> empty string, never raises.
    assert c._read_manifest_version(tmp_path) == ""
