"""A pair's second photo counts as shown, so it doesn't repeat (#60)."""
from __future__ import annotations

import asyncio
import random
import types
from collections import Counter

import pytest

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.const import ORDER_ALBUM, ORDER_RANDOM
from custom_components.album_slideshow.coordinator import MediaItem


def _item(idx: int, *, portrait: bool) -> MediaItem:
    w, h = (100, 200) if portrait else (200, 100)
    return MediaItem(url=f"item-{idx}", width=w, height=h, mime_type="image/jpeg", filename=f"{idx}.jpg")


class _FakeHass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture(autouse=True)
def _stub_image_decode(monkeypatch):
    monkeypatch.setattr(camera.ip, "open_image", lambda data, size: object())


def _cam(order_mode: str, *, seed: int = 1, index: int = 0):
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = _FakeHass()
    cam.store = types.SimpleNamespace(pair_min_gap_percent=0, order_mode=order_mode, shuffle_age_bias=0)
    cam._index = index
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cam._rng = random.Random(seed)

    async def _fetch_bytes(url):
        return b"x"

    async def _ensure_enriched(item):
        return None

    cam._fetch_bytes = _fetch_bytes
    cam._ensure_enriched = _ensure_enriched
    return cam


def _partner(cam, items):
    result = asyncio.run(cam._find_next_mismatch_image(items, False, 100, 75))
    return None if result is None else int(result[1].url.split("-")[1])


def test_random_partner_is_taken_out_of_the_rest_of_the_cycle():
    items = [_item(i, portrait=True) for i in range(10)]
    cam = _cam(ORDER_RANDOM)
    cam._random_order = [0, 7, 3, 1, 9, 2, 8, 4, 6, 5]
    cam._random_pos = 1

    assert _partner(cam, items) == 1
    assert cam._random_order[:2] == [0, 1]
    assert cam._random_pos == 2
    assert sorted(cam._random_order) == list(range(10))
    assert "item-1" in cam._recent_urls


def test_random_prefers_an_unshown_partner_beyond_the_nearest_ones():
    items = [_item(i, portrait=False) for i in range(40)]
    for i in (0, 1, 30):
        items[i] = _item(i, portrait=True)
    cam = _cam(ORDER_RANDOM)
    cam._random_order = [1, 0] + [i for i in range(2, 40)]
    cam._random_pos = 2  # photo 1 was already shown this cycle

    assert _partner(cam, items) == 30
    assert cam._random_pos == 3


def test_random_reuses_a_shown_partner_when_no_unshown_one_fits():
    items = [_item(i, portrait=False) for i in range(10)]
    items[0] = _item(0, portrait=True)
    items[1] = _item(1, portrait=True)
    cam = _cam(ORDER_RANDOM)
    cam._random_order = [1, 0] + list(range(2, 10))
    cam._random_pos = 2

    assert _partner(cam, items) == 1
    assert cam._random_pos == 2


def test_ordered_modes_step_over_the_second_photo_of_a_pair():
    items = [_item(i, portrait=True) for i in range(6)]
    cam = _cam(ORDER_ALBUM)
    frames = []
    for _ in range(4):
        frames.append((cam._index, _partner(cam, items)))
        cam._do_advance(len(items), items)

    assert frames == [(0, 1), (2, 3), (4, 5), (0, 1)]


def test_paired_photos_travel_with_the_navigation_cursor():
    cam = _cam(ORDER_ALBUM)
    cam.entry = cam.coordinator = cam._download_cache = None
    cam._paired_urls = ("item-3",)

    renderer = cam._make_renderer(cam._capture_cursor())

    assert renderer._paired_urls == ("item-3",)


def _repeats(order_mode: str, slides: int, *, n: int = 1212, seed: int = 1) -> int:
    rng = random.Random(seed)
    items = [_item(i, portrait=rng.random() < 0.7) for i in range(n)]
    cam = _cam(order_mode, seed=seed)
    shown = Counter()
    for _ in range(slides):
        cam._do_advance(n, items)
        shown[cam._index] += 1
        if items[cam._index].height > items[cam._index].width:
            partner = _partner(cam, items)
            if partner is not None:
                shown[partner] += 1
    return sum(count - 1 for count in shown.values())


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_random_pairs_do_not_repeat_within_one_cycle(seed):
    # Issue #60: about 220 repeats in 634 slides before this fix.
    assert _repeats(ORDER_RANDOM, 634, seed=seed) == 0


def test_album_order_pairs_do_not_repeat_within_one_pass():
    assert _repeats(ORDER_ALBUM, 200) == 0
