from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .const import (
    DEFAULT_SLIDE_INTERVAL,
    DEFAULT_REFRESH_HOURS,
    DEFAULT_FILL_MODE,
    DEFAULT_ORIENTATION_MISMATCH_MODE,
    DEFAULT_ORDER_MODE,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_PAIR_DIVIDER_PX,
    DEFAULT_PAIR_DIVIDER_COLOR,
    DEFAULT_IMAGE_CACHE_MB,
    DEFAULT_MAX_RESOLUTION,
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
    image_cache_mb: int = DEFAULT_IMAGE_CACHE_MB
    max_resolution: str = DEFAULT_MAX_RESOLUTION

    # In-memory last rendered frame. Not user-configurable; used to re-serve
    # the previous slide instantly across a camera reload.
    last_frame: bytes | None = None

    _listeners: list[Listener] = field(default_factory=list)

    def add_listener(self, cb: Listener) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def notify(self) -> None:
        for cb in list(self._listeners):
            cb()
