from __future__ import annotations

import asyncio
from collections import Counter, deque
import dataclasses
import random
import types

import pytest
from PIL import Image

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.const import (
    DEFAULT_NAVIGATION_BUFFER_SIZE,
    DOMAIN,
    ORDER_ALBUM,
    ORDER_RANDOM,
)
from custom_components.album_slideshow.store import SlideshowStore


class _FakeHass:
    def __init__(self):
        self.data = {DOMAIN: {}}

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_background_task(self, coro, name):
        return asyncio.create_task(coro, name=name)


def _cursor(index: int, seed: int = 42) -> camera._NavigationCursor:
    rng = random.Random(seed)
    return camera._NavigationCursor(
        index=index,
        random_order=(),
        random_pos=0,
        recent_urls=(),
        rng_state=rng.getstate(),
    )


def _frame(index: int, *, marker: str | None = None) -> camera._RenderedFrame:
    return camera._RenderedFrame(
        data=(marker or f"frame-{index}").encode(),
        cursor=_cursor(index),
        meta={
            "is_portrait": bool(index % 2),
            "captured_at_pair": None,
            "pair_frames": None,
            "pair_orientation": None,
        },
    )


def _make_cam(depth: int = 2, paused: bool = False):
    """Build a camera-shaped object for timeline tests without Home Assistant."""
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.entry = types.SimpleNamespace(title="Test")
    cam.store = types.SimpleNamespace(
        navigation_buffer_size=depth,
        paused=paused,
        slide_interval=60,
        last_frame=None,
    )
    cam._rng = random.Random(42)
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cam._framebuffer = None
    cam._frame_id = 0
    cam._frame_serials = {}
    cam._last_is_portrait = None
    cam._last_pair_frames = None
    cam._last_pair_orientation = None
    cam._current_frame = None
    cam._previous_frames = deque()
    cam._next_frames = deque()
    cam._navigation_lock = asyncio.Lock()
    cam._navigation_pending = 0
    cam._interrupt_event = asyncio.Event()
    cam._slide_deadline = None
    cam._next_ready_event = asyncio.Event()
    cam._timeline_generation = 0
    cam._timeline_dirty = False
    cam._preload_task = None
    cam._last_nav_direction = None
    cam._last_nav_requested_at = None
    cam._last_nav_started_at = None
    cam._last_nav_committed_at = None
    cam._state_writes = 0
    cam.async_write_ha_state = lambda: setattr(
        cam, "_state_writes", cam._state_writes + 1
    )
    # Most timeline operation tests install their frames explicitly and should
    # not start a real background worker.
    cam._schedule_preload = lambda: None
    return cam


# ── configuration and cursor isolation ─────────────────────────────────────


def test_navigation_buffer_defaults_to_two_slides():
    assert DEFAULT_NAVIGATION_BUFFER_SIZE == 2
    assert SlideshowStore().navigation_buffer_size == 2


def _dated_items(count=20):
    return [types.SimpleNamespace(url=f"photo-{index}", captured_at=index * 1000000) for index in range(count)]


@pytest.mark.parametrize("bias", [100, -100])
def test_age_bias_changes_frequency_and_avoids_immediate_repeats(bias):
    cam = _make_cam()
    cam.store = SlideshowStore(order_mode=ORDER_RANDOM, shuffle_age_bias=bias)
    items = _dated_items()
    counts = Counter()
    previous = cam._index
    for _ in range(6000):
        cam._do_advance(len(items), items)
        assert cam._index != previous
        counts[cam._index] += 1
        previous = cam._index

    oldest = sum(counts[index] for index in range(5))
    newest = sum(counts[index] for index in range(15, 20))
    assert newest > oldest * 2 if bias > 0 else oldest > newest * 2
    assert len(counts) == len(items)


def test_neutral_age_bias_keeps_original_random_cycle_exactly():
    control = _make_cam()
    control.store.order_mode = ORDER_RANDOM
    changed = _make_cam()
    changed.store = SlideshowStore(order_mode=ORDER_RANDOM, shuffle_age_bias=0)
    items = _dated_items()
    for _ in range(100):
        control._do_advance(len(items), items)
        changed._do_advance(len(items), items)
        assert control._capture_cursor() == changed._capture_cursor()


