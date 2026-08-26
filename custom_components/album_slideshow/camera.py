from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import random
from pathlib import Path
from typing import Any

import async_timeout
from PIL import Image

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    MAX_RESOLUTION_SHORT_EDGE,
    ORIENTATION_MISMATCH_PAIR,
    ORIENTATION_MISMATCH_AVOID,
    ORDER_ALBUM,
    ORDER_RANDOM,
    PROVIDER_GOOGLE_SHARED,
)
from . import image_processing as ip
from . import playlist
from .coordinator import AlbumCoordinator, MediaItem
from .store import SlideshowStore

_LOGGER = logging.getLogger(__name__)

# Cap a single download at 64 MB. Larger images are rejected before decode
# to protect low-memory devices. This is well above any realistic camera
# JPEG; RAW/NEF/etc. aren't supported as camera frames anyway.
_MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024

# Only these content types are accepted as image bodies. If a server returns
# HTML (captive portal, 404 page rendered as 200, etc.) we reject it early.
_ACCEPTED_IMAGE_PREFIX = ("image/",)

# Max candidates we'll scan when searching for a mismatched-orientation
# pairing partner. Metadata-only checks are nearly free; decode-only checks
# (no metadata available) are expensive.
_PAIR_SEARCH_LIMIT = 12
_SKIP_SEARCH_LIMIT = 30

_MAX_RENDER_ATTEMPTS = 10


@dataclass(frozen=True, slots=True)
class _NavigationCursor:
    """All mutable ordering state needed to render the following slide."""

    index: int
    random_order: tuple[int, ...]
    random_pos: int
    recent_urls: tuple[str, ...]
    rng_state: Any


@dataclass(frozen=True, slots=True)
class _RenderedFrame:
    """A complete slide that can be displayed without any further work."""

    data: bytes
    cursor: _NavigationCursor
    meta: dict


def _ts_to_iso(ts_ms: int | None) -> str | None:
    """Convert epoch milliseconds to an ISO-8601 string in UTC, or None."""
    if not isinstance(ts_ms, int):
        return None
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _utc_now_iso() -> str:
    """Return the current time as an ISO-8601 UTC string."""
    return datetime.now(timezone.utc).isoformat()


class _DownloadCache:
    """Byte-budget LRU cache for downloaded image data, O(1) per operation."""

    def __init__(self, max_bytes: int) -> None:
        self._cache: "OrderedDict[str, bytes]" = OrderedDict()
        self._total_bytes: int = 0
        self._max_bytes: int = max(max_bytes, 1)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, url: str) -> bytes | None:
        data = self._cache.get(url)
        if data is None:
            return None
        self._cache.move_to_end(url)
        return data

    def put(self, url: str, data: bytes) -> None:
        if len(data) > self._max_bytes:
            # Item exceeds the entire cache budget; skip caching but don't raise.
            return
        if url in self._cache:
            self._total_bytes -= len(self._cache[url])
            del self._cache[url]
        self._cache[url] = data
        self._total_bytes += len(data)
        self._evict()

    def resize(self, max_bytes: int) -> None:
        self._max_bytes = max(max_bytes, 1)
        self._evict()

    def _evict(self) -> None:
        while self._total_bytes > self._max_bytes and self._cache:
            _, data = self._cache.popitem(last=False)
            self._total_bytes -= len(data)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]

    cam = AlbumSlideshowCamera(hass, entry, coordinator, store)
    hass.data[DOMAIN][entry.entry_id]["camera"] = cam

    async_add_entities([cam])


