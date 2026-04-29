from __future__ import annotations

import asyncio
from collections import OrderedDict
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


def _ts_to_iso(ts_ms: int | None) -> str | None:
    """Convert epoch milliseconds to an ISO-8601 string in UTC, or None."""
    if not isinstance(ts_ms, int):
        return None
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


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
        # Cached effective playlist (after date filter + ordering). Invalidated
        # by any store change or coordinator update.
        self._effective_cache: tuple[int, list[MediaItem]] | None = None

        self._framebuffer: bytes | None = None
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._force_next: bool = False
        self._consecutive_failures: int = 0
        self._render_task: asyncio.Task | None = None

        def _on_coordinator_update() -> None:
            self._effective_cache = None
            self._interrupt_event.set()
            self.async_write_ha_state()

        coordinator.async_add_listener(_on_coordinator_update)

        def _on_store_change() -> None:
            self._download_cache.resize(self.store.image_cache_mb * 1024 * 1024)
            self._effective_cache = None
            self._interrupt_event.set()
            self.async_write_ha_state()

        store.add_listener(_on_store_change)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore last framebuffer (if the store kept one) so the camera has
        # something to show immediately after a restart, rather than a broken
        # image placeholder while the first render completes.
        restored = getattr(self.store, "last_frame", None)
        if isinstance(restored, (bytes, bytearray)) and restored:
            self._framebuffer = bytes(restored)
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
            "slide_interval": int(self.store.slide_interval),
            "fill_mode": self.store.fill_mode,
            "portrait_mode": self.store.portrait_mode,
            "order_mode": self.store.order_mode,
            "date_filter": self.store.date_filter,
            "paused": bool(self.store.paused),
            "refresh_hours": int(self.store.refresh_hours),
            "aspect_ratio": self.store.aspect_ratio,
            "pair_divider_px": int(self.store.pair_divider_px),
            "pair_divider_color": self.store.pair_divider_color,
            "pagination_debug": data.get("pagination_debug"),
        }

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
            self.store.order_mode,
        )
        if self._effective_cache is not None and self._effective_cache[0] == hash(cache_key):
            return self._effective_cache[1]

        filtered = playlist.filter_items(
            raw,
            mode=self.store.date_filter,
        )
        ordered = playlist.order_items(filtered, self.store.order_mode)
        self._effective_cache = (hash(cache_key), ordered)
        return ordered

    async def async_force_next(self) -> None:
        self._force_next = True
        self._interrupt_event.set()
        self.async_write_ha_state()

    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._framebuffer

    async def _wait_or_interrupt(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds, returning True if interrupted.

        Safe wrapper around clear() + wait_for() - callers don't have to
        worry about the ordering of the two operations. The clear() runs
        synchronously before the awaitable is created, so no interrupt can
        be lost on the single-threaded event loop.
        """
        self._interrupt_event.clear()
        try:
            await asyncio.wait_for(self._interrupt_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

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

            interrupted = await self._wait_or_interrupt(float(int(self.store.slide_interval)))
            if interrupted:
                should_advance = self._force_next
                self._force_next = False
            else:
                # Paused slideshows hold the current frame until the user
                # un-pauses or hits "next slide" explicitly.
                should_advance = not bool(self.store.paused)

    async def _render_cycle(self, advance: bool) -> None:
        """Fetch, render, and store one frame into _framebuffer."""
        items: list[MediaItem] = self._effective_items()
        if not items:
            return

        count = len(items)
        if advance:
            self._do_advance(count, items)

        out = await self._render_current(items)
        if out is not None:
            self._framebuffer = out
            # Cached on the store so a camera reload (without full HA restart)
            # can rehydrate the framebuffer instantly.
            self.store.last_frame = out
            self.async_write_ha_state()

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

    async def _render_current(self, items: list[MediaItem]) -> bytes | None:
        """Render the image at self._index and return encoded bytes."""
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
            return await self._skip_mismatch_and_render(items, width, height, fill_mode, is_portrait_canvas)

        cur_bytes = await self._fetch_bytes(cur.url)
        if not cur_bytes:
            raise RuntimeError(f"Failed to fetch image: {cur.url}")

        img = await self.hass.async_add_executor_job(
            ip.open_image, cur_bytes, (width, height)
        )
        try:
            cur_is_portrait = ip.is_portrait_item(cur, img)
            self._last_is_portrait = cur_is_portrait
            self._last_captured_at_pair = None
            orientation_mismatch = cur_is_portrait != is_portrait_canvas

            if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_AVOID:
                ip.safe_close(img)
                img = None
                return await self._skip_mismatch_and_render(items, width, height, fill_mode, is_portrait_canvas)

            if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_PAIR:
                pair = await self._find_next_mismatch_image(
                    items, is_portrait_canvas, width, height, limit=_PAIR_SEARCH_LIMIT
                )
                other_img = pair[0] if pair else None
                other_item = pair[1] if pair else None
                try:
                    if other_img is not None:
                        composed = await self.hass.async_add_executor_job(
                            ip.pair_images, img, other_img, width, height, fill_mode,
                            is_portrait_canvas, divider, divider_fill, transparent_divider,
                        )
                        # Top/left first matches the renderer's pair layout
                        # (landscape canvas: cur on left; portrait canvas: cur on top).
                        self._last_captured_at_pair = [
                            _ts_to_iso(getattr(cur, "captured_at", None)),
                            _ts_to_iso(getattr(other_item, "captured_at", None)),
                        ]
                    else:
                        composed = await self.hass.async_add_executor_job(
                            ip.render_image, img, fill_mode, width, height,
                        )
                finally:
                    ip.safe_close(other_img)
                try:
                    return await self.hass.async_add_executor_job(ip.encode_image, composed)
                finally:
                    ip.safe_close(composed)

            composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
            try:
                return await self.hass.async_add_executor_job(ip.encode_image, composed)
            finally:
                ip.safe_close(composed)
        finally:
            ip.safe_close(img)

    async def _skip_mismatch_and_render(
        self,
        items: list[MediaItem],
        width: int,
        height: int,
        fill_mode: str,
        is_portrait_canvas: bool,
    ) -> bytes | None:
        """Skip mismatched images (peeking through candidates) and render the first match.

        Peek-advances _index without touching the random cycle for rejected
        candidates, so the random order stays long.
        """
        count = len(items)
        if count <= 0:
            return None

        start = self._index

        for _ in range(min(count, _SKIP_SEARCH_LIMIT)):
            cur = items[self._index]

            # Metadata fast path: reject without ever downloading.
            meta_portrait = ip.is_portrait_item_by_metadata(cur)
            if meta_portrait is not None:
                if meta_portrait != is_portrait_canvas:
                    self._peek_advance(count, items)
                    continue
                # Metadata says it matches - commit and render.
                if self._index != start:
                    self._do_advance(count, items)
                return await self._render_single(cur, width, height, fill_mode, is_portrait_canvas)

            # No metadata; must download + decode to decide.
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
                self._last_is_portrait = is_portrait_canvas
                composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
                try:
                    return await self.hass.async_add_executor_job(ip.encode_image, composed)
                finally:
                    ip.safe_close(composed)
            finally:
                ip.safe_close(img)

        # Nothing matched within the limit; fall back to rendering the original.
        self._index = start
        return await self._render_single(items[self._index], width, height, fill_mode, is_portrait_canvas)

    async def _render_single(
        self,
        item: MediaItem,
        width: int,
        height: int,
        fill_mode: str,
        is_portrait_canvas: bool,
    ) -> bytes | None:
        b = await self._fetch_bytes(item.url)
        if not b:
            return None
        img = await self.hass.async_add_executor_job(ip.open_image, b, (width, height))
        try:
            self._last_is_portrait = ip.is_portrait_item(item, img)
            composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
            try:
                return await self.hass.async_add_executor_job(ip.encode_image, composed)
            finally:
                ip.safe_close(composed)
        finally:
            ip.safe_close(img)

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
        else:
            data = await self._http_get(url)
            if data is None:
                return None

        self._download_cache.put(url, data)
        return data

    async def _http_get(self, url: str) -> bytes | None:
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(30):
                async with session.get(url) as resp:
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