def test_age_bias_is_ignored_for_nonrandom_order():
    cam = _make_cam()
    cam.store = SlideshowStore(order_mode=ORDER_ALBUM, shuffle_age_bias=100)
    items = _dated_items()
    for expected in range(1, len(items)):
        cam._do_advance(len(items), items)
        assert cam._index == expected


def test_age_bias_cursors_replay_without_mutating_live_state():
    cam = _make_cam()
    cam.store = SlideshowStore(order_mode=ORDER_RANDOM, shuffle_age_bias=80)
    cam.hass = object()
    cam.coordinator = object()
    cam._download_cache = camera._DownloadCache(1024)
    initial = cam._capture_cursor()
    first = cam._make_renderer(initial)
    replay = cam._make_renderer(initial)
    items = _dated_items()
    for _ in range(30):
        first._do_advance(len(items), items)
        replay._do_advance(len(items), items)
        assert first._capture_cursor() == replay._capture_cursor()
    assert cam._capture_cursor() == initial


@pytest.mark.parametrize("count", [1, 2, 3])
def test_age_bias_works_with_tiny_playlists(count):
    cam = _make_cam()
    cam.store = SlideshowStore(order_mode=ORDER_RANDOM, shuffle_age_bias=100)
    items = _dated_items(count)
    for _ in range(30):
        previous = cam._index
        cam._do_advance(count, items)
        assert 0 <= cam._index < count
        assert count == 1 or previous != cam._index


def test_buffer_depth_clamps_to_supported_range():
    assert _make_cam(depth=-5)._buffer_depth == 0
    assert _make_cam(depth=2)._buffer_depth == 2
    assert _make_cam(depth=99)._buffer_depth == 10


def test_private_renderer_does_not_mutate_live_ordering_state():
    cam = _make_cam()
    cam.hass = object()
    cam.coordinator = object()
    cam._download_cache = camera._DownloadCache(1024)
    cam._index = 4
    cam._random_order = [4, 2, 1]
    cam._random_pos = 1
    cam._recent_urls = ["a"]

    renderer = cam._make_renderer(cam._capture_cursor())
    renderer._index = 9
    renderer._random_order.append(8)
    renderer._recent_urls.append("b")

    assert cam._index == 4
    assert cam._random_order == [4, 2, 1]
    assert cam._recent_urls == ["a"]


def test_image_jobs_still_share_one_cpu_slot_across_albums():
    async def run():
        first_started = asyncio.Event()
        second_requested = asyncio.Event()
        release_job = asyncio.Event()
        submitted = []

        class Hass(_FakeHass):
            async def async_add_executor_job(self, function, *args):
                submitted.append(args[0])
                first_started.set()
                await release_job.wait()
                return function(*args)

        first = _make_cam()
        second = _make_cam()
        first.hass = second.hass = Hass()
        first_task = asyncio.create_task(first._async_image_job(str, "first"))
        await first_started.wait()

        async def second_job():
            second_requested.set()
            return await second._async_image_job(str, "second")

        second_task = asyncio.create_task(second_job())
        try:
            await second_requested.wait()
            assert submitted == ["first"]
        finally:
            release_job.set()
            results = await asyncio.gather(first_task, second_task)
        assert submitted == ["first", "second"]
        assert results == ["first", "second"]

    asyncio.run(run())


def test_render_available_frame_composes_on_private_renderer(monkeypatch):
    cam = _make_cam()
    cam.hass = _FakeHass()
    cam.coordinator = object()
    cam._download_cache = camera._DownloadCache(1024)
    cam.store.order_mode = ORDER_ALBUM
    items = [types.SimpleNamespace(url="a"), types.SimpleNamespace(url="b")]

    async def fake_compose(renderer, _items):
        return Image.new("RGB", (4, 4), "red"), {
            "is_portrait": False,
            "captured_at_pair": None,
        }

    monkeypatch.setattr(
        camera.AlbumSlideshowCamera,
        "_compose_for_index",
        fake_compose,
    )

    frame = asyncio.run(
        cam._render_available_frame(cam._capture_cursor(), items, advance=True)
    )

    assert frame.cursor.index == 1
    assert frame.data.startswith(b"\xff\xd8")
    # Rendering ahead must never move the live entity.
    assert cam._index == 0


