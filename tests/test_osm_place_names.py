"""OpenStreetMap place names in place of the photo service's own.

Immich names a place after the nearest town in its own list, which can be a
neighbouring town. The ``osm_place_names`` option lets OpenStreetMap's name
(which follows town boundaries) replace it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import config_flow as cf
from custom_components.album_slideshow import coordinator as c
from custom_components.album_slideshow import immich
from custom_components.album_slideshow.const import (
    CONF_HIDE_HOME_COUNTRY,
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_SELECTION_ID,
    CONF_IMMICH_SELECTION_TYPE,
    CONF_IMMICH_URL,
    CONF_OSM_PLACE_NAMES,
    CONF_REVERSE_GEOCODE,
    DEFAULT_OSM_PLACE_NAMES,
)
from tests.test_immich_options_flow import _defaults, _entry_data, _flow
from tests.test_local_enrichment import _geocode_coordinator, _item

_SPOT = (48.8566, 2.3522)
_PARIS = {"city": "Paris", "state": "Ile-de-France",
          "country": "France", "country_code": "fr"}


def _immich_item(**kw):
    return _item("http://immich.test/a1", latitude=_SPOT[0], longitude=_SPOT[1],
                 location="Montreuil, France", **kw)


def _coordinator(monkeypatch, places, **options):
    coord, lookup = _geocode_coordinator(monkeypatch, places, options=options)
    coord.provider = "immich"
    return coord, lookup


def test_option_is_off_by_default():
    assert DEFAULT_OSM_PLACE_NAMES is False


@pytest.mark.parametrize("options", [{}, {CONF_OSM_PLACE_NAMES: False}])
def test_off_keeps_the_services_place_name(monkeypatch, options):
    coord, lookup = _coordinator(monkeypatch, {_SPOT: _PARIS}, **options)
    item = _immich_item()

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    lookup.assert_not_awaited()
    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)
    assert not coord._needs_geocode_lookup(item)


def test_on_replaces_the_services_place_name(monkeypatch):
    coord, lookup = _coordinator(monkeypatch, {_SPOT: _PARIS}, **{CONF_OSM_PLACE_NAMES: True})
    item = _immich_item()
    assert coord._needs_geocode_lookup(item)

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    lookup.assert_awaited_once()
    assert (item.location, item.location_geocoded) == ("Paris, France", True)
    assert not coord._needs_geocode_lookup(item)


def test_on_honours_hide_home_country(monkeypatch):
    coord, _ = _coordinator(
        monkeypatch, {_SPOT: _PARIS},
        **{CONF_OSM_PLACE_NAMES: True, CONF_HIDE_HOME_COUNTRY: True},
    )
    coord.hass.config.country = "FR"
    item = _immich_item()

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    assert item.location == "Paris"


def test_failed_lookup_keeps_the_services_name_and_retries(monkeypatch):
    coord, lookup = _coordinator(monkeypatch, {}, **{CONF_OSM_PLACE_NAMES: True})
    item = _immich_item()

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    lookup.assert_awaited_once()
    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)
    assert coord._needs_geocode_lookup(item)


def test_place_without_a_usable_name_keeps_the_services_name(monkeypatch):
    coord, _ = _coordinator(monkeypatch, {_SPOT: {"country_code": "fr"}},
                            **{CONF_OSM_PLACE_NAMES: True})
    item = _immich_item()

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)


def test_cached_place_replaces_the_name_without_a_lookup(monkeypatch):
    coord, lookup = _coordinator(monkeypatch, {}, **{CONF_OSM_PLACE_NAMES: True})
    coord._geocode_cache_store.saved = {"entries": {c._geocode_cache_key(*_SPOT): _PARIS}}
    item = _immich_item()

    asyncio.run(coord._relabel_from_place_cache([item]))

    lookup.assert_not_awaited()
    assert (item.location, item.location_geocoded) == ("Paris, France", True)


def test_cached_place_without_a_usable_name_keeps_the_services_name(monkeypatch):
    coord, _ = _coordinator(monkeypatch, {}, **{CONF_OSM_PLACE_NAMES: True})
    coord._geocode_cache_store.saved = {"entries": {c._geocode_cache_key(*_SPOT): {"country_code": "fr"}}}
    item = _immich_item()

    asyncio.run(coord._relabel_from_place_cache([item]))

    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)


def test_old_label_only_cache_entry_replaces_the_services_name(monkeypatch):
    coord, _ = _coordinator(monkeypatch, {}, **{CONF_OSM_PLACE_NAMES: True})
    coord._geocode_cache_store.saved = {"entries": {c._geocode_cache_key(*_SPOT): "Paris, France"}}
    item = _immich_item()

    asyncio.run(coord._relabel_from_place_cache([item]))

    assert (item.location, item.location_geocoded) == ("Paris, France", True)


def test_needs_reverse_geocoding_on(monkeypatch):
    coord, lookup = _coordinator(
        monkeypatch, {_SPOT: _PARIS},
        **{CONF_OSM_PLACE_NAMES: True, CONF_REVERSE_GEOCODE: False},
    )
    item = _immich_item()

    asyncio.run(coord._geocode_items_background({"items": [item]}))

    lookup.assert_not_awaited()
    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)


# ── Immich enrichment ─────────────────────────────────────────────────────

def _enrich(monkeypatch, item, **options):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"exifInfo": {"latitude": _SPOT[0], "longitude": _SPOT[1],
                                 "city": "Montreuil", "country": "France"}}

        async def async_get_faces(self, _asset_id):
            return []

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord.provider = "immich"
    coord.hass = SimpleNamespace()
    coord.entry = SimpleNamespace(entry_id="e1", options=options, data={
        CONF_IMMICH_URL: "http://immich.test", CONF_IMMICH_API_KEY: "secret",
        CONF_IMMICH_SELECTION_TYPE: "composite", CONF_IMMICH_SELECTION_ID: "{}",
    })
    asyncio.run(coord._enrich_immich_item(item))


def test_rereading_immich_keeps_an_openstreetmap_name_when_on(monkeypatch):
    item = _item("http://immich.test/a1", source_id="a1", latitude=_SPOT[0], longitude=_SPOT[1],
                 location="Paris, France", location_geocoded=True)

    _enrich(monkeypatch, item, **{CONF_OSM_PLACE_NAMES: True})

    assert item.face_scanned
    assert (item.location, item.location_geocoded) == ("Paris, France", True)


def test_rereading_immich_restores_its_name_when_off(monkeypatch):
    item = _item("http://immich.test/a1", source_id="a1", latitude=_SPOT[0], longitude=_SPOT[1],
                 location="Paris, France", location_geocoded=True)

    _enrich(monkeypatch, item)

    assert (item.location, item.location_geocoded) == ("Montreuil, France", False)


# ── Immich options ────────────────────────────────────────────────────────

def test_options_offer_the_switch_off_by_default(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    form = asyncio.run(flow.async_step_init())
    assert _defaults(form)[CONF_OSM_PLACE_NAMES] is False


def test_turning_it_on_saves_without_clearing_the_cache(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    clear = AsyncMock()
    monkeypatch.setattr(cf, "_async_clear_item_cache", clear)
    values = _defaults(asyncio.run(flow.async_step_init()))
    values[CONF_OSM_PLACE_NAMES] = True

    result = asyncio.run(flow.async_step_immich_select(values))

    assert result["data"][CONF_OSM_PLACE_NAMES] is True
    assert flow.hass.config_entries.updates[0]["options"][CONF_OSM_PLACE_NAMES] is True
    clear.assert_not_awaited()


def test_turning_it_off_clears_the_cache_so_immich_names_return(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    flow.config_entry.options = {CONF_OSM_PLACE_NAMES: True}
    clear = AsyncMock()
    monkeypatch.setattr(cf, "_async_clear_item_cache", clear)
    values = _defaults(asyncio.run(flow.async_step_init()))
    assert values[CONF_OSM_PLACE_NAMES] is True
    values[CONF_OSM_PLACE_NAMES] = False

    result = asyncio.run(flow.async_step_immich_select(values))

    assert result["data"][CONF_OSM_PLACE_NAMES] is False
    clear.assert_awaited_once()


def test_turning_geocoding_off_also_brings_immich_names_back(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    flow.config_entry.options = {CONF_OSM_PLACE_NAMES: True, CONF_REVERSE_GEOCODE: True}
    clear = AsyncMock()
    monkeypatch.setattr(cf, "_async_clear_item_cache", clear)
    values = _defaults(asyncio.run(flow.async_step_init()))
    values[CONF_REVERSE_GEOCODE] = False

    asyncio.run(flow.async_step_immich_select(values))

    clear.assert_awaited_once()


def test_unrelated_option_change_keeps_the_cache(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    flow.config_entry.options = {CONF_OSM_PLACE_NAMES: True}
    clear = AsyncMock()
    monkeypatch.setattr(cf, "_async_clear_item_cache", clear)
    values = _defaults(asyncio.run(flow.async_step_init()))
    values[CONF_HIDE_HOME_COUNTRY] = True

    asyncio.run(flow.async_step_immich_select(values))

    clear.assert_not_awaited()
