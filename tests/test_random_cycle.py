"""Random order keeps its cycle across album changes and cycle boundaries."""
from __future__ import annotations

import random
import types

import pytest

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.const import ORDER_RANDOM
from custom_components.album_slideshow.coordinator import MediaItem


def _photo(key, url: str | None = None) -> MediaItem:
    return MediaItem(
        url=url or f"https://example.test/{key}.jpg", width=200, height=100,
        mime_type="image/jpeg", filename=f"{key}.jpg", source_id=str(key),
    )


def _album(keys) -> list[MediaItem]:
    return [_photo(key) for key in keys]


def _cam(seed: int = 1):
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.store = types.SimpleNamespace(order_mode=ORDER_RANDOM, shuffle_age_bias=0)
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cam._rng = random.Random(seed)
    return cam


def _show(cam, items, slides) -> list[str]:
    shown = []
    for _ in range(slides):
        cam._do_advance(len(items), items)
        shown.append(items[cam._index].source_id)
    return shown


def test_each_photo_is_shown_once_per_cycle():
    items = _album(range(12))
    assert sorted(_show(_cam(), items, 12), key=int) == [str(i) for i in range(12)]


@pytest.mark.parametrize("seed", range(5))
def test_a_photo_waits_at_least_half_the_album_before_repeating(seed):
    n = 20
    shown = _show(_cam(seed), _album(range(n)), 5 * n)
    last_seen: dict[str, int] = {}
    for slide, key in enumerate(shown):
        if key in last_seen:
            assert slide - last_seen[key] > n // 2
        last_seen[key] = slide


def test_photos_added_mid_cycle_join_it_without_repeating_shown_ones():
    cam = _cam()
    items = _album(f"old{i}" for i in range(10))
    before = _show(cam, items, 4)
    # New photos land at the front, so every old photo moves to a new index.
    grown = _album(f"new{i}" for i in range(4)) + items
    after = _show(cam, grown, 10)
    assert sorted(after) == sorted({item.source_id for item in grown} - set(before))


def test_photos_removed_mid_cycle_leave_the_rest_of_it_intact():
    cam = _cam()
    items = _album(range(10))
    before = _show(cam, items, 5)
    unshown = [item for item in items if item.source_id not in before]
    kept = [item for item in items if item.source_id in before[:3]] + unshown[:3]
    after = _show(cam, kept[::-1], 3)
    assert sorted(after) == sorted(item.source_id for item in unshown[:3])


def test_a_swapped_photo_is_noticed_even_when_the_size_stays_the_same():
    cam = _cam()
    items = _album(range(8))
    before = _show(cam, items, 3)
    unshown = next(i for i, item in enumerate(items) if item.source_id not in before)
    swapped = list(items)
    swapped[unshown] = _photo("fresh")
    after = _show(cam, swapped, 5)
    assert "fresh" in after
    assert not set(after) & set(before)


def test_renewed_links_do_not_restart_the_cycle():
    items = _album(range(8))
    renewed = [_photo(item.source_id, url=item.url + "?sig=new") for item in items]
    twin, cam = _cam(), _cam()
    _show(twin, items, 3)
    _show(cam, items, 3)
    assert _show(cam, renewed, 5) == _show(twin, items, 5)


def test_a_partner_is_taken_out_of_the_cycle_by_identity():
    cam = _cam()
    items = _album(range(6))
    _show(cam, items, 1)
    renewed = [_photo(item.source_id, url=item.url + "?sig=new") for item in items]
    partner = cam._random_order[-1]
    cam._mark_partner_shown(partner, renewed)
    assert cam._random_pos == 2
    assert cam._random_order[1] == partner


def test_the_cycle_travels_with_the_navigation_cursor():
    cam = _cam()
    cam.hass = cam.entry = cam.coordinator = cam._download_cache = None
    items = _album(range(6))
    _show(cam, items, 2)
    renderer = cam._make_renderer(cam._capture_cursor())
    assert renderer._cycle_ids == cam._cycle_ids
    assert _show(renderer, items, 4) == _show(cam, items, 4)