def test_random_preloads_chain_from_each_rendered_cursor(monkeypatch):
    cam = _make_cam()
    cam.hass = _FakeHass()
    cam.coordinator = object()
    cam._download_cache = camera._DownloadCache(1024)
    cam.store.order_mode = ORDER_RANDOM
    items = [types.SimpleNamespace(url=f"url-{i}") for i in range(6)]

    async def fake_compose(_renderer, _items):
        return Image.new("RGB", (2, 2), "blue"), {"is_portrait": False}

    monkeypatch.setattr(
        camera.AlbumSlideshowCamera,
        "_compose_for_index",
        fake_compose,
    )

    async def run():
        first = await cam._render_available_frame(
            cam._capture_cursor(), items, advance=True
        )
        second = await cam._render_available_frame(
            first.cursor, items, advance=True
        )
        return first, second

    first, second = asyncio.run(run())
    assert first.cursor.index != 0
    assert second.cursor.index != first.cursor.index
    assert first.cursor.random_pos == 1
    assert second.cursor.random_pos == 2
    assert cam._index == 0


# ── O(1) rendered-frame navigation ─────────────────────────────────────────


def test_apply_frame_restores_bytes_cursor_and_metadata():
    cam = _make_cam()
    frame = _frame(3, marker="encoded-jpeg")

    cam._apply_frame(frame)

    assert cam._current_frame is frame
    assert cam._framebuffer == b"encoded-jpeg"
    assert cam.store.last_frame == b"encoded-jpeg"
    assert cam._index == 3
    assert cam._last_is_portrait is True
    assert cam._frame_id == 1


def test_next_uses_pre_rendered_frame_without_rendering():
    cam = _make_cam(depth=2)
    first = _frame(0)
    second = _frame(1)
    cam._current_frame = first
    cam._next_frames.append(second)

    async def forbidden_render(*_args, **_kwargs):
        raise AssertionError("buffered Next attempted to render")

    cam._render_available_frame = forbidden_render

    assert asyncio.run(cam._show_next_frame()) is True
    assert cam._current_frame is second
    assert list(cam._previous_frames) == [first]
    assert cam._framebuffer == second.data


def test_previous_restores_exact_encoded_frame_without_rendering():
    cam = _make_cam(depth=2)
    first = _frame(0, marker="exact-old-jpeg")
    second = _frame(1)
    cam._current_frame = second
    cam._previous_frames.append(first)

    async def forbidden_render(*_args, **_kwargs):
        raise AssertionError("Previous attempted to render")

    cam._render_available_frame = forbidden_render

    assert asyncio.run(cam._show_previous_frame()) is True
    assert cam._current_frame is first
    assert cam._framebuffer == b"exact-old-jpeg"
    assert list(cam._next_frames) == [second]


def test_back_then_forward_reuses_same_frame_objects():
    cam = _make_cam(depth=2)
    a, b, c = _frame(0), _frame(1), _frame(2)
    cam._current_frame = a
    cam._next_frames.extend([b, c])

    async def run():
        await cam._show_next_frame()
        assert cam._current_frame is b
        await cam._show_previous_frame()
        assert cam._current_frame is a
        await cam._show_next_frame()
        assert cam._current_frame is b

    asyncio.run(run())
    assert list(cam._next_frames) == [c]


def test_previous_and_next_buffers_are_bounded():
    cam = _make_cam(depth=2)
    frames = [_frame(i) for i in range(5)]
    cam._current_frame = frames[0]
    cam._next_frames.extend(frames[1:])
    cam._trim_timeline()
    assert list(cam._next_frames) == frames[1:3]

    async def run():
        await cam._show_next_frame()
        await cam._show_next_frame()

    asyncio.run(run())
    assert list(cam._previous_frames) == frames[:2]


def test_previous_at_oldest_is_a_noop():
    cam = _make_cam()
    current = _frame(4)
    cam._current_frame = current

    assert asyncio.run(cam._show_previous_frame()) is False
    assert cam._current_frame is current
    assert cam._frame_id == 0


