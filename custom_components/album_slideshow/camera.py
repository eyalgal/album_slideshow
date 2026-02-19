from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import io
import logging
import random
from pathlib import Path
from typing import Any

import async_timeout
from PIL import Image, ImageColor, ImageFilter, ImageOps

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    FILL_COVER,
    FILL_CONTAIN,
    FILL_BLUR,
    ORIENTATION_MISMATCH_PAIR,
    ORIENTATION_MISMATCH_SINGLE,
    ORIENTATION_MISMATCH_AVOID,
    ORDER_RANDOM,
    ORDER_ALBUM,
    PROVIDER_GOOGLE_SHARED,
)
from .coordinator import AlbumCoordinator, MediaItem
from .store import SlideshowStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class _BytesCache:
    when: datetime
    data: bytes


def _open_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def _is_portrait_img(img: Image.Image) -> bool:
    try:
        w, h = img.size
        return h > w
    except Exception:
        return False


def _is_portrait_dims(width: int | None, height: int | None) -> bool | None:
    if not width or not height:
        return None
    try:
        w = int(width)
        h = int(height)
        if w <= 0 or h <= 0:
            return None
        return h >= w
    except Exception:
        return None


def _is_portrait_item(item: MediaItem, img: Image.Image | None = None) -> bool:
    by_meta = _is_portrait_dims(item.width, item.height)
    if by_meta is not None:
        return by_meta
    if img is not None:
        return _is_portrait_img(img)
    return False


def _parse_aspect_ratio(ratio: str) -> tuple[int, int]:
    try:
        left, right = ratio.split(":", maxsplit=1)
        w = int(left)
        h = int(right)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return (16, 9)


def _resolve_output_size(req_w: int | None, req_h: int | None, ratio: str) -> tuple[int, int]:
    ratio_w, ratio_h = _parse_aspect_ratio(ratio)
    target = ratio_w / ratio_h

    if req_w is None and req_h is None:
        # Default to 4K: longest side = 3840
        if ratio_w >= ratio_h:
            width = 3840
            height = max(1, int(round(width / target)))
        else:
            height = 3840
            width = max(1, int(round(height * target)))
        return (width, height)

    if req_w is None:
        height = max(1, int(req_h or 2160))
        width = max(1, int(round(height * target)))
        return (width, height)

    if req_h is None:
        width = max(1, int(req_w or 3840))
        height = max(1, int(round(width / target)))
        return (width, height)

    req_w = max(1, int(req_w))
    req_h = max(1, int(req_h))
    if (req_w / req_h) >= target:
        height = req_h
        width = max(1, int(round(height * target)))
    else:
        width = req_w
        height = max(1, int(round(width / target)))

    return (width, height)


def _resize_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, int(round((new_w - target_w) / 2)))
    top = max(0, int(round((new_h - target_h) / 2)))
    return resized.crop((left, top, left + target_w, top + target_h))


def _resize_contain(img: Image.Image, target_w: int, target_h: int, bg=(0, 0, 0)) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), bg)
    left = (target_w - new_w) // 2
    top = (target_h - new_h) // 2
    canvas.paste(resized.convert("RGB"), (left, top))
    return canvas


def _blur_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    bg = _resize_cover(img, target_w, target_h).filter(ImageFilter.GaussianBlur(radius=24))

    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return bg

    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS).convert("RGB")

    left = (target_w - new_w) // 2
    top = (target_h - new_h) // 2
    bg.paste(fg, (left, top))
    return bg


def _render_by_fill_mode(img: Image.Image, fill_mode: str, width: int, height: int) -> Image.Image:
    if fill_mode == FILL_CONTAIN:
        return _resize_contain(img, width, height)
    if fill_mode == FILL_BLUR:
        return _blur_fill(img, width, height)
    return _resize_cover(img, width, height)


