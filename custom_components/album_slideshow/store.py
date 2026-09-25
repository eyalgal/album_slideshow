from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Iterable

from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    DEFAULT_SLIDE_INTERVAL,
    DEFAULT_REFRESH_HOURS,
    DEFAULT_FILL_MODE,
    DEFAULT_ORIENTATION_MISMATCH_MODE,
    DEFAULT_ORDER_MODE,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_PAIR_DIVIDER_PX,
    DEFAULT_PAIR_DIVIDER_COLOR,
    DEFAULT_PAIR_MIN_GAP_PERCENT,
    DEFAULT_IMAGE_CACHE_MB,
    DEFAULT_NAVIGATION_BUFFER_SIZE,
    DEFAULT_MAX_RESOLUTION,
    DEFAULT_DATE_FILTER,
    DEFAULT_MISSING_DATE_MODE,
)


Listener = Callable[[], None]


@dataclass
class SlideshowStore:
    slide_interval: int = DEFAULT_SLIDE_INTERVAL
    refresh_hours: int = DEFAULT_REFRESH_HOURS
    fill_mode: str = DEFAULT_FILL_MODE
    portrait_mode: str = DEFAULT_ORIENTATION_MISMATCH_MODE
    order_mode: str = DEFAULT_ORDER_MODE
    aspect_ratio: str = DEFAULT_ASPECT_RATIO
    pair_divider_px: int = DEFAULT_PAIR_DIVIDER_PX
    pair_divider_color: str = DEFAULT_PAIR_DIVIDER_COLOR
    pair_min_gap_percent: int = DEFAULT_PAIR_MIN_GAP_PERCENT
    image_cache_mb: int = DEFAULT_IMAGE_CACHE_MB
    navigation_buffer_size: int = DEFAULT_NAVIGATION_BUFFER_SIZE
    max_resolution: str = DEFAULT_MAX_RESOLUTION

    # Date filter mode (preset windows like this_year / on_this_day).
    date_filter: str = DEFAULT_DATE_FILTER

    # How the date filter treats photos with no EXIF capture date.
    missing_date_mode: str = DEFAULT_MISSING_DATE_MODE

    # Pause toggle - when True, the slideshow holds on the current frame.
    paused: bool = False

    # Draw face boxes, a centre crosshair and a crop summary on each slide.
    face_debug: bool = False

    # In-memory last rendered frame. Not user-configurable; used to re-serve
    # the previous slide instantly across a camera reload.
    last_frame: bytes | None = None

    hidden_photo_ids: frozenset[str] = field(default_factory=frozenset, init=False)
    last_hidden_photo_ids: tuple[str, ...] = field(default=(), init=False)
    hidden_revision: int = field(default=0, init=False)
    _hidden_storage: Store | None = field(default=None, init=False, repr=False)
    _hidden_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _listeners: list[Listener] = field(default_factory=list)

    async def async_load_hidden_photos(self, hass, entry_id: str) -> None:
        self._hidden_storage = Store(hass, 1, f"{DOMAIN}.{entry_id}.hidden")
        data = await self._hidden_storage.async_load()
        if data is None:
            return
        if not isinstance(data, dict):
            raise ValueError("Invalid hidden-photo storage")
        hidden = data.get("hidden", [])
        last_hidden = data.get("last_hidden", [])
        revision = data.get("revision", 0)
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid hidden-photo revision")
        if any(
            not isinstance(values, list)
            or any(not isinstance(photo_id, str) or not photo_id for photo_id in values)
            for values in (hidden, last_hidden)
        ):
            raise ValueError("Invalid hidden-photo identifiers")
        self.hidden_photo_ids = frozenset(hidden)
        self.hidden_revision = revision
        self.last_hidden_photo_ids = tuple(
            dict.fromkeys(photo_id for photo_id in last_hidden if photo_id in self.hidden_photo_ids)
        )

    async def async_set_photos_hidden(
        self, photo_ids: Iterable[str], *, hidden: bool
    ) -> bool:
        requested = frozenset(photo_ids)
        if any(not isinstance(photo_id, str) or not photo_id for photo_id in requested):
            raise ValueError("Invalid photo identifier")
        async with self._hidden_lock:
            if self._hidden_storage is None:
                raise RuntimeError("Photo exclusions have not been loaded")
            updated = (
                self.hidden_photo_ids | requested
                if hidden
                else self.hidden_photo_ids - requested
            )
            if updated == self.hidden_photo_ids:
                return False
            last_hidden = (
                tuple(sorted(updated - self.hidden_photo_ids))
                if hidden
                else tuple(photo_id for photo_id in self.last_hidden_photo_ids if photo_id in updated)
            )
            await self._hidden_storage.async_save(
                {
                    "hidden": sorted(updated),
                    "last_hidden": list(last_hidden),
                    "revision": self.hidden_revision + 1,
                }
            )
            self.hidden_photo_ids = updated
            self.last_hidden_photo_ids = last_hidden
            self.hidden_revision += 1
            self.last_frame = None
            self.notify()
            return True

    def add_listener(self, cb: Listener) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def notify(self) -> None:
        for cb in list(self._listeners):
            cb()