def test_zero_depth_renders_next_on_demand_without_retaining_previous():
    cam = _make_cam(depth=0)
    first = _frame(0)
    second = _frame(1)
    cam._current_frame = first

    async def render(_cursor, _items, *, advance):
        assert advance is True
        return second

    cam._render_available_frame = render
    cam._effective_items = lambda: [object(), object()]

    assert asyncio.run(cam._show_next_frame()) is True
    assert cam._current_frame is second
    assert not cam._previous_frames
    assert not cam._next_frames


# ── background look-ahead ──────────────────────────────────────────────────


def test_preload_fills_configured_number_of_rendered_frames():
    cam = _make_cam(depth=2)
    cam._current_frame = _frame(0)
    cam._effective_items = lambda: [object()] * 5

    async def render(cursor, items, *, advance):
        assert advance is True
        return _frame((cursor.index + 1) % len(items))

    cam._render_available_frame = render

    asyncio.run(cam._preload_loop(cam._timeline_generation))

    assert [frame.cursor.index for frame in cam._next_frames] == [1, 2]


def test_preload_stops_when_generation_changes():
    cam = _make_cam(depth=2)
    cam._current_frame = _frame(0)
    cam._effective_items = lambda: [object()] * 5

    async def render(cursor, _items, *, advance):
        assert advance is True
        cam._timeline_generation += 1
        return _frame(cursor.index + 1)

    cam._render_available_frame = render

    asyncio.run(cam._preload_loop(0))

    assert not cam._next_frames


def test_invalidation_clears_both_sides_and_cancels_preload():
    cam = _make_cam(depth=2)
    cam._current_frame = _frame(1)
    cam._previous_frames.append(_frame(0))
    cam._next_frames.append(_frame(2))

    async def run():
        cam._preload_task = asyncio.create_task(asyncio.sleep(30))
        task = cam._preload_task
        cam._invalidate_timeline()
        await asyncio.sleep(0)
        assert task.cancelled()

    asyncio.run(run())
    assert cam._timeline_dirty is True
    assert cam._timeline_generation == 1
    assert not cam._previous_frames
    assert not cam._next_frames


def test_real_scheduler_can_restart_after_invalidating_inflight_preload():
    cam = _make_cam(depth=2)
    # Restore the class implementation hidden by the lightweight fixture.
    del cam._schedule_preload
    cam.hass = _FakeHass()
    cam._current_frame = _frame(0)
    cam._effective_items = lambda: [object()] * 5
    first_started = asyncio.Event()

    async def blocked_render(cursor, _items, *, advance):
        assert advance is True
        first_started.set()
        await asyncio.Event().wait()
        return _frame(cursor.index + 1)

    async def fast_render(cursor, _items, *, advance):
        assert advance is True
        return _frame(cursor.index + 1)

    async def run():
        cam._render_available_frame = blocked_render
        cam._schedule_preload()
        first_task = cam._preload_task
        assert first_task is not None
        await first_started.wait()

        cam._invalidate_timeline()
        await asyncio.sleep(0)
        assert first_task.cancelled()
        assert cam._preload_task is None

        cam._timeline_dirty = False
        cam._render_available_frame = fast_render
        cam._schedule_preload()
        second_task = cam._preload_task
        assert second_task is not None and second_task is not first_task
        await second_task

    asyncio.run(run())
    assert [frame.cursor.index for frame in cam._next_frames] == [1, 2]


# ── wake-up and request behavior ───────────────────────────────────────────


def test_wait_returns_immediately_when_timeline_is_dirty():
    cam = _make_cam()
    cam._timeline_dirty = True
    assert asyncio.run(cam._wait_or_interrupt(timeout=30)) is True


def test_wait_times_out_when_idle():
    cam = _make_cam()
    assert asyncio.run(cam._wait_or_interrupt(timeout=0.01)) is False


