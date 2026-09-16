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


def test_default_min_gap_percent_is_off():
    # Opt-in: the default preserves the original deterministic behavior.
    assert DEFAULT_PAIR_MIN_GAP_PERCENT == 0
    assert SlideshowStore().pair_min_gap_percent == 0


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


def test_default_gap_is_deterministic_nearest_candidate_first():
    # With the feature off (the default), behavior must be byte-for-byte the
    # original algorithm: walk forward from the current index and take the
    # first orientation match, regardless of rng seed - i.e. no shuffling.
    n = 20
    items = [_item(i, portrait=False) for i in range(n)]
    items[5] = _item(5, portrait=True)
    items[12] = _item(12, portrait=True)

    for seed in range(10):
        cam = _make_search_cam(index=0, pair_min_gap_percent=0, seed=seed)
        result = _run(
            cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
        )
        assert result is not None
        assert result[1].url == "item-5"


def test_default_gap_does_not_consume_rng_state():
    # The old algorithm never touched randomness; preserving that means a
    # disabled cooldown must leave self._rng untouched, so anything else
    # relying on rng state (e.g. random order mode) is unaffected.
    n = 20
    items = [_item(i, portrait=False) for i in range(n)]
    items[5] = _item(5, portrait=True)

    cam = _make_search_cam(index=0, pair_min_gap_percent=0, seed=3)
    state_before = cam._rng.getstate()
    _run(cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100))
    assert cam._rng.getstate() == state_before


@pytest.mark.parametrize("limit", [0, 1, camera._PAIR_SEARCH_LIMIT])
def test_default_gap_candidate_generation_is_bounded(monkeypatch, limit):
    items = [_item(index, portrait=False) for index in range(1000)]
    cam = _make_search_cam(index=0, pair_min_gap_percent=0)
    generated_offsets = []

    def tracked_range(start, stop):
        for offset in range(start, stop):
            generated_offsets.append(offset)
            yield offset

    monkeypatch.setattr(camera, "range", tracked_range, raising=False)
    result = _run(
        cam._find_next_mismatch_image(
            items, is_portrait_canvas=False, width=100, height=100, limit=limit
        )
    )

    assert result is None
    assert generated_offsets == list(range(1, limit + 1))


def test_same_starting_state_reproduces_the_same_pairing_partner():
    # The navigation buffer restores Previous/Next frames by replaying a
    # captured cursor (index, recent_urls, rng state) rather than
    # recomposing - so for that replay to show the exact same paired frame,
    # this search must be a pure function of that state. This is the
    # property the buffer relies on.
    n = 40
    items = [_item(i, portrait=False) for i in range(n)]
    for i in (5, 12, 20, 33):
        items[i] = _item(i, portrait=True)

    def pick(seed):
        cam = _make_search_cam(index=0, pair_min_gap_percent=20, seed=seed)
        result = _run(
            cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
        )
        assert result is not None
        return result[1].url

    assert pick(seed=99) == pick(seed=99)


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


def test_no_eligible_partner_when_all_matches_are_recently_shown():
    # Orientation matches exist, but every one of them is in the recent-urls
    # cooldown - a normal "no partner right now" outcome, not a crash.
    n = 10
    items = [_item(i, portrait=False) for i in range(n)]
    items[3] = _item(3, portrait=True)
    items[7] = _item(7, portrait=True)

    cam = _make_search_cam(index=0, pair_min_gap_percent=0, seed=1)
    cam._recent_urls = ["item-3", "item-7"]
    result = _run(
        cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
    )
    assert result is None


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5])
@pytest.mark.parametrize("pair_min_gap_percent", [0, 10, 50])
def test_small_albums_never_crash_regardless_of_gap_setting(n, pair_min_gap_percent):
    # Every item shares the canvas orientation, so there is genuinely no
    # eligible partner - this must degrade to None gracefully at every
    # album size and every gap setting, never raise or hang.
    items = [_item(i, portrait=False) for i in range(n)]
    cam = _make_search_cam(index=0, pair_min_gap_percent=pair_min_gap_percent, seed=1)
    result = _run(
        cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
    )
    assert result is None


def test_tiny_album_with_gap_enabled_still_finds_the_only_candidate():
    # n=2 caps the exclusion radius to 0 (see _pair_exclusion_radius), so a
    # high gap setting must still fall back to finding the only other item.
    items = [_item(0, portrait=False), _item(1, portrait=True)]
    cam = _make_search_cam(index=0, pair_min_gap_percent=50, seed=1)
    result = _run(
        cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100)
    )
    assert result is not None
    assert result[1].url == "item-1"