def _pair_images(
    img1: Image.Image,
    img2: Image.Image,
    target_w: int,
    target_h: int,
    fill_mode: str,
    portrait_canvas: bool,
    divider: int,
    divider_fill: tuple[int, int, int] | tuple[int, int, int, int],
    transparent_divider: bool,
) -> Image.Image:
    canvas_mode = "RGBA" if transparent_divider else "RGB"
    canvas = Image.new(canvas_mode, (target_w, target_h), divider_fill)

    if portrait_canvas:
        top_h = max(1, (target_h - divider) // 2)
        bottom_h = max(1, target_h - divider - top_h)
        top_img = _render_by_fill_mode(img1, fill_mode, target_w, top_h)
        bottom_img = _render_by_fill_mode(img2, fill_mode, target_w, bottom_h)
        canvas.paste(top_img.convert(canvas_mode), (0, 0))
        canvas.paste(bottom_img.convert(canvas_mode), (0, top_h + divider))
        return canvas

    left_w = max(1, (target_w - divider) // 2)
    right_w = max(1, target_w - divider - left_w)
    left_img = _render_by_fill_mode(img1, fill_mode, left_w, target_h)
    right_img = _render_by_fill_mode(img2, fill_mode, right_w, target_h)
    canvas.paste(left_img.convert(canvas_mode), (0, 0))
    canvas.paste(right_img.convert(canvas_mode), (left_w + divider, 0))
    return canvas


def _parse_divider_color(color: str) -> tuple[tuple[int, int, int] | tuple[int, int, int, int], bool]:
    raw = (color or "").strip().lower()
    compact = raw.replace(" ", "")
    if compact in (
        "transparent",
        "transperant",
        "none",
        "clear",
        "rgba(0,0,0,0)",
    ):
    if raw in ("transparent", "none", "clear", "rgba(0,0,0,0)"):
        return (0, 0, 0, 0), True
    try:
        return ImageColor.getrgb(color), False
    except Exception:
        return (255, 255, 255), False


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

        self._next_advance_at: datetime | None = None
        self._render_cache: _BytesCache | None = None
        self._download_cache: dict[str, _BytesCache] = {}
        self._recent_urls: list[str] = []
        self._last_is_portrait: bool | None = None

        coordinator.async_add_listener(self.async_write_ha_state)

        def _on_store_change() -> None:
            self._render_cache = None
            self.async_write_ha_state()

        store.add_listener(_on_store_change)

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

    async def async_force_next(self) -> None:
        data = self.coordinator.data or {}
        items: list[MediaItem] = data.get("items", [])
        if not items:
            return
        self._advance(len(items), force=True)
        self._render_cache = None
        self.async_write_ha_state()

    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()
        self._render_cache = None
        self.async_write_ha_state()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        width, height = _resolve_output_size(width, height, self.store.aspect_ratio)

        data = self.coordinator.data or {}
        items: list[MediaItem] = data.get("items", [])
        if not items:
            return None

        self._advance(len(items), force=False)

        now = datetime.utcnow()
        if self._render_cache and (now - self._render_cache.when) < timedelta(seconds=2):
            return self._render_cache.data

        fill_mode = self.store.fill_mode
        portrait_mode = self.store.portrait_mode
        divider = max(0, int(self.store.pair_divider_px))
        divider_fill, transparent_divider = _parse_divider_color(self.store.pair_divider_color)

        cur = items[self._index]

        cur_bytes = await self._fetch_bytes(cur.url)
        if not cur_bytes:
            return None

        img = _open_image(cur_bytes)
        cur_is_portrait = _is_portrait_item(cur, img)
        self._last_is_portrait = cur_is_portrait

        is_portrait_canvas = height > width
        orientation_mismatch = cur_is_portrait != is_portrait_canvas

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_AVOID:
            out = await self._skip_mismatch_and_render(items, width, height, fill_mode, is_portrait_canvas)
            self._render_cache = _BytesCache(when=now, data=out) if out else None
            return out

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_PAIR:
            other = await self._find_next_mismatch_image(items, is_portrait_canvas, limit=12)
            if other:
                composed = _pair_images(
                    img,
                    other,
                    width,
                    height,
                    fill_mode,
                    is_portrait_canvas,
                    divider,
                    divider_fill,
                    transparent_divider,
                )
                out = self._encode_image(composed)
                self._render_cache = _BytesCache(when=now, data=out)
                return out

            composed = _render_by_fill_mode(img, fill_mode, width, height)
            out = self._encode_image(composed)
            self._render_cache = _BytesCache(when=now, data=out)
            return out

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_SINGLE:
            composed = _render_by_fill_mode(img, fill_mode, width, height)
            out = self._encode_image(composed)
            self._render_cache = _BytesCache(when=now, data=out)
            return out

        composed = _render_by_fill_mode(img, fill_mode, width, height)

        out = self._encode_image(composed)
        self._render_cache = _BytesCache(when=now, data=out)
        return out

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
        for _ in range(0, min(count, 30)):
            cur = items[self._index]
            b = await self._fetch_bytes(cur.url)
            if not b:
                self._advance(count, force=True)
                continue
            img = _open_image(b)
            if _is_portrait_item(cur, img) != is_portrait_canvas:
                self._advance(count, force=True)
                continue

            self._last_is_portrait = is_portrait_canvas
            composed = _render_by_fill_mode(img, fill_mode, width, height)
            return self._encode_image(composed)

        self._index = start
        cur = items[self._index]
        b = await self._fetch_bytes(cur.url)
        if not b:
            return None
        img = _open_image(b)
        self._last_is_portrait = _is_portrait_item(cur, img)
        composed = _render_by_fill_mode(img, fill_mode, width, height)
        return self._encode_image(composed)

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
                img = _open_image(b)
            except Exception:
                continue

            if _is_portrait_item(it, img) != is_portrait_canvas:
                return img

        return None

    def _advance(self, count: int, force: bool) -> None:
        interval = int(self.store.slide_interval)
        now = datetime.utcnow()

        if force:
            self._next_advance_at = now + timedelta(seconds=interval)
        else:
            if self._next_advance_at is None:
                self._next_advance_at = now + timedelta(seconds=interval)
                return
            if now < self._next_advance_at:
                return
            self._next_advance_at = now + timedelta(seconds=interval)

        if count <= 0:
            self._index = 0
            return

        self._index %= count
        order_mode = self.store.order_mode

        if order_mode == ORDER_ALBUM:
            self._index = (self._index + 1) % count
            return

        self._index = self._next_random_index(count)
        cur_url = self.coordinator.data["items"][self._index].url
        self._recent_urls.append(cur_url)
        keep = min(20, max(1, count - 1))
        if len(self._recent_urls) > keep:
            self._recent_urls = self._recent_urls[-keep:]

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
        now = datetime.utcnow()
        cached = self._download_cache.get(url)
        if cached and (now - cached.when) < timedelta(minutes=10):
            return cached.data

        if url.startswith("file://"):
            try:
                p = Path(url[7:])
                data = await self.hass.async_add_executor_job(p.read_bytes)
            except Exception as err:
                _LOGGER.warning("Failed to read local image: %s", err)
                return None
        else:
            session = async_get_clientsession(self.hass)
            try:
                async with async_timeout.timeout(30):
                    resp = await session.get(url)
                    resp.raise_for_status()
                    data = await resp.read()
            except Exception as err:
                _LOGGER.warning("Failed to fetch image: %s", err)
                return None

        self._download_cache[url] = _BytesCache(when=now, data=data)

        if len(self._download_cache) > 120:
            oldest = sorted(self._download_cache.items(), key=lambda kv: kv[1].when)[:25]
            for k, _ in oldest:
                self._download_cache.pop(k, None)

        return data

    def _encode_image(self, img: Image.Image) -> bytes:
        out = io.BytesIO()
        save_opts: dict[str, Any] = {}
        if "A" in img.getbands():
            img.save(out, format="PNG", optimize=True)
            return out.getvalue()
        save_opts.update({"quality": 88, "optimize": True})
        img.convert("RGB").save(out, format="JPEG", **save_opts)
        return out.getvalue()