@pytest.mark.parametrize("interrupt_at_deadline", [False, True])
def test_metadata_updates_do_not_restart_slide_timer(monkeypatch, interrupt_at_deadline):
    cam = _make_cam(depth=0)
    cam._effective_items = lambda: [object(), object()]
    elapsed = 0.0
    waits = []
    advances = []

    async def render(cursor, _items, *, advance):
        if advance:
            advances.append(elapsed)
        return _frame((cursor.index + int(advance)) % 2)

    async def wait(timeout):
        nonlocal elapsed
        if advances or len(waits) >= 9:
            raise asyncio.CancelledError
        waits.append(timeout)
        elapsed += min(10.0, timeout)
        if timeout <= 0.0 or (timeout <= 10.0 and not interrupt_at_deadline):
            return False
        cam._invalidate_timeline()
        return True

    cam._render_available_frame = render
    cam._wait_or_interrupt = wait

    async def run():
        with monkeypatch.context() as clock:
            clock.setattr(asyncio.get_running_loop(), "time", lambda: elapsed)
            with pytest.raises(asyncio.CancelledError):
                await cam._render_loop()

    asyncio.run(run())

    assert advances == [60.0]
    assert waits == [60.0, 50.0, 40.0, 30.0, 20.0, 10.0] + ([0.0] if interrupt_at_deadline else [])
    assert cam._current_frame.cursor.index == 1


def test_coordinator_keeps_deadline_but_user_settings_reset_it():
    listeners = []
    store = SlideshowStore(navigation_buffer_size=0)
    coordinator = types.SimpleNamespace(async_add_listener=listeners.append)
    cam = camera.AlbumSlideshowCamera(
        _FakeHass(), types.SimpleNamespace(entry_id="test", title="Test"), coordinator, store,
    )
    cam.async_write_ha_state = lambda: None
    cam._slide_deadline = 60.0

    listeners[0]()

    assert cam._timeline_dirty is True
    assert cam._slide_deadline == 60.0
    for field, value in [("slide_interval", 20), ("paused", True), ("paused", False)]:
        cam._slide_deadline = 60.0
        setattr(store, field, value)
        store.notify()
        assert cam._slide_deadline is None


@pytest.mark.parametrize("direction", [-1, 1])
@pytest.mark.parametrize("manual_at", [10.0, 60.0])
def test_manual_navigation_gets_full_interval_without_double_advance(monkeypatch, direction, manual_at):
    cam = _make_cam()
    cam._effective_items = lambda: [object(), object(), object()]
    cam._apply_frame(_frame(1))
    cam._previous_frames.append(_frame(0))
    elapsed = 0.0
    manual_finished = None
    waits = []
    displayed = []
    apply_frame = cam._apply_frame

    def display(frame):
        displayed.append((elapsed, frame.cursor.index))
        apply_frame(frame)

    cam._apply_frame = display

    async def render(cursor, _items, *, advance):
        nonlocal elapsed
        if manual_finished is None:
            elapsed += 15.0
        return _frame((cursor.index + int(advance)) % 3)

    async def wait(timeout):
        nonlocal elapsed, manual_finished
        if len(waits) >= 2:
            raise asyncio.CancelledError
        waits.append(timeout)
        if len(waits) == 1:
            elapsed = manual_at
            await cam._async_navigate(direction)
            manual_finished = elapsed
            return manual_at < timeout
        elapsed += timeout
        return False

    cam._render_available_frame = render
    cam._wait_or_interrupt = wait

    async def run():
        with monkeypatch.context() as clock:
            clock.setattr(asyncio.get_running_loop(), "time", lambda: elapsed)
            with pytest.raises(asyncio.CancelledError):
                await cam._render_loop()

    asyncio.run(run())

    assert waits == [60.0, 60.0]
    assert displayed == [
        (manual_finished, 2 if direction > 0 else 0),
        (manual_finished + 60.0, 0 if direction > 0 else 1),
    ]


def test_paused_wait_wakes_for_manual_navigation():
    cam = _make_cam(paused=True)

    async def run():
        waiter = asyncio.create_task(cam._wait_or_interrupt(timeout=30))
        await asyncio.sleep(0)
        cam._interrupt_event.set()
        return await waiter

    assert asyncio.run(run()) is True


