from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import config_flow as cf
from custom_components.album_slideshow import immich
from custom_components.album_slideshow.const import (
    CONF_ALBUM_NAME,
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_FILTER,
    CONF_IMMICH_IMAGE_SIZE,
    CONF_IMMICH_SELECTION_ID,
    CONF_IMMICH_SELECTION_TYPE,
    CONF_IMMICH_URL,
    CONF_PROVIDER,
    CONF_REVERSE_GEOCODE,
    PROVIDER_IMMICH,
)


class FakeClient:
    fail = False

    def __init__(self, _hass, url, _key):
        self.base_url = url.rstrip("/")

    async def async_validate(self):
        if FakeClient.fail:
            raise ConnectionError("down")

    async def async_list_albums(self):
        return [{"id": "a1", "albumName": "Holiday"}, {"id": "a2", "albumName": "Kids"}]

    async def async_list_people(self):
        return [{"id": "p1", "name": "Anna"}, {"id": "p2", "name": ""}]


class FakeEntries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, **kwargs):
        self.updates.append(kwargs)


def _flow(monkeypatch, data, *, fail=False):
    FakeClient.fail = fail
    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    flow = cf.ImmichOptionsFlow()
    flow.hass = SimpleNamespace(config_entries=FakeEntries())
    flow.config_entry = SimpleNamespace(
        entry_id="e1", title=data.get(CONF_ALBUM_NAME, "Old"), data=data, options={}
    )
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow


def _entry_data(**overrides):
    data = {
        CONF_PROVIDER: PROVIDER_IMMICH,
        CONF_IMMICH_URL: "http://immich.test",
        CONF_IMMICH_API_KEY: "secret",
        CONF_IMMICH_SELECTION_TYPE: "composite",
        CONF_IMMICH_SELECTION_ID: json.dumps(
            {"albums": ["a1", "gone"], "people": ["p1"], "favorites": True},
            sort_keys=True,
        ),
        CONF_IMMICH_IMAGE_SIZE: "preview",
        CONF_ALBUM_NAME: "Old",
    }
    data.update(overrides)
    return data


def _defaults(result):
    out = {}
    for marker in result["data_schema"].schema:
        default = marker.default() if callable(marker.default) else None
        if default is not None:
            out[str(marker)] = default
        elif marker.description:
            out[str(marker)] = marker.description.get("suggested_value")
    return out


def test_immich_entries_get_the_picker_not_the_noop_flow():
    entry = SimpleNamespace(data={CONF_PROVIDER: PROVIDER_IMMICH})
    assert isinstance(cf.ConfigFlow.async_get_options_flow(entry), cf.ImmichOptionsFlow)


def test_picker_is_prefilled_with_current_selection(monkeypatch):
    flow = _flow(monkeypatch, _entry_data(**{CONF_IMMICH_FILTER: '{"city": "Paris"}'}))

    result = asyncio.run(flow.async_step_init())

    assert result["step_id"] == "immich_select"
    defaults = _defaults(result)
    assert defaults[CONF_ALBUM_NAME] == "Old"
    assert defaults["albums"] == ["a1", "gone"]
    assert defaults["people"] == ["p1"]
    assert defaults["favorites"] is True
    assert defaults[CONF_IMMICH_FILTER] == '{"city": "Paris"}'


@pytest.mark.parametrize("kind", ["albums", "people"])
@pytest.mark.parametrize("failure", [PermissionError("listing denied"), TimeoutError()])
def test_failed_listing_keeps_saved_selection(monkeypatch, kind, failure):
    selection = {"albums": [], "people": [], "favorites": False}
    selection[kind] = ["a1" if kind == "albums" else "p1"]
    flow = _flow(
        monkeypatch,
        _entry_data(**{CONF_IMMICH_SELECTION_ID: json.dumps(selection)}),
    )

    async def failed_listing(self):
        raise failure

    monkeypatch.setattr(FakeClient, f"async_list_{kind}", failed_listing)
    form = asyncio.run(flow.async_step_init())
    submitted = form["data_schema"](_defaults(form))
    asyncio.run(flow.async_step_immich_select(submitted))

    saved = flow.hass.config_entries.updates[0]["data"]
    assert json.loads(saved[CONF_IMMICH_SELECTION_ID]) == selection


