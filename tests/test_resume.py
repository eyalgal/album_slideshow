"""Long slide intervals, and picking up the same slide after a restart."""
from __future__ import annotations

import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from custom_components import album_slideshow as integration
from custom_components.album_slideshow import camera, coordinator
from custom_components.album_slideshow import store as store_module
from custom_components.album_slideshow.const import ORDER_ALBUM, ORDER_RANDOM
from custom_components.album_slideshow.number import SlideIntervalNumber
from custom_components.album_slideshow.store import SlideshowStore
from tests.test_camera_next_slide import _FakeHass

NOW = 1_800_000_000.0
POSITION = "album_slideshow.test.position"


class _Storage:
    def __init__(self, data=None):
        self.data = data
        self.pending = None
        self.saved = []
        self.removed = False

    async def async_load(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def async_delay_save(self, data_func, delay):
        self.pending = (data_func, delay)

    async def async_save(self, data):
        self.pending = None
        self.saved.append(data)

    async def async_remove(self):
        self.removed = True


@pytest.fixture
def files(monkeypatch):
    stored: dict[str, _Storage] = {}
    monkeypatch.setattr(store_module, "Store", lambda hass, version, key: stored.setdefault(key, _Storage()))
    return stored


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(camera, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(camera.ip, "resolve_output_size", lambda *args: (80, 40))
    return clock


def _photo(n: int, *, portrait: bool = False) -> coordinator.MediaItem:
    width, height = (40, 80) if portrait else (80, 40)
    return coordinator.MediaItem(
        url=f"https://example.test/{n}.jpg", width=width, height=height,
        mime_type="image/jpeg", filename=f"{n}.jpg", source_id=f"photo-{n}",
    )


async def _store(files, saved=None, **settings) -> SlideshowStore:
    files[POSITION] = _Storage(saved)
    store = SlideshowStore(**settings)
    await store.async_load_position(object(), "test")
    return store


def _camera(items, store) -> camera.AlbumSlideshowCamera:
    coord = SimpleNamespace(data={"items": items}, async_add_listener=lambda listener: None)
    cam = camera.AlbumSlideshowCamera(_FakeHass(), SimpleNamespace(entry_id="test", title="Test"), coord, store)
    cam.async_write_ha_state = lambda: None
    cam._schedule_preload = lambda: None
    for item in items:
        with Image.new("RGB", (item.width, item.height), (90, 70, 120)) as image:
            output = BytesIO()
            image.save(output, format="JPEG")
            cam._download_cache.put(item.url, output.getvalue())
    return cam


def _pending(files) -> dict:
    data_func, delay = files[POSITION].pending
    assert delay == store_module._POSITION_SAVE_DELAY
    return data_func()


def test_slide_interval_goes_up_to_a_day():
    assert SlideIntervalNumber._attr_native_max_value == 86400


@pytest.mark.parametrize("saved", [
    None,
    [],
    {"photo_ids": [], "shown_at": NOW},
    {"photo_ids": ["a", "b", "c"], "shown_at": NOW},
    {"photo_ids": [""], "shown_at": NOW},
    {"photo_ids": [7], "shown_at": NOW},
    {"photo_ids": "a", "shown_at": NOW},
    {"photo_ids": ["a"]},
    {"photo_ids": ["a"], "shown_at": True},
    {"photo_ids": ["a"], "shown_at": "1800000000"},
    {"photo_ids": ["a"], "shown_at": float("nan")},
    {"photo_ids": ["a"], "shown_at": float("inf")},
    OSError("unreadable"),
])
def test_a_missing_or_damaged_saved_slide_is_ignored(files, saved):
    store = asyncio.run(_store(files, saved))
    assert store.resume_point is None


def test_a_saved_slide_is_loaded(files):
    store = asyncio.run(_store(files, {"photo_ids": ["a", "b"], "shown_at": 1800000000}))
    assert store.resume_point == {"photo_ids": ["a", "b"], "shown_at": NOW}


def test_saving_waits_and_a_reload_writes_it_right_away(files):
    async def run():
        store = await _store(files)
        store.save_position(["a", "b"], NOW)
        assert _pending(files) == {"photo_ids": ["a", "b"], "shown_at": NOW}
        assert files[POSITION].saved == []
        await store.async_flush_position()
        assert files[POSITION].saved == [{"photo_ids": ["a", "b"], "shown_at": NOW}]
        await store.async_flush_position()
        assert len(files[POSITION].saved) == 1

    asyncio.run(run())


def test_a_slide_without_photo_ids_is_not_resumed(files):
    async def run():
        store = await _store(files)
        store.save_position(["a", None], NOW)
        assert _pending(files) == {"photo_ids": ["a"], "shown_at": NOW}
        store.save_position([None, "b"], NOW)
        assert _pending(files) == {}

    asyncio.run(run())


def test_removing_an_album_deletes_its_saved_files(files):
    asyncio.run(integration.async_remove_entry(object(), SimpleNamespace(entry_id="test")))
    assert files["album_slideshow.test.hidden"].removed
    assert files[POSITION].removed


def test_a_restart_resumes_the_slide_with_the_time_it_had_left(files):
    items = [_photo(n) for n in range(6)]

    async def run():
        saved = {"photo_ids": [items[3].photo_id], "shown_at": NOW - 600}
        cam = _camera(items, await _store(files, saved, order_mode=ORDER_RANDOM, slide_interval=3600))
        await cam._rebuild_current_frame()
        assert cam._current_frame.meta["photo_ids"] == [items[3].photo_id]
        assert cam._remaining_hold() == 3000
        assert _pending(files) == saved

    asyncio.run(run())


@pytest.mark.parametrize(("shown_at", "left"), [(NOW - 7200, 0), (NOW + 600, 3600)])
def test_a_resumed_slide_never_waits_less_than_nothing_or_more_than_the_interval(files, shown_at, left):
    items = [_photo(n) for n in range(3)]

    async def run():
        saved = {"photo_ids": [items[1].photo_id], "shown_at": shown_at}
        cam = _camera(items, await _store(files, saved, slide_interval=3600))
        await cam._rebuild_current_frame()
        assert cam._remaining_hold() == left

    asyncio.run(run())


def test_a_restart_keeps_both_photos_of_a_pair(files):
    items = [_photo(n, portrait=True) for n in range(6)]

    async def run(saved_ids):
        saved = {"photo_ids": saved_ids, "shown_at": NOW}
        cam = _camera(items, await _store(files, saved, order_mode=ORDER_ALBUM, portrait_mode="pair"))
        await cam._rebuild_current_frame()
        return cam._current_frame.meta["photo_ids"]

    ids = [item.photo_id for item in items]
    # Without a saved partner, the nearest one would be picked.
    assert asyncio.run(run([ids[2]])) == [ids[2], ids[3]]
    assert asyncio.run(run([ids[2], ids[4]])) == [ids[2], ids[4]]


def test_without_a_saved_slide_random_order_starts_on_a_random_photo(files):
    items = [_photo(n) for n in range(6)]

    async def run():
        cam = _camera(items, await _store(files, order_mode=ORDER_RANDOM))
        await cam._rebuild_current_frame()
        return cam._current_frame.meta["photo_ids"][0]

    first = {asyncio.run(run()) for _ in range(10)}
    assert items[0].photo_id not in first


@pytest.mark.parametrize("saved_id", [None, "photo-gone"])
def test_ordered_modes_start_at_the_beginning_without_a_saved_slide(files, saved_id):
    items = [_photo(n) for n in range(4)]

    async def run():
        saved = {"photo_ids": [saved_id], "shown_at": NOW - 60} if saved_id else None
        cam = _camera(items, await _store(files, saved, order_mode=ORDER_ALBUM))
        await cam._rebuild_current_frame()
        assert cam._current_frame.meta["photo_ids"] == [items[0].photo_id]
        assert cam._resume_anchor is None

    asyncio.run(run())


def test_each_new_slide_is_saved_but_a_rerender_keeps_its_time(files, _clock):
    items = [_photo(n) for n in range(4)]

    async def run():
        cam = _camera(items, await _store(files, order_mode=ORDER_ALBUM))
        await cam._rebuild_current_frame()
        assert _pending(files) == {"photo_ids": [items[0].photo_id], "shown_at": NOW}
        _clock.now = NOW + 30
        await cam._rebuild_current_frame()
        assert _pending(files) == {"photo_ids": [items[0].photo_id], "shown_at": NOW}
        await cam._show_next_frame()
        assert _pending(files) == {"photo_ids": [items[1].photo_id], "shown_at": NOW + 30}
        await cam.async_will_remove_from_hass()
        assert files[POSITION].saved == [{"photo_ids": [items[1].photo_id], "shown_at": NOW + 30}]

    asyncio.run(run())


def test_pausing_gives_a_resumed_slide_a_full_interval(files):
    async def run():
        cam = _camera([_photo(0)], await _store(files, paused=True))
        cam._resume_anchor = NOW - 600
        waiting = asyncio.create_task(cam._wait_or_interrupt(5))
        await asyncio.sleep(0)
        assert cam._resume_anchor is None
        cam._interrupt_event.set()
        assert await waiting is True

    asyncio.run(run())


def test_a_returning_viewer_gives_a_resumed_slide_a_full_interval(files):
    async def run():
        cam = _camera([_photo(0)], await _store(files, pause_when_unwatched=True))
        cam._resume_anchor = NOW - 600
        cam._on_viewers_change()
        assert cam._resume_anchor is None

    asyncio.run(run())


def test_the_first_slide_waits_for_restored_settings(files):
    async def run():
        store = await _store(files)
        store.settings_restored = asyncio.Event()
        cam = _camera([_photo(0)], store)
        seen = []

        async def rebuild():
            seen.append(cam.store.order_mode)
            raise asyncio.CancelledError

        cam._rebuild_current_frame = rebuild
        loop = asyncio.create_task(cam._render_loop())
        await asyncio.sleep(0.01)
        assert seen == []
        store.order_mode = ORDER_ALBUM
        store.settings_restored.set()
        with pytest.raises(asyncio.CancelledError):
            await loop
        assert seen == [ORDER_ALBUM]

    asyncio.run(run())
