from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow import google_scraper
from custom_components.album_slideshow.coordinator import (
    MediaItem,
    _enrich_missing_dates,
    _photo_base_key,
    _pick_timestamp_ms,
)


# -- _photo_base_key --------------------------------------------------------

def test_base_key_strips_size_suffix():
    a = _photo_base_key("https://lh3.googleusercontent.com/abc123=w1920-h1080")
    b = _photo_base_key("https://lh3.googleusercontent.com/abc123=w640-h480-no")
    assert a == b == "https://lh3.googleusercontent.com/abc123"


def test_base_key_strips_query_string():
    a = _photo_base_key("https://lh3.googleusercontent.com/abc123=w1920-h1080")
    b = _photo_base_key("https://lh3.googleusercontent.com/abc123?authuser=0")
    assert a == b == "https://lh3.googleusercontent.com/abc123"


def test_base_key_handles_none_and_empty():
    assert _photo_base_key(None) is None
    assert _photo_base_key("") is None


# -- _enrich_missing_dates --------------------------------------------------

def _item(base: str, captured=None, uploaded=None, size="=w1920-h1080"):
    return MediaItem(
        url=f"{base}{size}",
        width=None,
        height=None,
        mime_type=None,
        filename=None,
        captured_at=captured,
        uploaded_at=uploaded,
    )


def test_enrich_backfills_dates_from_scraped_twin():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, size="=w640-h480")]  # publicalbum: no dates, different size
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 1
    assert api[0].captured_at == 1000
    assert api[0].uploaded_at == 2000


def test_enrich_does_not_overwrite_existing_dates():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, captured=111, uploaded=222)]
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 0
    assert api[0].captured_at == 111
    assert api[0].uploaded_at == 222


def test_enrich_fills_only_missing_field():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, captured=111, uploaded=None)]
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 1
    # captured_at is kept, only the missing uploaded_at is filled.
    assert api[0].captured_at == 111
    assert api[0].uploaded_at == 2000