def test_missing_choices_are_not_silently_removed(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    form = asyncio.run(flow.async_step_init())
    asyncio.run(flow.async_step_immich_select(_defaults(form)))

    saved = flow.hass.config_entries.updates[0]["data"]
    assert json.loads(saved[CONF_IMMICH_SELECTION_ID])["albums"] == ["a1", "gone"]
    assert flow._albums["gone"] == "gone (unavailable)"


def test_reverse_geocoding_option_remains_editable(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    flow.config_entry.options = {CONF_REVERSE_GEOCODE: False, "other": "kept"}
    form = asyncio.run(flow.async_step_init())
    values = _defaults(form)
    assert values[CONF_REVERSE_GEOCODE] is False
    values[CONF_REVERSE_GEOCODE] = True

    result = asyncio.run(flow.async_step_immich_select(values))

    assert result["data"] == {CONF_REVERSE_GEOCODE: True, "other": "kept"}
    assert flow.hass.config_entries.updates[0]["options"] == result["data"]


def test_cache_clear_failure_does_not_change_source(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    asyncio.run(flow.async_step_init())
    monkeypatch.setattr(cf, "_async_clear_item_cache", AsyncMock(side_effect=OSError()))

    result = asyncio.run(flow.async_step_immich_select({
        CONF_ALBUM_NAME: "Different", "albums": ["a2"],
    }))

    assert result["errors"] == {"base": "immich_cache_clear_failed"}
    assert flow.hass.config_entries.updates == []


def test_changing_api_key_invalidates_old_source_cache():
    original = _entry_data()
    changed = {**original, CONF_IMMICH_API_KEY: "different-user-key"}
    assert cf._immich_source_changed(original, changed)


def test_submit_updates_entry_data_and_keeps_credentials(monkeypatch):
    flow = _flow(monkeypatch, _entry_data(**{CONF_IMMICH_FILTER: '{"city": "Paris"}'}))
    asyncio.run(flow.async_step_init())

    result = asyncio.run(
        flow.async_step_immich_select(
            {CONF_ALBUM_NAME: " Kids ", "albums": ["a2"], "people": [], "favorites": False}
        )
    )

    assert result["type"] == "create_entry"
    (update,) = flow.hass.config_entries.updates
    assert update["title"] == "Kids"
    data = update["data"]
    assert data[CONF_IMMICH_URL] == "http://immich.test"
    assert data[CONF_IMMICH_API_KEY] == "secret"
    assert data[CONF_IMMICH_SELECTION_TYPE] == "composite"
    assert json.loads(data[CONF_IMMICH_SELECTION_ID]) == {
        "albums": ["a2"], "people": [], "favorites": False,
    }
    assert CONF_IMMICH_FILTER not in data  # cleared filter is removed


def test_invalid_filter_shows_error_and_does_not_save(monkeypatch):
    flow = _flow(monkeypatch, _entry_data())
    asyncio.run(flow.async_step_init())

    result = asyncio.run(
        flow.async_step_immich_select({CONF_ALBUM_NAME: "Old", CONF_IMMICH_FILTER: "[1]"})
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_IMMICH_FILTER: "immich_filter_invalid"}
    assert flow.hass.config_entries.updates == []


def test_legacy_person_entry_is_prefilled_and_saved_as_composite(monkeypatch):
    flow = _flow(
        monkeypatch,
        _entry_data(**{CONF_IMMICH_SELECTION_TYPE: "person", CONF_IMMICH_SELECTION_ID: "p1"}),
    )

    result = asyncio.run(flow.async_step_init())
    assert _defaults(result)["people"] == ["p1"]

    asyncio.run(flow.async_step_immich_select({CONF_ALBUM_NAME: "Anna", "people": ["p1"]}))
    data = flow.hass.config_entries.updates[0]["data"]
    assert data[CONF_IMMICH_SELECTION_TYPE] == "composite"
    assert json.loads(data[CONF_IMMICH_SELECTION_ID])["people"] == ["p1"]


def test_unreachable_immich_asks_for_connection_details(monkeypatch):
    flow = _flow(monkeypatch, _entry_data(), fail=True)

    result = asyncio.run(flow.async_step_init())
    assert result["step_id"] == "connection"
    assert result["errors"] == {"base": "immich_cannot_connect"}

    FakeClient.fail = False
    result = asyncio.run(
        flow.async_step_connection(
            {CONF_IMMICH_URL: "http://new.test/", CONF_IMMICH_API_KEY: " new-key "}
        )
    )
    assert result["step_id"] == "immich_select"

    asyncio.run(flow.async_step_immich_select({CONF_ALBUM_NAME: "Old"}))
    data = flow.hass.config_entries.updates[0]["data"]
    assert data[CONF_IMMICH_URL] == "http://new.test"
    assert data[CONF_IMMICH_API_KEY] == "new-key"
