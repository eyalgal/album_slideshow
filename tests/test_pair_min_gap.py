from __future__ import annotations

import asyncio
import random
import types

import pytest

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.const import DEFAULT_PAIR_MIN_GAP_PERCENT
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.store import SlideshowStore


# ── _pair_exclusion_radius ──────────────────────────────────────────────────


def test_default_min_gap_percent_is_ten():
    assert DEFAULT_PAIR_MIN_GAP_PERCENT == 10
    assert SlideshowStore().pair_min_gap_percent == 10


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_exclusion_radius_is_zero_for_tiny_albums(n):
    assert camera._pair_exclusion_radius(n, 50) == 0


def test_exclusion_radius_is_zero_when_gap_percent_disabled():
    assert camera._pair_exclusion_radius(1000, 0) == 0


def test_exclusion_radius_scales_with_album_size():
    # floor of 2 dominates for a small percentage of a modest album.
    assert camera._pair_exclusion_radius(10, 10) == 2
    # percentage dominates once it exceeds the floor.
    assert camera._pair_exclusion_radius(100, 10) == 10


def test_exclusion_radius_is_capped_so_candidates_remain():
    # n=4 only has 3 non-self candidates; the cap forces radius to 0 so the
    # pairing search always has somewhere to look, however small the album.
    assert camera._pair_exclusion_radius(4, 50) == 0
    assert camera._pair_exclusion_radius(5, 40) == 1


# ── _find_next_mismatch_image ───────────────────────────────────────────────


def _item(idx: int, *, portrait: bool) -> MediaItem:
    w, h = (100, 200) if portrait else (200, 100)
    return MediaItem(
        url=f"item-{idx}",
        width=w,
        height=h,
        mime_type="image/jpeg",
        filename=f"item-{idx}.jpg",
    )


class _FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture(autouse=True)
def _stub_image_decode(monkeypatch):
    # ``_find_next_mismatch_image`` always downloads+decodes to *confirm* a
    # metadata match (it only uses metadata to reject candidates early), but
    # every item in these tests carries width/height, so
    # ``ip.is_portrait_item`` resolves from metadata and never touches the
    # decoded image. A dummy object is enough to stand in for it.
    monkeypatch.setattr(camera.ip, "open_image", lambda data, size: object())


def _make_search_cam(*, index: int, pair_min_gap_percent: int, seed: int = 1):
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = _FakeHass()
    cam.store = types.SimpleNamespace(pair_min_gap_percent=pair_min_gap_percent)
    cam._index = index
    cam._recent_urls = []
    cam._rng = random.Random(seed)

    async def _fetch_bytes(url: str):
        return b"fake-bytes"

    cam._fetch_bytes = _fetch_bytes
    return cam


def _run(coro):
    return asyncio.run(coro)


def test_pairing_excludes_the_cooldown_zone_around_the_current_index():
    # 20-item album, all landscape except two portraits planted right next
    # to the current index (would have been picked first by the old
    # nearest-offset scan) and one further away that should remain eligible.
    n = 20
    items = [_item(i, portrait=False) for i in range(n)]
    items[1] = _item(1, portrait=True)  # adjacent - must be excluded
    items[19] = _item(19, portrait=True)  # adjacent (wraps) - must be excluded
    items[10] = _item(10, portrait=True)  # far away - eligible

    for seed in range(25):
        cam = _make_search_cam(index=0, pair_min_gap_percent=50, seed=seed)
        result = _run(
            cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
        )
        assert result is not None
        _, item = result
        assert item.url == "item-10"


def test_zero_gap_percent_allows_immediate_neighbors():
    n = 20
    items = [_item(i, portrait=False) for i in range(n)]
    items[1] = _item(1, portrait=True)

    cam = _make_search_cam(index=0, pair_min_gap_percent=0, seed=7)
    result = _run(
        cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
    )
    assert result is not None
    _, item = result
    assert item.url == "item-1"


def test_candidate_order_is_shuffled_not_nearest_first():
    # Many equally-eligible portrait candidates outside the cooldown zone;
    # across seeds we should see more than one of them picked, proving the
    # search isn't deterministically walking offsets in increasing order.
    n = 30
    items = [_item(i, portrait=False) for i in range(n)]
    for i in range(8, 23):
        items[i] = _item(i, portrait=True)

    picks = set()
    for seed in range(30):
        cam = _make_search_cam(index=0, pair_min_gap_percent=10, seed=seed)
        result = _run(
            cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
        )
        assert result is not None
        picks.add(result[1].url)

    assert len(picks) > 1


def test_no_eligible_candidates_returns_none():
    items = [_item(0, portrait=False), _item(1, portrait=False)]
    cam = _make_search_cam(index=0, pair_min_gap_percent=50, seed=1)
    result = _run(
        cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
    )
    assert result is None