def test_force_navigation_executes_rapid_presses_in_order():
    cam = _make_cam(depth=2)
    a, b, c = _frame(0), _frame(1), _frame(2)
    cam._current_frame = a
    cam._next_frames.extend([b, c])

    async def run():
        await asyncio.gather(
            cam.async_force_next(),
            cam.async_force_next(),
            cam.async_force_prev(),
        )

    asyncio.run(run())
    assert cam._current_frame is b
    assert cam._navigation_pending == 0
    assert cam._last_nav_direction == "previous"
    assert cam._last_nav_requested_at is not None
    assert cam._last_nav_committed_at is not None
    assert cam._last_nav_outcome == "displayed"
    assert cam._last_nav_error is None


def test_force_previous_reports_when_no_frame_is_available():
    cam = _make_cam()
    cam._current_frame = _frame(0)

    asyncio.run(cam.async_force_prev())

    assert cam._current_frame.cursor.index == 0
    assert cam._last_nav_outcome == "not_available"
    assert cam._navigation_pending == 0


def test_direct_navigation_operates_while_render_loop_is_waiting():
    cam = _make_cam(depth=2)
    a, b = _frame(0), _frame(1)
    ready = asyncio.Event()
    moved_next = asyncio.Event()
    moved_previous = asyncio.Event()

    async def rebuild():
        cam._timeline_dirty = False
        cam._apply_frame(a)
        cam._next_frames.append(b)
        ready.set()
        return True

    cam._rebuild_current_frame = rebuild

    def write_state():
        cam._state_writes += 1
        if cam._current_frame is b:
            moved_next.set()
        elif (
            cam._current_frame is a
            and cam._last_nav_direction == "previous"
            and cam._last_nav_started_at is not None
        ):
            moved_previous.set()

    cam.async_write_ha_state = write_state

    async def run():
        loop_task = asyncio.create_task(cam._render_loop())
        try:
            await asyncio.wait_for(ready.wait(), timeout=1)
            await cam.async_force_next()
            await asyncio.wait_for(moved_next.wait(), timeout=1)
            assert cam._current_frame is b

            await cam.async_force_prev()
            await asyncio.wait_for(moved_previous.wait(), timeout=1)
            assert cam._current_frame is a
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

    asyncio.run(run())


# ── history survives updates and follows the displayed slide ───────────────


def _media(name: str) -> MediaItem:
    return MediaItem(
        url=f"https://example.test/{name}.jpg", width=40, height=30,
        mime_type="image/jpeg", filename=f"{name}.jpg", source_id=name,
    )


def _shown(item: MediaItem, index: int, **meta) -> camera._RenderedFrame:
    return camera._RenderedFrame(
        data=item.photo_id.encode(),
        cursor=_cursor(index),
        meta={"photo_ids": [item.photo_id], **meta},
    )


def _history_cam(names="abcd"):
    """A listening camera showing c, with a and b behind it and d preloaded."""
    listeners = []
    items = [_media(name) for name in names]
    store = SlideshowStore(navigation_buffer_size=3, order_mode=ORDER_ALBUM)
    coordinator = types.SimpleNamespace(
        data={"items": items}, async_add_listener=listeners.append,
    )
    cam = camera.AlbumSlideshowCamera(
        _FakeHass(), types.SimpleNamespace(entry_id="test", title="Test"), coordinator, store,
    )
    cam.async_write_ha_state = lambda: None
    cam._schedule_preload = lambda: None
    frames = [_shown(item, index, faces=(None,)) for index, item in enumerate(items)]
    cam._previous_frames.extend(frames[:2])
    cam._current_frame = frames[2]
    cam._index = 2
    cam._next_frames.extend(frames[3:])
    cam._effective_items()
    return cam, listeners[0], store, items, frames


def test_every_store_setting_is_classified_for_history():
    keeps_history = {
        "slide_interval", "refresh_hours", "image_cache_mb", "navigation_buffer_size",
        "paused", "last_frame", "hidden_photo_ids", "last_hidden_photo_ids",
        "_hidden_storage", "_hidden_lock", "_listeners",
    }
    names = {field.name for field in dataclasses.fields(SlideshowStore)}
    assert names == keeps_history | set(camera._RENDER_SETTINGS)
    assert not keeps_history & set(camera._RENDER_SETTINGS)