def test_enrich_leaves_unmatched_items_untouched():
    api = [_item("https://lh3.googleusercontent.com/only_in_api")]
    scraped = [_item("https://lh3.googleusercontent.com/only_in_scrape", captured=1000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 0
    assert api[0].captured_at is None
    assert api[0].uploaded_at is None


def test_enrich_noop_when_a_source_empty():
    scraped = [_item("https://lh3.googleusercontent.com/photo1", captured=1000)]
    assert _enrich_missing_dates([], scraped) == 0
    assert _enrich_missing_dates(scraped, []) == 0


def _google_coordinator(monkeypatch, *, enabled=True):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(coordinator_module.aiohttp, "ClientSession", lambda **kwargs: Session())
    monkeypatch.setattr(coordinator_module, "_GOOGLE_METADATA_INTERVAL", 0)
    monkeypatch.setattr(google_scraper, "fetch_metadata_keys", AsyncMock(return_value=object()))
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.provider = "google_shared"
    coord.album_url = "https://photos.example.test/album"
    coord.entry = SimpleNamespace(entry_id="test", options={"google_metadata": enabled})
    coord._enrich_progress = {"phase": "exif", "exif_done": 0}
    coord._items_cache_store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock(return_value=None))
    coord._items_cache_loaded = False
    coord._enrichment_task = None
    coord.hass = SimpleNamespace(async_add_executor_job=AsyncMock(
        side_effect=lambda job, *args: job(*args),
    ))
    coord.data = {}
    coord.async_set_updated_data = lambda data: setattr(coord, "data", data)
    return coord


def _google_photo(source_id="photo-key", **kwargs):
    return MediaItem(
        url=f"https://example.test/{source_id}", width=1000, height=800,
        mime_type=None, filename=None, source_id=source_id, **kwargs,
    )


def test_google_metadata_explicit_off_prevents_requests(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord._update_google_shared = AsyncMock(return_value={"items": [_google_photo()]})

    result = asyncio.run(coord._async_update_data())

    assert len(result["items"]) == 1
    assert coord._needs_enrichment(result["items"][0]) is False
    google_scraper.fetch_metadata_keys.assert_not_awaited()
    assert coord._enrichment_task is None


@pytest.mark.parametrize(("options", "enabled"), [
    ({}, True), ({"google_metadata": True}, True), ({"google_metadata": False}, False),
])
def test_google_metadata_defaults_on_and_respects_saved_options(monkeypatch, options, enabled):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options = options

    assert coord.google_metadata_enabled is enabled
    assert coord._needs_enrichment(_google_photo()) is enabled


@pytest.mark.parametrize("provider", ["local_folder", "immich", "photoprism", "icloud", "ente"])
def test_google_metadata_default_does_not_enable_other_providers(monkeypatch, provider):
    coord = _google_coordinator(monkeypatch)
    coord.provider = provider
    coord.entry.options = {}

    assert coord.google_metadata_enabled is False


@pytest.mark.parametrize(("options", "enabled"), [
    ({}, True), ({"google_metadata": True}, True), ({"google_metadata": False}, False),
])
def test_google_options_default_on_and_preserve_other_options(options, enabled):
    from custom_components.album_slideshow import config_flow

    entry = SimpleNamespace(
        data={"provider": "google_shared"}, options={"other": "preserved", **options},
    )
    flow = config_flow.ConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, config_flow.GoogleOptionsFlow)
    flow.config_entry = entry
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs

    form = asyncio.run(flow.async_step_init())
    assert form["data_schema"]({}) == {
        "google_metadata": enabled, "google_location": False, "reverse_geocode": False,
        "hide_home_country": False,
    }
    submit = getattr(flow, f"async_step_{form['step_id']}")
    saved = asyncio.run(submit({"google_metadata": not enabled}))
    assert saved["data"] == {
        "google_metadata": not enabled, "other": "preserved",
        "google_location": False, "reverse_geocode": False, "hide_home_country": False,
    }


def test_google_options_preserve_independent_location_choices():
    from custom_components.album_slideshow import config_flow

    entry = SimpleNamespace(data={"provider": "google_shared"}, options={
        "google_metadata": False, "google_location": True, "reverse_geocode": True,
        "hide_home_country": True, "other": "preserved",
    })
    flow = config_flow.ConfigFlow.async_get_options_flow(entry)
    flow.config_entry = entry
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    form = asyncio.run(flow.async_step_init())
    assert form["data_schema"]({}) == {
        "google_metadata": False, "google_location": True, "reverse_geocode": True,
        "hide_home_country": True,
    }
    submit = getattr(flow, f"async_step_{form['step_id']}")
    saved = asyncio.run(submit({"google_metadata": True}))
    assert saved["data"] == {**entry.options, "google_metadata": True}
    saved = asyncio.run(submit({"google_location": False, "reverse_geocode": False}))
    assert saved["data"] == {
        "google_metadata": False, "google_location": False, "reverse_geocode": False,
        "hide_home_country": True, "other": "preserved",
    }


def test_google_options_disclose_location_privacy_in_matching_translations():
    import json
    from pathlib import Path

    root = Path("custom_components/album_slideshow")
    strings = json.loads((root / "strings.json").read_text())
    translations = json.loads((root / "translations/en.json").read_text())
    options = strings["options"]["step"]["google_metadata"]
    assert options == translations["options"]["step"]["google_metadata"]
    assert set(options["data"]) == {
        "google_metadata", "google_location", "reverse_geocode", "hide_home_country",
    }
    for disclosure in ["off by default", "256 KB", "precise GPS", "location sharing", "Nominatim", "history or backups"]:
        assert disclosure in options["description"]


def test_google_enrichment_persists_success_and_empty_results(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    metadata = AsyncMock(side_effect=[{
        "camera_model": "Test camera", "iso": 100, "description": "Summer by the lake",
    }, {}])
    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", metadata)
    items = [_google_photo("first"), _google_photo("second")]

    asyncio.run(coord._enrich_google_items_background({"items": items}))

    assert items[0].camera_metadata == {"camera_model": "Test camera", "iso": 100}
    assert items[0].description == "Summer by the lake"
    assert items[1].camera_metadata == {}
    assert items[1].description is None
    assert all(not coord._needs_enrichment(item) for item in items)
    saved = coord._items_cache_store.async_save.call_args.args[0]
    coord._items_cache_store.async_load.return_value = saved
    loaded = asyncio.run(coord._load_cached_items())
    assert loaded["items"][0].camera_metadata == items[0].camera_metadata
    assert loaded["items"][0].description == "Summer by the lake"
    assert loaded["items"][1].camera_metadata == {}


def test_google_enrichment_failure_keeps_photos_and_is_retryable(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    metadata = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", metadata)
    items = [_google_photo(str(index)) for index in range(5)]

    asyncio.run(coord._enrich_google_items_background({"items": items}))

    assert metadata.await_count == 3
    assert all(coord._needs_enrichment(item) for item in items)
    assert len(coord._items_cache_store.async_save.call_args.args[0]["items"]) == 5


def test_google_enrichment_cancellation_does_not_mark_scan_complete(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", AsyncMock(side_effect=asyncio.CancelledError()))
    item = _google_photo()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.camera_metadata is None


def test_google_enrichment_cache_reuses_stable_ids_after_url_changes(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    coord._items_cache_store.async_load.return_value = {
        "items": [{"url": "old-url", "source_id": "photo-key", "camera_metadata": {"iso": 50},
                   "description": "Cached caption"}],
    }
    coord._update_google_shared = AsyncMock(return_value={"items": [_google_photo()]})

    result = asyncio.run(coord._async_update_data())

    assert result["items"][0].camera_metadata == {"iso": 50}
    assert result["items"][0].description == "Cached caption"
    google_scraper.fetch_metadata_keys.assert_not_awaited()
    assert coord._enrichment_task is None


@pytest.mark.parametrize("options", [{}, {"google_location": False}, {"reverse_geocode": True}])
def test_google_location_requires_separate_explicit_opt_in(monkeypatch, options):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options = options
    header = AsyncMock()
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", header)
    item = _google_photo(camera_metadata={})

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert coord.google_location_enabled is False
    assert coord.google_reverse_geocode_enabled is False
    assert coord._needs_enrichment(item) is False
    header.assert_not_awaited()


def test_google_location_is_independent_of_camera_metadata(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    header = AsyncMock(return_value=b"image prefix")
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", header)
    monkeypatch.setattr(coordinator_module, "_read_exif_from_bytes", lambda *args, **kwargs: {
        "latitude": 37.4, "longitude": -122.1, "description": "Must not replace Google caption",
        "captured_at": 999,
    })
    coord._geocode_items_background = AsyncMock()
    item = _google_photo(description="Google caption", captured_at=123)

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert (item.latitude, item.longitude) == (37.4, -122.1)
    assert item.google_location_scanned is True
    assert item.camera_metadata is None
    assert item.description == "Google caption"
    assert item.captured_at == 123
    assert not coord._needs_enrichment(item)
    google_scraper.fetch_metadata_keys.assert_not_awaited()
    coord._geocode_items_background.assert_not_awaited()
    saved = coord._items_cache_store.async_save.call_args.args[0]
    coord._items_cache_store.async_load.return_value = saved
    loaded = asyncio.run(coord._load_cached_items())["items"][0]
    assert loaded.google_location_scanned is True
    assert (loaded.latitude, loaded.longitude) == (37.4, -122.1)


@pytest.mark.parametrize("location", [
    {}, {"latitude": 0.0, "longitude": 0.0}, {"latitude": 100.0, "longitude": 5.0},
    {"latitude": True, "longitude": 5.0}, {"latitude": 30.0, "longitude": float("nan")},
])
def test_google_successful_read_without_valid_gps_is_cached(monkeypatch, location):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", AsyncMock(return_value=b"prefix"))
    monkeypatch.setattr(coordinator_module, "_read_exif_from_bytes", lambda *args, **kwargs: location)
    item = _google_photo()

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.google_location_scanned is True
    assert item.latitude is item.longitude is None
    assert not coord._needs_enrichment(item)


def test_google_gps_failure_keeps_successful_camera_metadata_retryable(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options["google_location"] = True
    metadata = AsyncMock(return_value={"iso": 100})
    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", metadata)
    header = AsyncMock(side_effect=TimeoutError())
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", header)
    item = _google_photo()

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))
    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.camera_metadata == {"iso": 100}
    assert item.google_location_scanned is False
    assert coord._needs_enrichment(item)
    assert metadata.await_count == 1
    assert header.await_count == 2


def test_google_metadata_failure_does_not_block_gps(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options["google_location"] = True
    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", AsyncMock(side_effect=TimeoutError()))
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", AsyncMock(return_value=b"prefix"))
    monkeypatch.setattr(coordinator_module, "_read_exif_from_bytes", lambda *args, **kwargs: {
        "latitude": 37.4, "longitude": -122.1,
    })
    item = _google_photo()

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.camera_metadata is None
    assert item.google_location_scanned is True
    assert item.latitude == 37.4
    assert coord._needs_enrichment(item)


def test_google_location_cancellation_leaves_scan_retryable(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", AsyncMock(side_effect=asyncio.CancelledError()))
    item = _google_photo()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.google_location_scanned is False
    assert item.latitude is None


def test_google_truncated_header_does_not_cache_missing_gps(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", AsyncMock(return_value=b"truncated"))
    item = _google_photo()

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.google_location_scanned is False
    assert coord._needs_enrichment(item)


_GPS = {"latitude": pytest.approx(37.41667, abs=1e-4), "longitude": pytest.approx(-122.08333, abs=1e-4)}


def _gps_exif() -> bytes:
    exif = Image.Exif()
    gps = exif.get_ifd(coordinator_module._EXIF_TAG_GPS_IFD)
    gps[coordinator_module._EXIF_GPS_LAT] = (37.0, 25.0, 0.0)
    gps[coordinator_module._EXIF_GPS_LAT_REF] = "N"
    gps[coordinator_module._EXIF_GPS_LON] = (122.0, 5.0, 0.0)
    gps[coordinator_module._EXIF_GPS_LON_REF] = "W"
    return exif.tobytes()


def _jpeg(exif: bytes | None = None, *, padding: int = 0) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8)).save(output, "JPEG", **({"exif": exif} if exif else {}))
    data = output.getvalue()
    tables = data.index(b"\xff\xdb")
    return data[:tables] + (b"\xff\xeb\xff\xff" + bytes(65533)) * padding + data[tables:]


def test_google_gps_is_read_when_later_metadata_exceeds_the_header(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    header = _jpeg(_gps_exif(), padding=5)[:google_scraper._LOCATION_HEADER_BYTES]
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", AsyncMock(return_value=header))
    item = _google_photo()

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert item.google_location_scanned is True
    assert {"latitude": item.latitude, "longitude": item.longitude} == _GPS


def test_unreadable_google_photos_do_not_stop_the_gps_scan(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    header = AsyncMock(side_effect=[b"unreadable"] * 3 + [_jpeg(_gps_exif())])
    monkeypatch.setattr(google_scraper, "fetch_photo_location_header", header)
    items = [_google_photo(str(index)) for index in range(4)]

    asyncio.run(coord._enrich_google_items_background({"items": items}))

    assert header.await_count == 4
    assert [item.google_location_scanned for item in items] == [False, False, False, True]
    assert {"latitude": items[3].latitude, "longitude": items[3].longitude} == _GPS


def test_google_gps_is_read_from_heic_originals():
    header = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + bytes(64) + b"\x00\x00\x00\x06" + _gps_exif()

    assert coordinator_module._read_gps_from_header(header) == _GPS


def test_google_gps_reader_distinguishes_missing_from_truncated_exif():
    assert coordinator_module._read_gps_from_header(_jpeg()) == {}
    for truncated in (_jpeg()[:20], _jpeg(_gps_exif())[:40]):
        with pytest.raises(ValueError):
            coordinator_module._read_gps_from_header(truncated)


def test_google_location_cache_reuses_photo_identity(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options["google_location"] = True
    coord._items_cache_store.async_load.return_value = {"items": [{
        "url": "old-url", "source_id": "photo-key", "google_location_scanned": True,
        "latitude": 37.4, "longitude": -122.1,
    }]}
    coord._update_google_shared = AsyncMock(return_value={"items": [_google_photo()]})

    result = asyncio.run(coord._async_update_data())

    assert result["items"][0].google_location_scanned is True
    assert result["items"][0].latitude == 37.4
    assert coord._enrichment_task is None


@pytest.mark.parametrize("location_enabled,geocoding_enabled", [(False, True), (True, False)])
def test_google_opt_out_hides_cached_location_even_when_album_is_offline(
    monkeypatch, location_enabled, geocoding_enabled,
):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options.update(google_location=location_enabled, reverse_geocode=geocoding_enabled)
    coord._items_cache_store.async_load.return_value = {"items": [{
        "url": "old-url", "source_id": "photo-key", "google_location_scanned": True,
        "latitude": 37.4, "longitude": -122.1, "location": "Cached place",
    }]}
    coord._update_google_shared = AsyncMock(side_effect=coordinator_module.UpdateFailed("offline"))

    item = asyncio.run(coord._async_update_data())["items"][0]

    assert item.location is None
    assert item.google_location_scanned is location_enabled
    assert item.latitude == (37.4 if location_enabled else None)
    assert item.longitude == (-122.1 if location_enabled else None)


@pytest.mark.parametrize("location_enabled,geocoding_enabled,called", [
    (False, False, False), (False, True, False), (True, False, False), (True, True, True),
])
def test_google_place_names_require_both_opt_ins(monkeypatch, location_enabled, geocoding_enabled, called):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options.update(google_location=location_enabled, reverse_geocode=geocoding_enabled)
    coord._geocode_items_background = AsyncMock()
    item = _google_photo(google_location_scanned=True, latitude=37.4, longitude=-122.1)

    asyncio.run(coord._enrich_google_items_background({"items": [item]}))

    assert coord._geocode_items_background.await_count == int(called)
    assert coord._enrich_progress["phase"] == "done"


def test_google_geocode_worker_itself_enforces_opt_in(monkeypatch):
    coord = _google_coordinator(monkeypatch)
    coord.entry.options["reverse_geocode"] = True
    coord._ensure_geocode_cache_loaded = AsyncMock()
    item = _google_photo(latitude=37.4, longitude=-122.1)

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    coord._ensure_geocode_cache_loaded.assert_not_awaited()


def test_google_place_names_follow_label_options_without_lookups(monkeypatch):
    coord = _google_coordinator(monkeypatch, enabled=False)
    coord.entry.options.update(google_location=True, reverse_geocode=True, hide_home_country=True)
    coord.hass.config = SimpleNamespace(country="CA")
    coord._geocode_cache_store = SimpleNamespace(async_load=AsyncMock(return_value={"entries": {
        "45.964,-66.643": {"city": "Fredericton", "country": "Canada", "country_code": "ca"},
    }}))
    coord._geocode_cache = {}
    coord._geocode_legacy = {}
    coord._geocode_cache_loaded = False
    coord._items_cache_store.async_load.return_value = {"items": [{
        "url": "old-url", "source_id": "photo-key", "google_location_scanned": True,
        "latitude": 45.9636, "longitude": -66.6431, "location": "Fredericton, Canada",
        "location_geocoded": True,
    }]}
    coord._update_google_shared = AsyncMock(return_value={"items": [_google_photo()]})

    item = asyncio.run(coord._async_update_data())["items"][0]

    assert item.location == "Fredericton"
    assert coord._enrichment_task is None


@pytest.mark.parametrize("year", [1800, 1900, 1950, 1965, 1971, 1980, 1995, 1999, 2000, 2026])
@pytest.mark.parametrize("encoding", ["seconds", "milliseconds", "iso"])
def test_fallback_preserves_historical_capture_dates(year, encoding):
    captured = datetime(year, 6, 1, tzinfo=timezone.utc)
    milliseconds = int(captured.timestamp() * 1000)
    values = {
        "seconds": milliseconds // 1000,
        "milliseconds": milliseconds,
        "iso": captured.isoformat(),
    }

    assert _pick_timestamp_ms({"creationTime": values[encoding]}, "creationTime") == milliseconds


@pytest.mark.parametrize("value", [None, True, False, "not a date", [], {}])
def test_fallback_rejects_invalid_capture_dates(value):
    assert _pick_timestamp_ms({"creationTime": value}, "creationTime") is None


# -- ENRICHING_PROVIDERS ----------------------------------------------------
# The Enrichment progress sensor is created from this tuple, so it has to stay
# in step with the providers the coordinator actually schedules work for.

def test_enriching_providers_matches_documented_set():
    from custom_components.album_slideshow.const import (
        ENRICHING_PROVIDERS,
        PROVIDER_ENTE,
        PROVIDER_GOOGLE_SHARED,
        PROVIDER_IMMICH,
        PROVIDER_LOCAL_FOLDER,
        PROVIDER_MEDIA_SOURCE,
        PROVIDER_NEXTCLOUD,
        PROVIDER_UGREEN,
    )

    assert set(ENRICHING_PROVIDERS) == {
        PROVIDER_LOCAL_FOLDER,
        PROVIDER_IMMICH,
        PROVIDER_NEXTCLOUD,
        PROVIDER_ENTE,
        PROVIDER_UGREEN,
    }
    # Providers with no metadata to enrich must stay out, or they'd get a
    # progress sensor that never moves.
    assert PROVIDER_GOOGLE_SHARED not in ENRICHING_PROVIDERS
    assert PROVIDER_MEDIA_SOURCE not in ENRICHING_PROVIDERS


def test_sensor_platform_uses_the_shared_enrichment_tuple():
    import pathlib

    src = pathlib.Path(
        "custom_components/album_slideshow/sensor.py"
    ).read_text()
    assert "coordinator.provider in ENRICHING_PROVIDERS" in src


# -- publicalbum.org video filtering (#26) ----------------------------------
# publicalbum.org returns mimetype/mediaMetadata as null, verified against a
# real shared album, so it can only drop videos by reusing the scraper's keys.

def test_looks_like_video_accepts_lowercase_mimetype():
    from custom_components.album_slideshow.coordinator import _looks_like_video

    # publicalbum.org spells it lowercase; Google's own shapes use camelCase.
    assert _looks_like_video({"mimetype": "video/mp4"}) is True
    assert _looks_like_video({"mimeType": "video/mp4"}) is True
    assert _looks_like_video({"mimetype": "image/jpeg"}) is False


def test_looks_like_video_cannot_detect_a_publicalbum_video():
    from custom_components.album_slideshow.coordinator import _looks_like_video

    # This is the exact shape a real album returned for a video: no signal at
    # all. It documents why the media-key fallback exists.
    raw = {
        "id": "AF1QipOVuW0YZ1YRJ-dHFC8FZ1wp0JEjwAHdYIG2CF_v",
        "description": None,
        "url": "https://lh3.googleusercontent.com/pw/AP1GczPO7xue=w1920-h1080",
        "mimetype": None,
        "mediaMetadata": None,
    }
    assert _looks_like_video(raw) is False


# -- config flow diagnostics (#30, #34) -------------------------------------
# Every provider validation step used to swallow its exception, so users kept
# reporting "nothing in the logs". These pin the helpers that fixed that.

def _load_config_flow():
    import importlib, sys, types

    sys.modules["homeassistant.helpers"].selector = types.ModuleType("selector")
    return importlib.import_module("custom_components.album_slideshow.config_flow")


def test_describe_error_includes_http_status_when_present():
    cf = _load_config_flow()

    class Boom(Exception):
        status = 403

    assert cf._describe_error(Boom("Forbidden")) == "HTTP 403: Forbidden"
    assert cf._describe_error(ValueError("nope")) == "ValueError: nope"


def test_redact_token_never_leaks_the_whole_token():
    cf = _load_config_flow()
    token = "045YeI20-8u3X31bBPD5z9B_A"
    out = cf._redact_token(token)
    assert token not in out
    assert out.startswith("045")
    # The shape is what matters for debugging: length and where -/_ sit.
    assert f"{len(token)} chars" in out
    assert "-" in out
    assert cf._redact_token("") == "<empty>"
    assert cf._redact_token(None) == "<empty>"


def test_every_provider_validation_logs_its_failure():
    import pathlib
    import re

    src = pathlib.Path(
        "custom_components/album_slideshow/config_flow.py"
    ).read_text()
    # A bare "except Exception:" means the cause is being discarded again.
    assert not re.search(r"except Exception:\s*(#.*)?\n\s+errors\[", src)
