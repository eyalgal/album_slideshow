from __future__ import annotations

import asyncio
from collections import deque
import random
import types

from PIL import Image

from custom_components.album_slideshow import camera
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
    cam._last_is_portrait = None
    cam._last_captured_at_pair = None
    cam._last_pair_frames = None
    cam._last_pair_orientation = None
    cam._current_frame = None
    cam._previous_frames = deque()
    cam._next_frames = deque()
    cam._nav_requests = deque()
    cam._interrupt_event = asyncio.Event()
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


def test_wait_returns_immediately_when_navigation_is_pending():
    cam = _make_cam()
    cam._nav_requests.append(1)
    assert asyncio.run(cam._wait_or_interrupt(timeout=30)) is True


def test_wait_times_out_when_idle():
    cam = _make_cam()
    assert asyncio.run(cam._wait_or_interrupt(timeout=0.01)) is False


def test_paused_wait_wakes_for_manual_navigation():
    cam = _make_cam(paused=True)

    async def run():
        waiter = asyncio.create_task(cam._wait_or_interrupt(timeout=30))
        await asyncio.sleep(0)
        cam._nav_requests.append(1)
        cam._interrupt_event.set()
        return await waiter

    assert asyncio.run(run()) is True


def test_force_navigation_queues_every_press():
    cam = _make_cam()

    async def run():
        await cam.async_force_next()
        await cam.async_force_next()
        await cam.async_force_prev()

    asyncio.run(run())
    assert list(cam._nav_requests) == [1, 1, -1]
    assert cam._last_nav_direction == "previous"
    assert cam._last_nav_requested_at is not None


def test_render_loop_processes_buffered_next_and_previous_requests():
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


