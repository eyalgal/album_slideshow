from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import store as store_module
from custom_components.album_slideshow.number import CustomLookbackDaysNumber, ShuffleAgeBiasNumber
from custom_components.album_slideshow.image_processing import resolve_output_size
from custom_components.album_slideshow.select import (
    ASPECT_RATIO_OPTIONS,
    AspectRatioSelect,
    DateFilterSelect,
)
from custom_components.album_slideshow.const import DATE_FILTER_CUSTOM


@pytest.mark.parametrize(("control_type", "field", "value", "expected"), [
    (CustomLookbackDaysNumber, "custom_lookback_days", 1825, 1825),
    (CustomLookbackDaysNumber, "custom_lookback_days", 0, 1),
    (CustomLookbackDaysNumber, "custom_lookback_days", 100000, 36500),
    (ShuffleAgeBiasNumber, "shuffle_age_bias", -60, -60),
    (ShuffleAgeBiasNumber, "shuffle_age_bias", 500, 100),
    (ShuffleAgeBiasNumber, "shuffle_age_bias", -500, -100),
])
def test_playlist_controls_update_and_restore(control_type, field, value, expected):
    store = store_module.SlideshowStore()
    notifications = []
    store.add_listener(lambda: notifications.append(True))
    control = control_type(SimpleNamespace(entry_id="album"), store)
    asyncio.run(control.async_set_native_value(value))
    assert getattr(store, field) == expected
    assert control.native_value == expected
    assert notifications == [True]
    restored_store = store_module.SlideshowStore()
    restored = control_type(SimpleNamespace(entry_id="album"), restored_store)
    restored.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=str(expected)))
    asyncio.run(restored.async_added_to_hass())
    assert getattr(restored_store, field) == expected


@pytest.mark.parametrize("control_type", [CustomLookbackDaysNumber, ShuffleAgeBiasNumber])
@pytest.mark.parametrize("state", ["unknown", "unavailable", "invalid", "nan", "inf"])
def test_playlist_controls_ignore_invalid_restored_values(control_type, state):
    store = store_module.SlideshowStore()
    control = control_type(SimpleNamespace(entry_id="album"), store)
    default = control.native_value
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=state))
    asyncio.run(control.async_added_to_hass())
    assert control.native_value == default


def test_custom_date_filter_option_can_be_selected_and_restored():
    store = store_module.SlideshowStore()
    control = DateFilterSelect(SimpleNamespace(entry_id="album"), store)
    assert DATE_FILTER_CUSTOM in control.options
    asyncio.run(control.async_select_option(DATE_FILTER_CUSTOM))
    assert store.date_filter == DATE_FILTER_CUSTOM
    store.date_filter = "off"
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=DATE_FILTER_CUSTOM))
    asyncio.run(control.async_added_to_hass())
    assert store.date_filter == DATE_FILTER_CUSTOM


@pytest.mark.parametrize("ratio", ["5:3", "3:5", "3:2", "2:3"])
def test_aspect_ratio_select_accepts_and_restores_new_ratios(ratio):
    store = store_module.SlideshowStore()
    control = AspectRatioSelect(SimpleNamespace(entry_id="album"), store)
    asyncio.run(control.async_select_option(ratio))
    assert store.aspect_ratio == ratio
    assert control.current_option == ratio
    restored_store = store_module.SlideshowStore()
    restored = AspectRatioSelect(SimpleNamespace(entry_id="album"), restored_store)
    restored.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=ratio))
    asyncio.run(restored.async_added_to_hass())
    assert restored_store.aspect_ratio == ratio


# A malformed option would silently fall back to a 16:9 canvas.
@pytest.mark.parametrize("ratio", ASPECT_RATIO_OPTIONS)
def test_every_aspect_ratio_option_renders_its_ratio(ratio):
    w, h = (int(part) for part in ratio.split(":"))
    width, height = resolve_output_size(None, None, ratio, 1080)
    assert min(width, height) == 1080
    assert width / height == pytest.approx(w / h, rel=0.005)


