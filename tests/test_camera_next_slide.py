from __future__ import annotations

import asyncio
import types
from collections import deque

from custom_components.album_slideshow import camera


def _make_cam(paused: bool = False):
    """A bare camera instance with just the render-loop navigation state.

    Bypasses ``__init__`` (which needs hass/coordinator/store) since these
    tests only exercise the wait/interrupt and navigation planning helpers.
    """
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam._interrupt_event = asyncio.Event()
    cam._nav_requests = deque()
    cam._history = []
    cam._history_pos = -1
    cam._history_dirty = False
    cam._record_history = False
    cam._index = 0
    cam.store = types.SimpleNamespace(paused=paused)
    return cam


# ── _wait_or_interrupt ─────────────────────────────────────────────────────

def test_wait_returns_immediately_when_nav_pending():
    """A press that landed before the wait must not sleep.

    Regression for the race where a press during the previous render was
    swallowed and the user waited the full slide interval.
    """
    cam = _make_cam()
    cam._nav_requests.append(1)

    async def run():
        return await cam._wait_or_interrupt(timeout=30)

    assert asyncio.run(run()) is True


def test_wait_wakes_on_event_without_clearing_first():
    cam = _make_cam()

    async def run():
        cam._interrupt_event.set()  # e.g. a coordinator/store change
        return await cam._wait_or_interrupt(timeout=30)

    assert asyncio.run(run()) is True


def test_wait_times_out_when_idle():
    cam = _make_cam()

    async def run():
        return await cam._wait_or_interrupt(timeout=0.01)

    assert asyncio.run(run()) is False


def test_signal_set_during_render_survives_until_wait():
    """The loop clears the event before rendering, so a signal raised while
    rendering is still pending when the wait runs."""
    cam = _make_cam()

    async def run():
        cam._interrupt_event.clear()
        cam._nav_requests.append(1)  # pressed during render
        cam._interrupt_event.set()
        return await cam._wait_or_interrupt(timeout=30)

    assert asyncio.run(run()) is True


# ── history append ─────────────────────────────────────────────────────────

def test_append_history_moves_cursor_to_head():
    cam = _make_cam()
    cam._append_history(3)
    cam._append_history(7)
    assert cam._history == [3, 7]
    assert cam._history_pos == 1


def test_append_history_caps_at_max():
    cam = _make_cam()
    for i in range(camera._HISTORY_MAX + 50):
        cam._append_history(i)
    assert len(cam._history) == camera._HISTORY_MAX
    # Oldest entries dropped; newest kept; cursor at head.
    assert cam._history[-1] == camera._HISTORY_MAX + 49
    assert cam._history_pos == camera._HISTORY_MAX - 1


# ── forward / back planning ────────────────────────────────────────────────

def test_plan_forward_at_head_draws_new():
    cam = _make_cam()
    cam._history = [5]
    cam._history_pos = 0
    assert cam._plan_forward() == (True, True)


def test_plan_forward_redoes_when_rewound():
    cam = _make_cam()
    cam._history = [5, 6, 7]
    cam._history_pos = 0
    assert cam._plan_forward() == (False, False)
    assert cam._history_pos == 1
    assert cam._index == 6


def test_plan_back_steps_backward():
    cam = _make_cam()
    cam._history = [5, 6, 7]
    cam._history_pos = 2
    cam._index = 7
    assert cam._plan_back() == (False, False)
    assert cam._history_pos == 1
    assert cam._index == 6


def test_plan_back_holds_at_oldest():
    cam = _make_cam()
    cam._history = [5, 6]
    cam._history_pos = 0
    cam._index = 5
    assert cam._plan_back() == (False, False)
    assert cam._history_pos == 0
    assert cam._index == 5


# ── _plan_next dispatch ────────────────────────────────────────────────────

def test_plan_next_consumes_one_nav_request():
    cam = _make_cam()
    cam._history = [1, 2, 3]
    cam._history_pos = 2
    cam._nav_requests.extend([1, 1])  # two next presses queued
    advance, record = cam._plan_next(interrupted=True)
    # At head: forward draws a new slide.
    assert (advance, record) == (True, True)
    # Only one request consumed per cycle so none are lost.
    assert list(cam._nav_requests) == [1]


def test_plan_next_prev_walks_back():
    cam = _make_cam()
    cam._history = [1, 2, 3]
    cam._history_pos = 2
    cam._index = 3
    cam._nav_requests.append(-1)
    assert cam._plan_next(interrupted=False) == (False, False)
    assert cam._index == 2


def test_plan_next_timer_advances_forward():
    cam = _make_cam()
    cam._history = [1]
    cam._history_pos = 0
    assert cam._plan_next(interrupted=False) == (True, True)


def test_plan_next_paused_holds_current():
    cam = _make_cam(paused=True)
    cam._history = [1]
    cam._history_pos = 0
    # No nav, not interrupted, paused: hold (don't advance), don't re-record.
    assert cam._plan_next(interrupted=False) == (False, False)


def test_plan_next_interrupt_reseeds_empty_history():
    cam = _make_cam()
    # Simulate a data change that reset history.
    cam._history_dirty = True
    advance, record = cam._plan_next(interrupted=True)
    assert advance is False
    # History was emptied by the reset, so record the current frame to seed it.
    assert record is True
    assert cam._history == []
    assert cam._history_pos == -1


def test_plan_next_interrupt_does_not_duplicate_when_history_present():
    cam = _make_cam()
    cam._history = [1, 2]
    cam._history_pos = 1
    assert cam._plan_next(interrupted=True) == (False, False)


# ── end-to-end navigation sequence ─────────────────────────────────────────

def test_navigation_sequence_back_and_forward():
    """Show 10,11,12 then step back twice and forward once via the planner."""
    cam = _make_cam()
    # Three slides were shown and recorded.
    for idx in (10, 11, 12):
        cam._index = idx
        cam._append_history(idx)
    assert cam._history_pos == 2 and cam._index == 12

    # Press previous twice.
    cam._nav_requests.append(-1)
    cam._plan_next(interrupted=False)
    assert cam._index == 11
    cam._nav_requests.append(-1)
    cam._plan_next(interrupted=False)
    assert cam._index == 10

    # Press next: redo forward through history (no new draw).
    cam._nav_requests.append(1)
    advance, record = cam._plan_next(interrupted=False)
    assert (advance, record) == (False, False)
    assert cam._index == 11