def test_pause_interval_and_buffer_changes_keep_history():
    cam, _update, store, _items, frames = _history_cam()
    for name, value in [
        ("paused", True), ("paused", False), ("slide_interval", 20),
        ("navigation_buffer_size", 5), ("image_cache_mb", 64),
    ]:
        setattr(store, name, value)
        store.notify()
        assert not cam._timeline_dirty
        assert list(cam._previous_frames) == frames[:2]
        assert cam._current_frame is frames[2]

    store.fill_mode = "contain"
    store.notify()
    assert cam._timeline_dirty
    assert not cam._previous_frames


def test_metadata_updates_keep_history_and_refresh_captions():
    cam, update, _store, items, frames = _history_cam()
    cam._last_pair_frames = [{"location": None}, {"location": None}]
    cam._current_frame = camera._RenderedFrame(
        b"pair", _cursor(2), {"photo_ids": [items[2].photo_id, items[0].photo_id]},
    )
    items[0].location = "Lisbon, Portugal"
    items[0].captured_at = 1_700_000_000_000

    update()

    assert not cam._timeline_dirty
    assert list(cam._previous_frames) == frames[:2]
    assert list(cam._next_frames) == frames[3:]
    attributes = cam.extra_state_attributes
    assert [frame["location"] for frame in attributes["caption_frames"]] == [None, "Lisbon, Portugal"]
    assert attributes["captured_at"][1].startswith("2023-11-14")


def test_playlist_changes_keep_shown_slides_at_their_new_positions():
    cam, update, _store, items, frames = _history_cam()
    serial = frames[2].serial
    cam.coordinator.data = {"items": [_media("new"), items[3], items[0], items[2]]}

    update()

    assert not cam._timeline_dirty
    assert [frame.cursor.index for frame in cam._previous_frames] == [2]
    assert cam._current_frame.serial == serial
    assert cam._current_frame.cursor.index == cam._index == 3
    assert not cam._next_frames

    cam.coordinator.data = {"items": [items[0], items[3]]}
    update()
    assert cam._timeline_dirty
    assert not cam._previous_frames


def test_new_face_data_rerenders_only_slides_that_used_it():
    cam, update, _store, items, frames = _history_cam("abcde")
    cam._next_frames.pop()
    items[4].faces = [[0.1, 0.1, 0.3, 0.3, 1.0]]
    update()
    assert not cam._timeline_dirty
    assert list(cam._previous_frames) == frames[:2]

    items[1].faces = [[0.1, 0.1, 0.3, 0.3, 1.0]]
    update()
    assert cam._timeline_dirty


def test_navigation_starts_from_the_slide_a_card_is_showing():
    def camera_ahead_of_card():
        cam = _make_cam(depth=3)
        a, b, c = _frame(0), _frame(1), _frame(2)
        shown = []
        for frame in (a, b, c):
            if cam._current_frame is not None:
                cam._previous_frames.append(cam._current_frame)
            cam._apply_frame(frame)
            shown.append(cam._frame_id)
        return cam, (a, b, c), shown

    cam, (a, b, c), shown = camera_ahead_of_card()
    asyncio.run(cam.async_force_prev(shown[1]))
    assert cam._current_frame is a
    assert list(cam._next_frames) == [b, c]
    assert cam._last_nav_outcome == "displayed"

    cam, (a, b, c), shown = camera_ahead_of_card()
    asyncio.run(cam.async_force_next(shown[0]))
    assert cam._current_frame is b
    assert list(cam._previous_frames) == [a]
    assert list(cam._next_frames) == [c]

    cam, (a, b, c), shown = camera_ahead_of_card()
    asyncio.run(cam.async_force_prev(shown[0]))
    assert cam._current_frame is a
    assert cam._frame_id == shown[2] + 1
    assert cam._last_nav_outcome == "not_available"

    cam, (a, b, c), shown = camera_ahead_of_card()
    asyncio.run(cam.async_force_prev(999))
    assert cam._current_frame is b


def test_frame_id_memory_is_bounded():
    cam = _make_cam(depth=0)
    for index in range(camera._FRAME_ID_MEMORY + 5):
        cam._apply_frame(_frame(index))
    assert len(cam._frame_serials) == camera._FRAME_ID_MEMORY
    assert min(cam._frame_serials) == 6