class AlbumSlideshowCamera(Camera):
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: AlbumCoordinator, store: SlideshowStore) -> None:
        super().__init__()
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator
        self.store = store

        self._attr_name = f"Album Slideshow {entry.title}"
        self._attr_unique_id = f"{entry.entry_id}_camera"

        self._rng = random.Random()
        self._index = 0
        self._random_order: list[int] = []
        self._random_pos = 0

        self._download_cache = _DownloadCache(
            max_bytes=store.image_cache_mb * 1024 * 1024
        )
        self._recent_urls: list[str] = []
        self._last_is_portrait: bool | None = None
        # When the current frame is a paired image, this is [taken_a, taken_b]
        # ISO strings (top/left first); None for single frames.
        self._last_captured_at_pair: list[str | None] | None = None
        # Full per-half caption metadata for a paired frame: a list of two
        # dicts (top/left first) each carrying captured_at / location /
        # latitude / longitude. None for single frames. Lets the Lovelace
        # card overlay an accurate caption on each half of a pair.
        self._last_pair_frames: list[dict] | None = None
        # ``horizontal`` (side-by-side, left/right) or ``vertical`` (stacked,
        # top/bottom) for a paired frame; None for single frames.
        self._last_pair_orientation: str | None = None
        # Cached effective playlist (after date filter + ordering). Invalidated
        # by any store change or coordinator update.
        self._effective_cache: tuple[int, list[MediaItem]] | None = None

        self._framebuffer: bytes | None = None

        # Monotonic counter incremented every time a new still is committed.
        # Exposed as the ``frame_id`` state attribute so the Lovelace card
        # has an unambiguous "new frame ready" signal even when other
        # attributes happen not to change between slides.
        self._frame_id: int = 0

        self._interrupt_event: asyncio.Event = asyncio.Event()
        # Navigation runs directly in the button/service coroutine. The lock
        # serialises rapid presses while buffered swaps remain independent of
        # the background timer loop.
        self._navigation_lock: asyncio.Lock = asyncio.Lock()
        self._navigation_pending: int = 0
        # Rendered timeline around the current frame. Previous frames and future
        # frames are final encoded JPEGs, so both navigation directions are an
        # O(1) deque swap. The future deque is replenished in the background.
        self._current_frame: _RenderedFrame | None = None
        self._previous_frames: deque[_RenderedFrame] = deque()
        self._next_frames: deque[_RenderedFrame] = deque()
        self._timeline_generation: int = 0
        self._timeline_dirty: bool = False
        self._next_ready_event: asyncio.Event = asyncio.Event()
        self._preload_task: asyncio.Task | None = None
        # Visible navigation diagnostics. These are state attributes rather
        # than debug-only log messages so they remain observable when Home
        # Assistant's UI filters debug logs.
        self._last_nav_direction: str | None = None
        self._last_nav_requested_at: str | None = None
        self._last_nav_started_at: str | None = None
        self._last_nav_committed_at: str | None = None
        self._last_nav_outcome: str | None = None
        self._last_nav_error: str | None = None
        self._consecutive_failures: int = 0
        self._render_task: asyncio.Task | None = None

        def _on_coordinator_update() -> None:
            self._effective_cache = None
            self._invalidate_timeline()

        coordinator.async_add_listener(_on_coordinator_update)

        def _on_store_change() -> None:
            self._download_cache.resize(self.store.image_cache_mb * 1024 * 1024)
            self._effective_cache = None
            self._invalidate_timeline()

        store.add_listener(_on_store_change)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore last framebuffer (if the store kept one) so the camera has
        # something to show immediately after a restart, rather than a broken
        # image placeholder while the first render completes.
        restored = getattr(self.store, "last_frame", None)
        if isinstance(restored, (bytes, bytearray)) and restored:
            self._framebuffer = bytes(restored)
        # Stagger the first render across multiple albums so they don't all
        # decode + encode at the same instant on HA startup. Deterministic
        # offset based on entry_id keeps the pattern stable across
        # restarts. Up to ~3 s spread across albums.
        startup_delay = (hash(self.entry.entry_id) % 3000) / 1000.0
        self._render_task = self.hass.async_create_background_task(
            self._render_loop(initial_delay=startup_delay),
            name="album_slideshow_render_loop",
        )

    async def async_will_remove_from_hass(self) -> None:
        preload_task = self._preload_task
        self._cancel_preload()
        if self._render_task is not None:
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass
        if preload_task is not None:
            try:
                await preload_task
            except asyncio.CancelledError:
                pass

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }

    @property
    def icon(self) -> str:
        if self.coordinator.provider == PROVIDER_GOOGLE_SHARED:
            return "mdi:google-photos"
        return "mdi:folder-multiple-image"

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data or {}
        items: list[MediaItem] = self._effective_items()
        cur = items[self._index] if items and 0 <= self._index < len(items) else None
        captured_at = _ts_to_iso(getattr(cur, "captured_at", None))
        captured_at_pair = self._last_captured_at_pair
        return {
            "album_title": data.get("title"),
            "media_count": len(items),
            "media_count_total": len(data.get("items", []) or []),
            "current_index": self._index,
            "current_filename": getattr(cur, "filename", None),
            "current_url": getattr(cur, "url", None),
            "current_is_portrait": self._last_is_portrait,
            "captured_at": captured_at_pair if captured_at_pair else captured_at,
            "captured_at_primary": captured_at,
            "uploaded_at": _ts_to_iso(getattr(cur, "uploaded_at", None)),
            "byte_size": getattr(cur, "byte_size", None),
            # GPS + reverse-geocoded label come from EXIF for local-folder
            # entries; Google albums leave these as ``None``.
            "latitude": getattr(cur, "latitude", None),
            "longitude": getattr(cur, "longitude", None),
            "location": getattr(cur, "location", None),
            "description": getattr(cur, "description", None),
            # Structured per-image caption metadata. A single-element list for
            # normal slides; two elements (top/left first) for paired slides,
            # so the card can overlay an accurate date/location on each half.
            # ``pair_orientation`` tells the card how the two halves are laid
            # out: ``horizontal`` (left/right) or ``vertical`` (top/bottom).
            "caption_frames": self._caption_frames(cur, captured_at),
            "pair_orientation": self._last_pair_orientation,
            "slide_interval": int(self.store.slide_interval),
            "fill_mode": self.store.fill_mode,
            "portrait_mode": self.store.portrait_mode,
            "order_mode": self.store.order_mode,
            "date_filter": self.store.date_filter,
            "missing_date_mode": self.store.missing_date_mode,
            "paused": bool(self.store.paused),
            "refresh_hours": int(self.store.refresh_hours),
            "aspect_ratio": self.store.aspect_ratio,
            "pair_divider_px": int(self.store.pair_divider_px),
            "pair_divider_color": self.store.pair_divider_color,
            "frame_id": self._frame_id,
            "navigation_buffer_size": self._buffer_depth,
            "previous_frames_cached": len(self._previous_frames),
            "next_frames_preloaded": len(self._next_frames),
            "navigation_preloading": bool(
                self._preload_task is not None and not self._preload_task.done()
            ),
            "navigation_queue_size": self._navigation_pending,
            "last_navigation_direction": self._last_nav_direction,
            "last_navigation_requested_at": self._last_nav_requested_at,
            "last_navigation_started_at": self._last_nav_started_at,
            "last_navigation_committed_at": self._last_nav_committed_at,
            "last_navigation_outcome": self._last_nav_outcome,
            "last_navigation_error": self._last_nav_error,
            "pagination_debug": data.get("pagination_debug"),
        }

    def _caption_frames(self, cur, captured_at: str | None) -> list[dict]:
        """Per-image caption metadata for the current slide.

        Returns a list with one dict for a normal slide, or two (top/left
        first) for a paired slide. Each dict carries ``captured_at`` (ISO
        string or ``None``), ``location`` (human label or ``None``), and
        ``latitude`` / ``longitude``. The card reads this to overlay an
        accurate caption on each image, including each half of a pair.
        """
        if self._last_pair_frames:
            return self._last_pair_frames
        return [
            {
                "captured_at": captured_at,
                "location": getattr(cur, "location", None),
                "latitude": getattr(cur, "latitude", None),
                "longitude": getattr(cur, "longitude", None),
                "description": getattr(cur, "description", None),
            }
        ]

    @property
    def entity_picture(self) -> str | None:
        """Return the camera proxy URL with a per-frame cache-buster.

        HA core's default ``entity_picture`` only changes when the access
        token rotates (about every five minutes). Browsers happily serve
        the cached image to the more-info dialog and other surfaces in
        between rotations, so they end up showing the previous slide
        while a fresh slide is already in the framebuffer. Appending the
        ``frame_id`` invalidates that cache as soon as a new slide is
        committed, no matter where in HA the picture is rendered.
        """
        base = super().entity_picture
        if not base:
            return base
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}frame={self._frame_id}"

    @property
    def cache_usage_mb(self) -> float:
        return round(self._download_cache.total_bytes / (1024 * 1024), 1)

    def _effective_items(self) -> list[MediaItem]:
        """Return the playlist after applying the date filter and order mode.

        Cached until the coordinator or store changes (see invalidations
        wired up in __init__).
        """
        data = self.coordinator.data or {}
        raw: list[MediaItem] = data.get("items", []) or []
        cache_key = (
            id(raw),
            self.store.date_filter,
            self.store.missing_date_mode,
            self.store.order_mode,
        )
        if self._effective_cache is not None and self._effective_cache[0] == hash(cache_key):
            return self._effective_cache[1]

        filtered = playlist.filter_items(
            raw,
            mode=self.store.date_filter,
            missing_date=self.store.missing_date_mode,
        )
        ordered = playlist.order_items(filtered, self.store.order_mode)
        self._effective_cache = (hash(cache_key), ordered)
        return ordered

    async def async_force_next(self) -> None:
        """Display the next buffered slide immediately."""
        await self._async_navigate(1)

    async def async_force_prev(self) -> None:
        """Display the previous retained slide immediately."""
        await self._async_navigate(-1)

    async def _async_navigate(self, direction: int) -> None:
        """Serialise a manual navigation request and execute it directly."""
        direction_name = "next" if direction > 0 else "previous"
        self._navigation_pending += 1
        self._last_nav_direction = direction_name
        self._last_nav_requested_at = _utc_now_iso()
        self._last_nav_outcome = "pending"
        self._last_nav_error = None
        # Wake the timer loop so the manual frame starts a fresh interval.
        self._interrupt_event.set()
        self.async_write_ha_state()

        try:
            async with self._navigation_lock:
                self._last_nav_started_at = _utc_now_iso()
                if self._timeline_dirty:
                    await self._rebuild_current_frame()
                changed = (
                    await self._show_next_frame()
                    if direction > 0
                    else await self._show_previous_frame()
                )
                if changed:
                    self._last_nav_committed_at = _utc_now_iso()
                    self._last_nav_outcome = "displayed"
                else:
                    self._last_nav_outcome = "not_available"
        except Exception as err:
            self._last_nav_outcome = "error"
            self._last_nav_error = str(err)
            _LOGGER.warning(
                "Album Slideshow %s: manual %s navigation failed: %s",
                self.entry.title,
                direction_name,
                err,
            )
        finally:
            self._navigation_pending -= 1
            self.async_write_ha_state()

    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._framebuffer

    async def handle_async_mjpeg_stream(self, request):
        """Serve the current slide as MJPEG for Home Assistant core surfaces.

        This is what the more-info dialog and picture-glance live view use
        (the camera advertises no live stream, so the frontend falls back to
        ``/api/camera_proxy_stream``). We delegate to HA's still-stream
        helper, which polls ``async_camera_image`` at ``frame_interval`` and
        writes a correct multipart response.

        Crucially it emits frames *continuously* rather than only on slide
        change. A browser parsing ``multipart/x-mixed-replace`` holds the
        current part until the next boundary arrives, so a stream that sent
        one frame and then went quiet until the next slide (potentially many
        seconds away, or never while paused) left the more-info view blank.
        Polling keeps a boundary coming right away, so the current frame
        renders immediately.
        """
        # Imported lazily so the module still loads in test environments
        # that stub out homeassistant.
        from homeassistant.components.camera import async_get_still_stream

        return await async_get_still_stream(
            request,
            self.async_camera_image,
            self.content_type,
            self.frame_interval,
        )

    # Older HA cores may dispatch via the alt name; alias for compatibility.
    async def async_handle_async_mjpeg_stream(self, request):
        return await self.handle_async_mjpeg_stream(request)

    @property
    def _buffer_depth(self) -> int:
        """Configured number of rendered frames retained in each direction."""
        return min(10, max(0, int(self.store.navigation_buffer_size)))

    def _capture_cursor(self, source=None) -> _NavigationCursor:
        """Snapshot ordering state from this camera or a private renderer."""
        source = source or self
        return _NavigationCursor(
            index=int(source._index),
            random_order=tuple(source._random_order),
            random_pos=int(source._random_pos),
            recent_urls=tuple(source._recent_urls),
            rng_state=source._rng.getstate(),
        )

    def _make_renderer(self, cursor: _NavigationCursor):
        """Create an isolated render context sharing only immutable services/cache.

        Composition helpers historically operate on ``self._index`` and random
        ordering fields. A private camera-shaped context lets background
        preloading reuse those mature helpers without ever mutating the live
        entity's current frame or navigation cursor.
        """
        renderer = AlbumSlideshowCamera.__new__(AlbumSlideshowCamera)
        renderer.hass = self.hass
        renderer.entry = self.entry
        renderer.coordinator = self.coordinator
        renderer.store = self.store
        renderer._download_cache = self._download_cache
        renderer._index = cursor.index
        renderer._random_order = list(cursor.random_order)
        renderer._random_pos = cursor.random_pos
        renderer._recent_urls = list(cursor.recent_urls)
        renderer._rng = random.Random()
        renderer._rng.setstate(cursor.rng_state)
        return renderer

    async def _render_available_frame(
        self,
        cursor: _NavigationCursor,
        items: list[MediaItem],
        *,
        advance: bool,
    ) -> _RenderedFrame:
        """Render the current or next usable slide from ``cursor``.

        Broken candidates are skipped, matching the old loop's retry behavior.
        All state mutations occur on a private renderer. The returned JPEG and
        cursor are therefore safe to place in the future buffer.
        """
        if not items:
            raise RuntimeError("No media available")

        attempts = min(len(items), _MAX_RENDER_ATTEMPTS)
        last_error: Exception | None = None
        should_advance = advance

        for _ in range(attempts):
            renderer = self._make_renderer(cursor)
            renderer._index %= len(items)
            if should_advance:
                renderer._do_advance(len(items), items)

            composed: Image.Image | None = None
            try:
                async with self._compose_semaphore:
                    composed, meta = await renderer._compose_for_index(items)
                    cursor = self._capture_cursor(renderer)
                    if composed is None:
                        raise RuntimeError("Image composition returned no frame")
                    encoded = await self.hass.async_add_executor_job(
                        ip.encode_image, composed
                    )
                return _RenderedFrame(encoded, cursor, meta or {})
            except asyncio.CancelledError:
                raise
            except Exception as err:
                last_error = err
                cursor = self._capture_cursor(renderer)
                should_advance = True
                _LOGGER.debug(
                    "Album Slideshow %s: skipping unrenderable buffered slide: %s",
                    self.entry.title,
                    err,
                )
            finally:
                ip.safe_close(composed)

        raise RuntimeError(
            f"Could not render a usable slide after {attempts} attempts: {last_error}"
        )

    def _apply_frame(self, frame: _RenderedFrame) -> None:
        """Make a rendered frame current and publish it to Home Assistant."""
        self._current_frame = frame
        self._framebuffer = frame.data
        self.store.last_frame = frame.data
        self._index = frame.cursor.index
        self._random_order = list(frame.cursor.random_order)
        self._random_pos = frame.cursor.random_pos
        self._recent_urls = list(frame.cursor.recent_urls)
        self._rng.setstate(frame.cursor.rng_state)

        meta = frame.meta
        self._last_is_portrait = meta.get("is_portrait")
        self._last_captured_at_pair = meta.get("captured_at_pair")
        self._last_pair_frames = meta.get("pair_frames")
        self._last_pair_orientation = meta.get("pair_orientation")
        self._frame_id += 1

        _LOGGER.debug(
            "Album Slideshow %s: displayed buffered frame_id=%d index=%d "
            "previous=%d next=%d",
            self.entry.title,
            self._frame_id,
            self._index,
            len(self._previous_frames),
            len(self._next_frames),
        )
        self.async_write_ha_state()

    def _trim_timeline(self) -> None:
        """Enforce the configured frame count on both sides of current."""
        depth = self._buffer_depth
        while len(self._previous_frames) > depth:
            self._previous_frames.popleft()
        while len(self._next_frames) > depth:
            self._next_frames.pop()

    def _cancel_preload(self) -> None:
        task = self._preload_task
        self._preload_task = None
        if task is not None and not task.done():
            task.cancel()
        self._next_ready_event.set()

    def _invalidate_timeline(self) -> None:
        """Drop frames rendered from stale media/settings and wake the loop."""
        self._timeline_generation += 1
        self._timeline_dirty = True
        self._previous_frames.clear()
        self._next_frames.clear()
        self._cancel_preload()
        self._interrupt_event.set()
        self.async_write_ha_state()

    def _schedule_preload(self) -> None:
        """Start the single per-camera worker that fills future frames."""
        self._trim_timeline()
        if (
            self._buffer_depth <= 0
            or self._current_frame is None
            or len(self._next_frames) >= self._buffer_depth
        ):
            return
        if self._preload_task is not None and not self._preload_task.done():
            return

        generation = self._timeline_generation
        self._preload_task = self.hass.async_create_background_task(
            self._preload_loop(generation),
            name="album_slideshow_preload",
        )

    async def _preload_loop(self, generation: int) -> None:
        """Render future slides sequentially until the configured buffer is full."""
        this_task = asyncio.current_task()
        try:
            while generation == self._timeline_generation:
                self._trim_timeline()
                if len(self._next_frames) >= self._buffer_depth:
                    return

                base = self._next_frames[-1] if self._next_frames else self._current_frame
                if base is None:
                    return
                items = self._effective_items()
                if not items:
                    return

                frame = await self._render_available_frame(
                    base.cursor,
                    items,
                    advance=True,
                )
                if generation != self._timeline_generation:
                    return

                # Navigation may have moved the base from future to current or
                # vice versa while rendering. It is still valid if it remains
                # the last frame in the known timeline.
                current_tail = (
                    self._next_frames[-1]
                    if self._next_frames
                    else self._current_frame
                )
                if current_tail is not base:
                    continue
                if len(self._next_frames) < self._buffer_depth:
                    self._next_frames.append(frame)
                    self._next_ready_event.set()
                    self.async_write_ha_state()
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            _LOGGER.warning(
                "Album Slideshow %s: could not fill navigation buffer: %s",
                self.entry.title,
                err,
            )
        finally:
            if self._preload_task is this_task:
                self._preload_task = None
            self._next_ready_event.set()
            self.async_write_ha_state()

    async def _await_preloaded_frame(self) -> bool:
        """Wait for the in-flight worker's first frame, without waiting for all X."""
        if self._next_frames:
            return True
        if self._buffer_depth <= 0:
            return False

        self._schedule_preload()
        while not self._next_frames:
            task = self._preload_task
            if task is None or task.done():
                return False
            self._next_ready_event.clear()
            if self._next_frames:
                return True
            waiter = asyncio.create_task(self._next_ready_event.wait())
            try:
                await asyncio.wait(
                    {task, waiter},
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                waiter.cancel()
                try:
                    await waiter
                except asyncio.CancelledError:
                    pass
        return True

    async def _show_next_frame(self) -> bool:
        """Display the next buffered frame, rendering on demand only if empty."""
        if self._current_frame is None:
            return await self._rebuild_current_frame()

        await self._await_preloaded_frame()
        if self._next_frames:
            frame = self._next_frames.popleft()
        else:
            items = self._effective_items()
            frame = await self._render_available_frame(
                self._current_frame.cursor,
                items,
                advance=True,
            )

        if self._buffer_depth > 0:
            self._previous_frames.append(self._current_frame)
        self._trim_timeline()
        self._apply_frame(frame)
        self._schedule_preload()
        return True

    async def _show_previous_frame(self) -> bool:
        """Restore the most recent retained frame without I/O or composition."""
        if not self._previous_frames:
            return False

        frame = self._previous_frames.pop()
        if self._current_frame is not None and self._buffer_depth > 0:
            self._next_frames.appendleft(self._current_frame)
        self._trim_timeline()
        self._apply_frame(frame)
        self._schedule_preload()
        return True

    async def _rebuild_current_frame(self) -> bool:
        """Render the current cursor after startup or playlist/settings changes."""
        generation = self._timeline_generation
        self._timeline_dirty = False
        items = self._effective_items()
        if not items:
            return False

        cursor = (
            self._current_frame.cursor
            if self._current_frame is not None
            else self._capture_cursor()
        )
        frame = await self._render_available_frame(cursor, items, advance=False)
        if generation != self._timeline_generation:
            return False
        self._previous_frames.clear()
        self._next_frames.clear()
        self._apply_frame(frame)
        self._schedule_preload()
        return True

    async def _wait_or_interrupt(self, timeout: float) -> bool:
        """Wait for configuration/navigation or for the slide timer to expire."""
        if self._timeline_dirty:
            return True
        self._interrupt_event.clear()
        # Close the clear/wait race: callbacks cannot run between these two
        # synchronous statements without setting the event again.
        if self._timeline_dirty:
            return True
        try:
            if self.store.paused:
                await self._interrupt_event.wait()
            else:
                await asyncio.wait_for(
                    self._interrupt_event.wait(),
                    timeout=timeout,
                )
            return True
        except asyncio.TimeoutError:
            return False

    async def _render_loop(self, initial_delay: float = 0.0) -> None:
        """Display buffered frames on command/timer and refill them in the background."""
        if initial_delay > 0:
            await asyncio.sleep(initial_delay)

        while self._current_frame is None:
            try:
                async with self._navigation_lock:
                    if self._current_frame is None:
                        await self._rebuild_current_frame()
                if self._current_frame is not None:
                    self._consecutive_failures = 0
                    break
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._consecutive_failures += 1
                backoff = min(2 ** self._consecutive_failures, 60)
                _LOGGER.warning(
                    "Album Slideshow: initial render failed (attempt %d), retrying in %ds: %s",
                    self._consecutive_failures,
                    backoff,
                    err,
                )
                await asyncio.sleep(backoff)
            if not self._effective_items():
                self._interrupt_event.clear()
                if not self._effective_items():
                    await self._interrupt_event.wait()

        while True:
            try:
                if self._timeline_dirty:
                    async with self._navigation_lock:
                        if self._timeline_dirty:
                            await self._rebuild_current_frame()
                    continue

                interrupted = await self._wait_or_interrupt(
                    float(int(self.store.slide_interval))
                )
                if not interrupted and not self.store.paused:
                    async with self._navigation_lock:
                        if not self._timeline_dirty:
                            await self._show_next_frame()
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._consecutive_failures += 1
                _LOGGER.warning(
                    "Album Slideshow: buffered navigation/render failed (attempt %d): %s",
                    self._consecutive_failures,
                    err,
                )

    @property
    def _compose_semaphore(self) -> asyncio.Semaphore:
        """Return the domain-wide compose semaphore, creating it on demand.

        ``__init__.py`` populates it during setup, but defensive
        initialisation here means a partially-loaded integration can
        still render without crashing.
        """
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        sem = domain_data.get("compose_semaphore")
        if sem is None:
            sem = asyncio.Semaphore(1)
            domain_data["compose_semaphore"] = sem
        return sem

    def _do_advance(self, count: int, items: list) -> None:
        """Advance _index to the next slide and commit random-order position."""
        if count <= 0:
            self._index = 0
            return

        self._index %= count
        order_mode = self.store.order_mode

        # Sequential modes (album order + sorted-by-time orderings) walk in
        # order. The list is already pre-sorted by ``order_items``, so we
        # only need to step forward.
        if order_mode != ORDER_RANDOM:
            self._index = (self._index + 1) % count
            return

        self._index = self._next_random_index(count)
        cur_url = items[self._index].url
        self._recent_urls.append(cur_url)
        keep = min(20, max(1, count - 1))
        if len(self._recent_urls) > keep:
            self._recent_urls = self._recent_urls[-keep:]

    def _peek_advance(self, count: int, items: list) -> None:
        """Advance _index without committing to random-order bookkeeping.

        Used by the orientation-avoid search so that rejected candidates
        don't burn through the random cycle and cause premature repeats.
        """
        if count <= 0:
            self._index = 0
            return
        self._index = (self._index + 1) % count

    async def _compose_for_index(
        self, items: list[MediaItem]
    ) -> tuple[Image.Image | None, dict | None]:
        """Compose the slide at ``self._index`` into a PIL image.

        Returns ``(composed, meta)`` where ``meta`` carries the orientation
        and paired-capture metadata that ``_commit_composed`` will publish
        as state attributes. Returns ``(None, None)`` if compose failed.

        Pure compose - does NOT mutate ``self._framebuffer`` or
        ``self._last_*`` state. The caller commits via ``_commit_composed``.
        """
        fill_mode = self.store.fill_mode
        portrait_mode = self.store.portrait_mode
        divider = max(0, int(self.store.pair_divider_px))
        divider_fill, transparent_divider = ip.parse_divider_color(self.store.pair_divider_color)
        max_short_edge = MAX_RESOLUTION_SHORT_EDGE.get(self.store.max_resolution)
        width, height = ip.resolve_output_size(None, None, self.store.aspect_ratio, max_short_edge)

        cur = items[self._index]
        is_portrait_canvas = height > width

        # Metadata fast path: if we can resolve orientation without downloading,
        # we may short-circuit the mismatch handling before any bytes are read.
        meta_portrait = ip.is_portrait_item_by_metadata(cur)
        if (
            meta_portrait is not None
            and meta_portrait != is_portrait_canvas
            and portrait_mode == ORIENTATION_MISMATCH_AVOID
        ):
            return await self._compose_skip_mismatch(items, width, height, fill_mode, is_portrait_canvas)

        cur_bytes = await self._fetch_bytes(cur.url)
        if not cur_bytes:
            raise RuntimeError(f"Failed to fetch image: {cur.url}")

        img = await self.hass.async_add_executor_job(
            ip.open_image, cur_bytes, (width, height)
        )
        try:
            cur_is_portrait = ip.is_portrait_item(cur, img)
            orientation_mismatch = cur_is_portrait != is_portrait_canvas

            if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_AVOID:
                ip.safe_close(img)
                img = None
                return await self._compose_skip_mismatch(items, width, height, fill_mode, is_portrait_canvas)

            if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_PAIR:
                pair = await self._find_next_mismatch_image(
                    items, is_portrait_canvas, width, height, limit=_PAIR_SEARCH_LIMIT
                )
                other_img = pair[0] if pair else None
                other_item = pair[1] if pair else None
                pair_meta: list[str | None] | None = None
                pair_frames: list[dict] | None = None
                try:
                    if other_img is not None:
                        composed = await self.hass.async_add_executor_job(
                            ip.pair_images, img, other_img, width, height, fill_mode,
                            is_portrait_canvas, divider, divider_fill, transparent_divider,
                        )
                        pair_frames = [
                            {
                                "captured_at": _ts_to_iso(getattr(cur, "captured_at", None)),
                                "location": getattr(cur, "location", None),
                                "latitude": getattr(cur, "latitude", None),
                                "longitude": getattr(cur, "longitude", None),
                                "description": getattr(cur, "description", None),
                            },
                            {
                                "captured_at": _ts_to_iso(getattr(other_item, "captured_at", None)),
                                "location": getattr(other_item, "location", None),
                                "latitude": getattr(other_item, "latitude", None),
                                "longitude": getattr(other_item, "longitude", None),
                                "description": getattr(other_item, "description", None),
                            },
                        ]
                        pair_meta = [f["captured_at"] for f in pair_frames]
                    else:
                        composed = await self.hass.async_add_executor_job(
                            ip.render_image, img, fill_mode, width, height,
                        )
                finally:
                    ip.safe_close(other_img)
                meta = {
                    "is_portrait": cur_is_portrait,
                    "captured_at_pair": pair_meta,
                    "pair_frames": pair_frames,
                    # ``pair_images`` stacks images top/bottom on a portrait
                    # canvas and places them left/right on a landscape canvas.
                    "pair_orientation": (
                        ("vertical" if is_portrait_canvas else "horizontal")
                        if pair_frames
                        else None
                    ),
                }
                return composed, meta

            composed = await self.hass.async_add_executor_job(
                ip.render_image, img, fill_mode, width, height
            )
            return composed, {
                "is_portrait": cur_is_portrait,
                "captured_at_pair": None,
            }
        finally:
            ip.safe_close(img)

    async def _compose_skip_mismatch(
        self,
        items: list[MediaItem],
        width: int,
        height: int,
        fill_mode: str,
        is_portrait_canvas: bool,
    ) -> tuple[Image.Image | None, dict | None]:
        """Skip-mismatch variant of ``_compose_for_index``.

        Walks forward (peek-advancing for non-matches) until it finds an
        image whose orientation matches the canvas, then composes it.
        """
        count = len(items)
        if count <= 0:
            return None, None

        start = self._index

        for _ in range(min(count, _SKIP_SEARCH_LIMIT)):
            cur = items[self._index]

            meta_portrait = ip.is_portrait_item_by_metadata(cur)
            if meta_portrait is not None:
                if meta_portrait != is_portrait_canvas:
                    self._peek_advance(count, items)
                    continue
                if self._index != start:
                    self._do_advance(count, items)
                return await self._compose_single(cur, width, height, fill_mode)

            b = await self._fetch_bytes(cur.url)
            if not b:
                self._peek_advance(count, items)
                continue
            img = await self.hass.async_add_executor_job(ip.open_image, b, (width, height))
            try:
                if ip.is_portrait_item(cur, img) != is_portrait_canvas:
                    self._peek_advance(count, items)
                    continue
                if self._index != start:
                    self._do_advance(count, items)
                composed = await self.hass.async_add_executor_job(
                    ip.render_image, img, fill_mode, width, height
                )
                return composed, {
                    "is_portrait": is_portrait_canvas,
                    "captured_at_pair": None,
                }
            finally:
                ip.safe_close(img)

        self._index = start
        return await self._compose_single(items[self._index], width, height, fill_mode)

    async def _compose_single(
        self,
        item: MediaItem,
        width: int,
        height: int,
        fill_mode: str,
    ) -> tuple[Image.Image | None, dict | None]:
        b = await self._fetch_bytes(item.url)
        if not b:
            return None, None
        img = await self.hass.async_add_executor_job(ip.open_image, b, (width, height))
        try:
            cur_is_portrait = ip.is_portrait_item(item, img)
            composed = await self.hass.async_add_executor_job(
                ip.render_image, img, fill_mode, width, height
            )
            return composed, {
                "is_portrait": cur_is_portrait,
                "captured_at_pair": None,
            }
        finally:
            ip.safe_close(img)

    async def _render_current(self, items: list[MediaItem]) -> bytes | None:
        """Compatibility wrapper: compose + encode the current slide.

        Kept as a thin wrapper because external code paths (e.g., tests)
        may still call it. ``_render_cycle`` no longer does.
        """
        composed, _ = await self._compose_for_index(items)
        if composed is None:
            return None
        try:
            return await self.hass.async_add_executor_job(ip.encode_image, composed)
        finally:
            ip.safe_close(composed)

    async def _find_next_mismatch_image(
        self,
        items: list[MediaItem],
        is_portrait_canvas: bool,
        width: int,
        height: int,
        limit: int = _PAIR_SEARCH_LIMIT,
    ) -> tuple[Image.Image, MediaItem] | None:
        """Find an image with the opposite orientation of the canvas.

        Uses metadata wherever possible - only candidates without width/height
        metadata are downloaded and decoded for their orientation. The returned
        PIL image is the caller's to close. The matching ``MediaItem`` is
        returned alongside so the caller can attribute timestamps etc.
        """
        if not items:
            return None
        n = len(items)
        tries = 0
        offset = 1
        while tries < limit and offset < n:
            idx = (self._index + offset) % n
            it = items[idx]
            offset += 1
            tries += 1

            if it.url in self._recent_urls:
                continue

            meta_portrait = ip.is_portrait_item_by_metadata(it)
            if meta_portrait is not None and meta_portrait == is_portrait_canvas:
                # Metadata says this one is the wrong orientation for pairing; skip.
                continue

            b = await self._fetch_bytes(it.url)
            if not b:
                continue

            try:
                img = await self.hass.async_add_executor_job(ip.open_image, b, (width, height))
            except Exception:
                continue

            if ip.is_portrait_item(it, img) != is_portrait_canvas:
                return img, it
            ip.safe_close(img)

        return None

    def _next_random_index(self, count: int) -> int:
        if count <= 1:
            self._random_order = [0]
            self._random_pos = 0
            return 0

        needs_new_cycle = len(self._random_order) != count or self._random_pos >= len(self._random_order)
        if needs_new_cycle:
            self._random_order = list(range(count))
            self._rng.shuffle(self._random_order)
            self._random_pos = 0

            if self._random_order and self._random_order[0] == self._index:
                self._random_order.append(self._random_order.pop(0))

        idx = self._random_order[self._random_pos]
        self._random_pos += 1
        return idx

    async def _fetch_bytes(self, url: str) -> bytes | None:
        cached = self._download_cache.get(url)
        if cached is not None:
            return cached

        if url.startswith("file://"):
            try:
                p = Path(url[7:])
                data = await self.hass.async_add_executor_job(p.read_bytes)
            except Exception as err:
                _LOGGER.warning("Album Slideshow: failed to read local image: %s", err)
                return None
            if len(data) > _MAX_DOWNLOAD_BYTES:
                _LOGGER.warning(
                    "Album Slideshow: local image %s is %d bytes, exceeds %d byte limit; skipping",
                    url, len(data), _MAX_DOWNLOAD_BYTES,
                )
                return None
        elif not url.startswith("http"):
            # End-to-end encrypted providers (Ente) can't be fetched by URL:
            # the coordinator downloads and decrypts the bytes for us.
            data = await self._provider_bytes(url)
            if data is None:
                return None
        else:
            data = await self._http_get(url)
            if data is None:
                return None

        self._download_cache.put(url, data)
        return data

    async def _provider_bytes(self, url: str) -> bytes | None:
        """Ask the coordinator for decrypted bytes behind a provider URL."""
        fetcher = getattr(self.coordinator, "async_fetch_image_bytes", None)
        if fetcher is None:
            _LOGGER.warning("Album Slideshow: no handler for image url %s", url)
            return None
        try:
            data = await fetcher(url)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Album Slideshow: failed to fetch %s: %s", url, err)
            return None
        if data is None:
            return None
        if len(data) > _MAX_DOWNLOAD_BYTES:
            _LOGGER.warning(
                "Album Slideshow: %s is %d bytes, exceeds %d byte limit; skipping",
                url, len(data), _MAX_DOWNLOAD_BYTES,
            )
            return None
        return data

    def _image_request_headers(self, url: str) -> dict[str, str] | None:
        """Auth headers required to fetch image bytes for some providers.

        The Immich provider stores an ``x-api-key`` header on the coordinator;
        it is sent server-side only, so the key never reaches the browser or
        the camera's ``current_url`` attribute. Returns ``None`` when no extra
        headers are needed (Google, local folder, media source).
        """
        headers = getattr(self.coordinator, "image_request_headers", None)
        if headers and isinstance(url, str) and url.startswith("http"):
            return dict(headers)
        return None

    async def _http_get(self, url: str) -> bytes | None:
        """Fetch one remote image with validation and a hard timeout."""
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(30):
                async with session.get(url, headers=self._image_request_headers(url)) as resp:
                    resp.raise_for_status()

                    content_type = resp.headers.get("Content-Type", "")
                    primary = content_type.split(";", 1)[0].strip().lower()
                    if primary and not primary.startswith(_ACCEPTED_IMAGE_PREFIX):
                        _LOGGER.debug(
                            "Album Slideshow: rejecting %s, content-type %r is not an image",
                            url, primary,
                        )
                        return None

                    content_length = resp.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            declared = int(content_length)
                        except ValueError:
                            declared = -1
                        if declared > _MAX_DOWNLOAD_BYTES:
                            _LOGGER.warning(
                                "Album Slideshow: %s advertises %d bytes, exceeds %d byte limit; skipping",
                                url, declared, _MAX_DOWNLOAD_BYTES,
                            )
                            return None

                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > _MAX_DOWNLOAD_BYTES:
                            _LOGGER.warning(
                                "Album Slideshow: %s exceeded %d byte limit mid-download; aborting",
                                url, _MAX_DOWNLOAD_BYTES,
                            )
                            return None
                        chunks.append(chunk)
                    return b"".join(chunks)
        except Exception as err:
            _LOGGER.warning("Album Slideshow: failed to fetch image: %s", err)
            return None