@pytest.fixture
def storage(monkeypatch):
    records = {}

    class FakeStorage:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return deepcopy(records.get(self.key))

        async def async_save(self, data):
            records[self.key] = deepcopy(data)

    monkeypatch.setattr(store_module, "Store", FakeStorage)
    return records


def test_exclusions_persist_per_entry_and_undo_survives_restart(storage):
    async def run():
        original = store_module.SlideshowStore()
        await original.async_load_hidden_photos(None, "first")
        assert await original.async_set_photos_hidden(["photo-a", "photo-b"], hidden=True)
        restored = store_module.SlideshowStore()
        await restored.async_load_hidden_photos(None, "first")
        other = store_module.SlideshowStore()
        await other.async_load_hidden_photos(None, "second")
        assert restored.hidden_photo_ids == {"photo-a", "photo-b"}
        assert restored.last_hidden_photo_ids == ("photo-a", "photo-b")
        assert restored.hidden_revision == original.hidden_revision == 1
        assert not other.hidden_photo_ids
        assert await restored.async_set_photos_hidden(restored.last_hidden_photo_ids, hidden=False)
        assert not restored.hidden_photo_ids
        assert storage["album_slideshow.first.hidden"] == {"hidden": [], "last_hidden": [], "revision": 2}

    asyncio.run(run())


def test_repeated_hide_is_a_noop_and_preserves_last_action(storage):
    async def run():
        store = store_module.SlideshowStore()
        await store.async_load_hidden_photos(None, "test")
        notifications = []
        store.add_listener(lambda: notifications.append(store.hidden_revision))
        store.last_frame = b"old-photo"
        assert await store.async_set_photos_hidden(["photo-a"], hidden=True)
        assert store.last_frame is None
        assert not await store.async_set_photos_hidden(["photo-a"], hidden=True)
        assert store.last_hidden_photo_ids == ("photo-a",)
        assert notifications == [1]
        assert await store.async_set_photos_hidden(["photo-b"], hidden=True)
        assert await store.async_set_photos_hidden(store.last_hidden_photo_ids, hidden=False)
        assert store.hidden_photo_ids == {"photo-a"}

    asyncio.run(run())


def test_failed_save_leaves_exclusions_and_frame_unchanged(storage, monkeypatch):
    async def run():
        store = store_module.SlideshowStore()
        await store.async_load_hidden_photos(None, "test")
        store.last_frame = b"current-photo"
        notifications = []
        store.add_listener(lambda: notifications.append(True))

        async def fail_save(data):
            raise OSError("Disk unavailable")

        monkeypatch.setattr(store._hidden_storage, "async_save", fail_save)
        with pytest.raises(OSError, match="Disk unavailable"):
            await store.async_set_photos_hidden(["photo-a"], hidden=True)
        assert not store.hidden_photo_ids
        assert not store.last_hidden_photo_ids
        assert store.hidden_revision == 0
        assert store.last_frame == b"current-photo"
        assert not notifications

    asyncio.run(run())


@pytest.mark.parametrize("data", [[], {"hidden": "bad"}, {"hidden": [None]}, {"last_hidden": [""]}, {"revision": -1}])
def test_invalid_storage_is_not_silently_treated_as_empty(storage, data):
    storage["album_slideshow.test.hidden"] = data
    store = store_module.SlideshowStore()
    with pytest.raises(ValueError, match="Invalid hidden-photo"):
        asyncio.run(store.async_load_hidden_photos(None, "test"))


def test_restore_selected_does_not_reset_other_exclusions(storage):
    async def run():
        store = store_module.SlideshowStore()
        await store.async_load_hidden_photos(None, "test")
        await store.async_set_photos_hidden(["photo-a", "photo-b"], hidden=True)
        await store.async_set_photos_hidden(["photo-a"], hidden=False)
        assert store.hidden_photo_ids == {"photo-b"}
        assert store.last_hidden_photo_ids == ("photo-b",)
        await store.async_set_photos_hidden(store.hidden_photo_ids, hidden=False)
        assert not store.hidden_photo_ids

    asyncio.run(run())