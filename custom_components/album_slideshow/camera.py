from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import random
from pathlib import Path

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
    PROVIDER_GOOGLE_SHARED,
)
from . import image_processing as ip
from .coordinator import AlbumCoordinator, MediaItem
from .store import SlideshowStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class _BytesCache:
    when: datetime
    data: bytes


class _DownloadCache:
    """Byte-budget LRU cache for downloaded image data."""

    def __init__(self, max_bytes: int) -> None:
        self._cache: dict[str, _BytesCache] = {}
        self._total_bytes: int = 0
        self._max_bytes: int = max(max_bytes, 1)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, url: str) -> bytes | None:
        entry = self._cache.get(url)
        if entry is None:
            return None
        entry.when = datetime.now(timezone.utc)
        return entry.data

    def put(self, url: str, data: bytes) -> None:
        if len(data) > self._max_bytes:
            # Item exceeds the entire cache budget; skip caching but don't raise.
            return
        if url in self._cache:
            self._total_bytes -= len(self._cache[url].data)
        self._cache[url] = _BytesCache(when=datetime.now(timezone.utc), data=data)
        self._total_bytes += len(data)
        self._evict()

    def resize(self, max_bytes: int) -> None:
        self._max_bytes = max(max_bytes, 1)
        self._evict()

    def _evict(self) -> None:
        while self._total_bytes > self._max_bytes and self._cache:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].when)
            self._total_bytes -= len(self._cache[oldest_key].data)
            del self._cache[oldest_key]


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

        self._framebuffer: bytes | None = None
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._force_next: bool = False
        self._consecutive_failures: int = 0
        self._render_task: asyncio.Task | None = None

        def _on_coordinator_update() -> None:
            self._interrupt_event.set()
            self.async_write_ha_state()

        coordinator.async_add_listener(_on_coordinator_update)

        def _on_store_change() -> None:
            self._download_cache.resize(self.store.image_cache_mb * 1024 * 1024)
            self._interrupt_event.set()
            self.async_write_ha_state()

        store.add_listener(_on_store_change)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._render_task = self.hass.async_create_background_task(
            self._render_loop(), name="album_slideshow_render_loop"
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._render_task is not None:
            self._render_task.cancel()
            try:
                await self._render_task
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
        items: list[MediaItem] = data.get("items", [])
        cur = items[self._index] if items and 0 <= self._index < len(items) else None
        return {
            "album_title": data.get("title"),
            "media_count": len(items),
            "current_index": self._index,
            "current_filename": getattr(cur, "filename", None),
            "current_url": getattr(cur, "url", None),
            "current_is_portrait": self._last_is_portrait,
            "slide_interval": int(self.store.slide_interval),
            "fill_mode": self.store.fill_mode,
            "portrait_mode": self.store.portrait_mode,
            "order_mode": self.store.order_mode,
            "refresh_hours": int(self.store.refresh_hours),
            "aspect_ratio": self.store.aspect_ratio,
            "pair_divider_px": int(self.store.pair_divider_px),
            "pair_divider_color": self.store.pair_divider_color,
            "pagination_debug": data.get("pagination_debug"),
        }

    @property
    def cache_usage_mb(self) -> float:
        return round(self._download_cache.total_bytes / (1024 * 1024), 1)

    async def async_force_next(self) -> None:
        self._force_next = True
        self._interrupt_event.set()
        self.async_write_ha_state()

    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._framebuffer

    async def _render_loop(self) -> None:
        """Background task: render slides into _framebuffer, advance on timer or interrupt."""
        should_advance = False  # Don't advance on the very first render
        while True:
            try:
                await self._render_cycle(advance=should_advance)
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._consecutive_failures += 1
                backoff = min(2 ** self._consecutive_failures, 60)
                _LOGGER.warning(
                    "Album Slideshow: render cycle failed (attempt %d), retrying in %ds: %s",
                    self._consecutive_failures, backoff, err,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                should_advance = True  # Skip the broken image on retry
                continue

            # Wait for slide_interval, or wake early on interrupt
            # clear() before wait_for() is safe: there is no await between them,
            # so no interrupt can fire in between on the single-threaded event loop.
            self._interrupt_event.clear()
            interval = int(self.store.slide_interval)
            try:
                await asyncio.wait_for(self._interrupt_event.wait(), timeout=float(interval))
                # Woken by interrupt: advance only if force_next was set
                should_advance = self._force_next
                self._force_next = False
            except asyncio.TimeoutError:
                # Normal timer expiry: advance to next slide
                should_advance = True

    async def _render_cycle(self, advance: bool) -> None:
        """Fetch, render, and store one frame into _framebuffer."""
        data = self.coordinator.data or {}
        items: list[MediaItem] = data.get("items", [])
        if not items:
            return

        count = len(items)
        if advance:
            self._do_advance(count, items)

        out = await self._render_current(items)
        if out is not None:
            self._framebuffer = out
            self.async_write_ha_state()

    def _do_advance(self, count: int, items: list) -> None:
        """Advance _index to the next slide."""
        if count <= 0:
            self._index = 0
            return

        self._index %= count
        order_mode = self.store.order_mode

        if order_mode == ORDER_ALBUM:
            self._index = (self._index + 1) % count
            return

        self._index = self._next_random_index(count)
        cur_url = items[self._index].url
        self._recent_urls.append(cur_url)
        keep = min(20, max(1, count - 1))
        if len(self._recent_urls) > keep:
            self._recent_urls = self._recent_urls[-keep:]

    async def _render_current(self, items: list[MediaItem]) -> bytes | None:
        """Render the image at self._index and return encoded bytes."""
        fill_mode = self.store.fill_mode
        portrait_mode = self.store.portrait_mode
        divider = max(0, int(self.store.pair_divider_px))
        divider_fill, transparent_divider = ip.parse_divider_color(self.store.pair_divider_color)
        max_short_edge = MAX_RESOLUTION_SHORT_EDGE.get(self.store.max_resolution)
        width, height = ip.resolve_output_size(None, None, self.store.aspect_ratio, max_short_edge)

        cur = items[self._index]
        cur_bytes = await self._fetch_bytes(cur.url)
        if not cur_bytes:
            raise RuntimeError(f"Failed to fetch image: {cur.url}")

        img = await self.hass.async_add_executor_job(ip.open_image, cur_bytes)
        cur_is_portrait = ip.is_portrait_item(cur, img)
        self._last_is_portrait = cur_is_portrait

        is_portrait_canvas = height > width
        orientation_mismatch = cur_is_portrait != is_portrait_canvas

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_AVOID:
            return await self._skip_mismatch_and_render(items, width, height, fill_mode, is_portrait_canvas)

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_PAIR:
            other = await self._find_next_mismatch_image(items, is_portrait_canvas, limit=12)
            if other:
                composed = await self.hass.async_add_executor_job(
                    ip.pair_images, img, other, width, height, fill_mode,
                    is_portrait_canvas, divider, divider_fill, transparent_divider,
                )
            else:
                composed = await self.hass.async_add_executor_job(
                    ip.render_image, img, fill_mode, width, height,
                )
            return await self.hass.async_add_executor_job(ip.encode_image, composed)

        composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
        return await self.hass.async_add_executor_job(ip.encode_image, composed)

    async def _skip_mismatch_and_render(
        self,
        items: list[MediaItem],
        width: int,
        height: int,
        fill_mode: str,
        is_portrait_canvas: bool,
    ) -> bytes | None:
        count = len(items)
        if count <= 0:
            return None

        start = self._index
        for _ in range(min(count, 30)):
            cur = items[self._index]
            b = await self._fetch_bytes(cur.url)
            if not b:
                self._do_advance(count)
                continue
            img = await self.hass.async_add_executor_job(ip.open_image, b)
            if ip.is_portrait_item(cur, img) != is_portrait_canvas:
                self._do_advance(count)
                continue

            self._last_is_portrait = is_portrait_canvas
            composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
            return await self.hass.async_add_executor_job(ip.encode_image, composed)

        self._index = start
        cur = items[self._index]
        b = await self._fetch_bytes(cur.url)
        if not b:
            return None
        img = await self.hass.async_add_executor_job(ip.open_image, b)
        self._last_is_portrait = ip.is_portrait_item(cur, img)
        composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
        return await self.hass.async_add_executor_job(ip.encode_image, composed)

    async def _find_next_mismatch_image(
        self,
        items: list[MediaItem],
        is_portrait_canvas: bool,
        limit: int = 10,
    ) -> Image.Image | None:
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

            b = await self._fetch_bytes(it.url)
            if not b:
                continue

            try:
                img = await self.hass.async_add_executor_job(ip.open_image, b)
            except Exception:
                continue

            if ip.is_portrait_item(it, img) != is_portrait_canvas:
                return img

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
        else:
            session = async_get_clientsession(self.hass)
            try:
                async with async_timeout.timeout(30):
                    resp = await session.get(url)
                    resp.raise_for_status()
                    data = await resp.read()
            except Exception as err:
                _LOGGER.warning("Album Slideshow: failed to fetch image: %s", err)
                return None

        self._download_cache.put(url, data)

        return data

