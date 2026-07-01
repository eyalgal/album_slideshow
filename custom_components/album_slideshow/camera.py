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

        # MJPEG subscribers. Each open stream owns an asyncio.Queue of JPEG
        # byte payloads. The render loop pushes the latest still as soon
        # as it's encoded; if a subscriber falls behind we drop frames
        # for that subscriber rather than block the whole loop.
        self._mjpeg_subscribers: set[asyncio.Queue[bytes]] = set()

        # Monotonic counter incremented every time a new still is committed.
        # Exposed as the ``frame_id`` state attribute so the Lovelace card
        # has an unambiguous "new frame ready" signal even when other
        # attributes happen not to change between slides.
        self._frame_id: int = 0

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
            # GPS + reverse-geocoded label come from EXIF for local-folder
            # entries; Google albums leave these as ``None``.
            "latitude": getattr(cur, "latitude", None),
            "longitude": getattr(cur, "longitude", None),
            "location": getattr(cur, "location", None),
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
        self._force_next = True
        self._interrupt_event.set()
        self.async_write_ha_state()

    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._framebuffer

    async def handle_async_mjpeg_stream(self, request):
        """Stream the slideshow as multipart MJPEG.

        Each open client gets a bounded asyncio.Queue that the render loop
        pushes JPEG payloads into when a new still is committed. Visible
        transitions are now handled by the Lovelace card on the client
        side, so this stream just emits the latest still per slide change.
        """
        # Imported lazily so the module still loads in test environments
        # that stub out homeassistant without installing aiohttp.
        from aiohttp import web

        boundary = "frame"
        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": f"multipart/x-mixed-replace;boundary={boundary}",
                "Cache-Control": "no-cache, private",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        # Bounded queue: a slow client should fall behind on slide commits
        # rather than balloon memory.
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
        self._mjpeg_subscribers.add(queue)

        # Push the held still immediately so the client renders something
        # before the next slide change.
        if self._framebuffer is not None:
            try:
                queue.put_nowait(self._framebuffer)
            except asyncio.QueueFull:
                pass

        try:
            while True:
                payload = await queue.get()
                try:
                    await response.write(
                        b"--" + boundary.encode() + b"\r\n"
                        b"Content-Type: image/jpeg\r\n"
                        b"Content-Length: " + str(len(payload)).encode() + b"\r\n\r\n"
                        + payload + b"\r\n"
                    )
                except (ConnectionResetError, asyncio.CancelledError):
                    raise
                except Exception as err:
                    _LOGGER.debug("Album Slideshow: mjpeg client write failed: %s", err)
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            self._mjpeg_subscribers.discard(queue)
            try:
                await response.write_eof()
            except Exception:
                pass

        return response

    # Older HA cores may dispatch via the alt name; alias for compatibility.
    async def async_handle_async_mjpeg_stream(self, request):
        return await self.handle_async_mjpeg_stream(request)

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

    async def _render_loop(self, initial_delay: float = 0.0) -> None:
        """Background task: render slides into _framebuffer, advance on timer or interrupt."""
        if initial_delay > 0:
            try:
                await asyncio.sleep(initial_delay)
            except asyncio.CancelledError:
                raise
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
        """Render one frame.

        The slideshow is just "advance index, compose, encode, broadcast".
        Visible transitions are handled by the Lovelace card client-side,
        so this path stays minimal: at most one PIL decode + encode per
        slide change.

        Compose work is serialised across all albums via a domain-wide
        semaphore so 4 cameras don't all decode + encode at once.
        """
        items: list[MediaItem] = self._effective_items()
        if not items:
            return

        count = len(items)
        if advance:
            self._do_advance(count, items)

        async with self._compose_semaphore:
            composed, meta = await self._compose_for_index(items)
            if composed is None:
                return
            try:
                await self._commit_composed(composed, meta)
            finally:
                ip.safe_close(composed)

    async def _commit_composed(self, composed: Image.Image, meta: dict) -> None:
        """Encode the composed slide into the framebuffer and broadcast.

        Encodes off the loop so the JPEG encode (30-80 ms at 1080p, more
        at 4K) doesn't block HA.
        """
        encoded = await self.hass.async_add_executor_job(
            ip.encode_image, composed
        )

        self._framebuffer = encoded
        self.store.last_frame = encoded
        self._frame_id += 1
        if meta:
            self._last_is_portrait = meta.get("is_portrait")
        else:
            self._last_is_portrait = None
        self._last_captured_at_pair = meta.get("captured_at_pair") if meta else None
        self._last_pair_frames = meta.get("pair_frames") if meta else None
        self._last_pair_orientation = meta.get("pair_orientation") if meta else None

        self._broadcast_frame(encoded)
        self.async_write_ha_state()

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

    def _broadcast_frame(self, payload: bytes) -> None:
        """Push a frame to every active MJPEG subscriber.

        Slow subscribers get their frame dropped rather than backing up the
        queue; the next still emission will catch them up.
        """
        for queue in list(self._mjpeg_subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                # Drain one and retry once so a wedged client still sees
                # the latest frame eventually instead of forever stale.
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(payload)
                except asyncio.QueueFull:
                    pass

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
                            },
                            {
                                "captured_at": _ts_to_iso(getattr(other_item, "captured_at", None)),
                                "location": getattr(other_item, "location", None),
                                "latitude": getattr(other_item, "latitude", None),
                                "longitude": getattr(other_item, "longitude", None),
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
