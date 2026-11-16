from __future__ import annotations

import asyncio
import random
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.const import (
    DEFAULT_PAIR_PARTNER,
    ORDER_ALBUM,
    ORDER_RANDOM,
    PAIR_PARTNER_DEFAULT,
    PAIR_PARTNER_OPTIONS,
    PAIR_PARTNER_SAME_DAY,
)
from custom_components.album_slideshow.select import PairPartnerSelect
from custom_components.album_slideshow.store import SlideshowStore


# ── Pair partner select ─────────────────────────────────────────────────────


def test_default_pair_partner_keeps_todays_pairing():
    assert DEFAULT_PAIR_PARTNER == PAIR_PARTNER_DEFAULT == "default"
    assert SlideshowStore().pair_partner == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_offers_both_modes():
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), SlideshowStore())
    assert control.options == PAIR_PARTNER_OPTIONS == ["default", "same_day_other_years"]
    assert control.current_option == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_sets_the_store_and_ignores_unknown_options():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    asyncio.run(control.async_select_option(PAIR_PARTNER_SAME_DAY))
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY
    asyncio.run(control.async_select_option("nearest"))
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY


def test_pair_partner_select_shows_default_for_an_unknown_stored_value():
    store = SlideshowStore()
    store.pair_partner = "nearest"
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    assert control.current_option == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_restores_its_value():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=PAIR_PARTNER_SAME_DAY))
    asyncio.run(control.async_added_to_hass())
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY


def test_pair_partner_select_ignores_an_unknown_restored_value():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="nearest"))
    asyncio.run(control.async_added_to_hass())
    assert store.pair_partner == PAIR_PARTNER_DEFAULT


def test_changing_pair_partner_discards_rendered_slides():
    assert "pair_partner" in camera._RENDER_SETTINGS


# ── Month-day index ─────────────────────────────────────────────────────────

LA = ZoneInfo("America/Los_Angeles")


def _ms(year, month, day, hour=12, tz=timezone.utc):
    return int(datetime(year, month, day, hour, tzinfo=tz).timestamp() * 1000)


def _day_of(month, day):
    return datetime(2001, month, day).timetuple().tm_yday


def _on(year, month, day):
    return date(year, month, day).toordinal()


def test_local_day_reads_the_calendar_day():
    assert camera._local_day(_ms(2023, 1, 1), timezone.utc) == (_on(2023, 1, 1), 1)
    assert camera._local_day(_ms(2023, 12, 31), timezone.utc) == (_on(2023, 12, 31), 365)
    assert camera._local_day(_ms(2020, 12, 31), timezone.utc) == (_on(2020, 12, 31), 365)
    assert camera._local_day(_ms(2019, 7, 14), timezone.utc) == (_on(2019, 7, 14), _day_of(7, 14))


def test_local_day_files_february_29_as_february_28():
    assert camera._local_day(_ms(2024, 2, 29), timezone.utc) == (_on(2024, 2, 29), _day_of(2, 28))
    assert camera._local_day(_ms(2024, 3, 1), timezone.utc) == (_on(2024, 3, 1), _day_of(3, 1))


def test_local_day_uses_the_given_time_zone():
    # 03:00 UTC on 15 July is still the evening of 14 July in Los Angeles.
    taken = _ms(2023, 7, 15, hour=3)
    assert camera._local_day(taken, timezone.utc) == (_on(2023, 7, 15), _day_of(7, 15))
    assert camera._local_day(taken, LA) == (_on(2023, 7, 14), _day_of(7, 14))
    # 03:00 UTC on 1 January is still New Year's Eve in Los Angeles.
    assert camera._local_day(_ms(2024, 1, 1, hour=3), LA) == (_on(2023, 12, 31), 365)


def test_local_day_without_a_date():
    assert camera._local_day(None, timezone.utc) is None
    assert camera._local_day(10**20, timezone.utc) is None


def test_ring_days_wrap_across_the_new_year():
    assert camera._ring_days(100, 0) == (100,)
    assert camera._ring_days(100, 3) == (97, 103)
    assert camera._ring_days(1, 1) == (2, 365)
    assert camera._ring_days(365, 2) == (2, 363)


def _dated(idx, taken):
    return MediaItem(url=f"item-{idx}", width=100, height=200, mime_type="image/jpeg",
                     filename=f"item-{idx}.jpg", captured_at=taken)


def _index_cam():
    return camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)


def test_same_day_index_groups_dated_photos_by_day():
    items = [_dated(0, _ms(2019, 7, 14)), _dated(1, None), _dated(2, _ms(2023, 7, 14)), _dated(3, _ms(2023, 7, 15))]
    index = _index_cam()._same_day_index(items)
    assert index == {
        _day_of(7, 14): [(0, _on(2019, 7, 14)), (2, _on(2023, 7, 14))],
        _day_of(7, 15): [(3, _on(2023, 7, 15))],
    }


def test_same_day_index_is_kept_until_the_playlist_changes(monkeypatch):
    cam = _index_cam()
    items = [_dated(0, _ms(2023, 7, 15, hour=3))]
    first = cam._same_day_index(items)
    assert cam._same_day_index(items) is first
    # A new playlist list (album refresh, filter, hidden photo, order) rebuilds.
    rebuilt = cam._same_day_index(list(items))
    assert rebuilt is not first and rebuilt == first
    # So does a change of Home Assistant's time zone.
    monkeypatch.setattr(camera.dt_util, "DEFAULT_TIME_ZONE", LA)
    assert cam._same_day_index(items) == {_day_of(7, 14): [(0, _on(2023, 7, 14))]}


# ── Same day, other years search ────────────────────────────────────────────



class _FakeHass:
    def __init__(self):
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture(autouse=True)
def _stub_image_decode(monkeypatch):
    # Every item carries width/height, so orientation resolves from metadata.
    monkeypatch.setattr(camera.ip, "open_image", lambda data, size: object())


def _photo(idx, taken=None, *, portrait=True):
    w, h = (100, 200) if portrait else (200, 100)
    return MediaItem(url=f"item-{idx}", width=w, height=h, mime_type="image/jpeg",
                     filename=f"item-{idx}.jpg", captured_at=taken)


def _album(n, current_taken, placed):
    """n landscape photos; the current one (index 0) is dated; ``placed`` maps
    index -> portrait photo's capture time (None = undated portrait)."""
    items = [_photo(i, portrait=False) for i in range(n)]
    items[0] = _photo(0, current_taken)
    for idx, taken in placed.items():
        items[idx] = _photo(idx, taken)
    return items


def _cam(*, partner=PAIR_PARTNER_SAME_DAY, gap=0, seed=1, index=0, omit_partner=False, order=ORDER_ALBUM):
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = _FakeHass()
    store = SimpleNamespace(pair_min_gap_percent=gap, order_mode=order, shuffle_age_bias=0)
    if not omit_partner:
        store.pair_partner = partner
    cam.store = store
    cam._index = index
    cam._recent_urls = []
    cam._rng = random.Random(seed)

    async def _fetch_bytes(url):
        return b"fake-bytes"

    cam._fetch_bytes = _fetch_bytes
    return cam


def _partner(cam, items):
    result = asyncio.run(cam._find_next_mismatch_image(items, is_portrait_canvas=False, width=100, height=100))
    return None if result is None else result[1].url


TODAY = _ms(2023, 7, 14)


def test_exact_day_in_another_year_beats_nearer_photos():
    items = _album(30, TODAY, {1: None, 2: _ms(2020, 7, 15), 20: _ms(2019, 7, 14)})
    for seed in range(10):
        assert _partner(_cam(seed=seed), items) == "item-20"


def test_the_same_year_never_counts():
    items = _album(30, TODAY, {2: None, 9: _ms(2023, 7, 14), 15: _ms(2023, 7, 16)})
    # No other year within 7 days: the default search takes the nearest portrait.
    assert _partner(_cam(), items) == "item-2"
    items[25] = _photo(25, _ms(2018, 7, 20))
    assert _partner(_cam(), items) == "item-25"


@pytest.mark.parametrize("days", [1, 3, 7])
def test_search_widens_up_to_seven_days(days):
    items = _album(30, TODAY, {1: None, 20: _ms(2019, 7, 14 + days)})
    assert _partner(_cam(), items) == "item-20"
    items = _album(30, TODAY, {1: None, 20: _ms(2019, 7, 14 - days)})
    assert _partner(_cam(), items) == "item-20"


def test_nearer_day_wins_over_further_day():
    items = _album(30, TODAY, {5: _ms(2019, 7, 20), 20: _ms(2018, 7, 12)})
    for seed in range(10):
        assert _partner(_cam(seed=seed), items) == "item-20"


def test_beyond_seven_days_falls_back_to_the_default_search():
    items = _album(30, TODAY, {1: None, 20: _ms(2019, 7, 22)})
    assert _partner(_cam(), items) == "item-1"


def test_days_later_across_new_year_is_the_same_occasion_not_another_year():
    # 30 Dec 2023 and 2 Jan 2024 are three days apart, not years apart.
    items = _album(30, _ms(2023, 12, 30), {1: None, 20: _ms(2024, 1, 2)})
    assert _partner(_cam(), items) == "item-1"
    items = _album(30, _ms(2024, 1, 2), {1: None, 20: _ms(2023, 12, 30)})
    assert _partner(_cam(), items) == "item-1"
    # A year earlier is another year, even within the same calendar year.
    items = _album(30, _ms(2023, 12, 30), {1: None, 20: _ms(2023, 1, 2)})
    assert _partner(_cam(), items) == "item-20"


def test_search_wraps_across_the_new_year():
    items = _album(30, _ms(2023, 12, 30), {1: None, 20: _ms(2021, 1, 2)})
    assert _partner(_cam(), items) == "item-20"
    items = _album(30, _ms(2023, 1, 2), {1: None, 20: _ms(2019, 12, 30)})
    assert _partner(_cam(), items) == "item-20"


def test_february_29_pairs_with_february_28():
    items = _album(30, _ms(2024, 2, 29), {1: _ms(2019, 3, 1), 20: _ms(2021, 2, 28)})
    assert _partner(_cam(), items) == "item-20"
    items = _album(30, _ms(2023, 2, 28), {1: _ms(2019, 3, 1), 20: _ms(2020, 2, 29)})
    assert _partner(_cam(), items) == "item-20"


def test_days_are_matched_in_home_assistants_time_zone(monkeypatch):
    # Taken 14 July 2023 at 8 PM in Los Angeles: 15 July in UTC.
    current = _ms(2023, 7, 15, hour=3)
    items = _album(30, current, {5: _ms(2019, 7, 15, hour=12), 20: _ms(2019, 7, 14, hour=12)})
    assert _partner(_cam(), items) == "item-5"
    monkeypatch.setattr(camera.dt_util, "DEFAULT_TIME_ZONE", LA)
    assert _partner(_cam(), items) == "item-20"


def _playlist_cam(raw):
    for item in raw:
        item.photo_id = item.url
    cam = _cam()
    store = SlideshowStore()
    store.order_mode = ORDER_ALBUM
    store.pair_partner = PAIR_PARTNER_SAME_DAY
    cam.store = store
    cam.coordinator = SimpleNamespace(data={"items": raw})
    cam._effective_cache = None
    return cam


def _hide(cam, *photo_ids):
    cam.store.hidden_photo_ids = frozenset(photo_ids)
    cam.store.hidden_revision += 1
    cam._paired_urls = ()


def test_hiding_a_photo_keeps_the_day_index_in_step_with_the_playlist():
    # Hiding an earlier photo shifts every later position in the filtered
    # playlist (_effective_items); the day index must follow, or it would
    # point one past the same-day photo, at the undated portrait 21.
    cam = _playlist_cam(_album(30, TODAY, {1: None, 20: _ms(2019, 7, 14), 21: None}))
    assert _partner(cam, cam._effective_items()) == "item-20"
    _hide(cam, "item-5")
    assert _partner(cam, cam._effective_items()) == "item-20"


def test_hidden_photos_never_become_partners():
    cam = _playlist_cam(_album(30, TODAY, {1: None, 20: _ms(2019, 7, 14)}))
    assert _partner(cam, cam._effective_items()) == "item-20"
    _hide(cam, "item-20")
    assert _partner(cam, cam._effective_items()) == "item-1"


def test_unshown_photos_come_before_shown_ones():
    items = _album(30, TODAY, {10: _ms(2019, 7, 14), 20: _ms(2018, 7, 14)})
    cam = _cam()
    cam._paired_urls = ("item-10",)
    assert _partner(cam, items) == "item-20"


def test_when_every_match_was_shown_the_one_shown_longest_ago_wins():
    items = _album(30, TODAY, {1: None, 10: _ms(2019, 7, 14), 20: _ms(2018, 7, 14)})
    for older, newer in (("item-20", "item-10"), ("item-10", "item-20")):
        cam = _cam()
        cam._paired_urls = (older, newer, "item-1")
        assert _partner(cam, items) == older


def test_recently_shown_photos_are_still_skipped():
    # The slideshow's last ~20 slides are never partners, in any mode.
    items = _album(30, TODAY, {1: None, 20: _ms(2019, 7, 14)})
    cam = _cam()
    cam._recent_urls = ["item-20"]
    assert _partner(cam, items) == "item-1"


@pytest.mark.parametrize("gap", [0, 50])
def test_no_match_uses_the_default_search_with_its_gap(gap):
    items = _album(30, TODAY, {1: None, 15: None})
    assert _partner(_cam(gap=gap), items) == _partner(_cam(partner=PAIR_PARTNER_DEFAULT, gap=gap), items)
    assert _partner(_cam(gap=gap), items) == ("item-1" if gap == 0 else "item-15")


def test_an_undated_photo_uses_the_default_search():
    items = _album(30, None, {1: None, 20: _ms(2019, 7, 14)})
    assert _partner(_cam(), items) == "item-1"


def test_the_same_state_replays_the_same_partner():
    items = _album(40, TODAY, {i: _ms(2000 + i % 20, 7, 14) for i in range(5, 35, 3)})
    picks = {_partner(_cam(seed=seed), items) for seed in range(30)}
    assert len(picks) > 1  # ties are shuffled
    for seed in range(5):
        assert _partner(_cam(seed=seed), items) == _partner(_cam(seed=seed), items)


@pytest.mark.parametrize("omit_partner", [True, False])
def test_default_mode_pairs_exactly_as_before(omit_partner):
    items = _album(40, TODAY, {3: None, 12: _ms(2019, 7, 14), 30: None})
    cam = _cam(partner=PAIR_PARTNER_DEFAULT, seed=4, omit_partner=omit_partner)
    untouched = random.Random(4).getstate()
    # Nearest portrait, as before, and no random numbers drawn.
    assert _partner(cam, items) == "item-3"
    assert cam._rng.getstate() == untouched


@pytest.mark.parametrize("gap", [0, 50])
def test_default_mode_never_runs_the_same_day_search(gap):
    items = _album(40, TODAY, {3: None, 12: _ms(2019, 7, 14), 30: None})
    cam = _cam(partner=PAIR_PARTNER_DEFAULT, gap=gap)

    def _refuse(_items):
        raise AssertionError("same-day search ran in default mode")

    cam._same_day_candidates = _refuse
    _partner(cam, items)


def _random_cam(items, shown_first):
    """Random order, with ``shown_first`` (then the current photo) shown this cycle."""
    cam = _cam(order=ORDER_RANDOM)
    rest = [i for i in range(len(items)) if i not in shown_first and i != 0]
    cam._random_order = [*shown_first, 0, *rest]
    cam._random_pos = len(shown_first) + 1
    cam._cycle_ids = tuple(item.url for item in items)
    return cam


def test_random_order_prefers_a_same_day_photo_not_yet_shown_this_cycle():
    items = _album(30, TODAY, {1: None, 10: _ms(2019, 7, 14), 20: _ms(2018, 7, 14)})
    assert _partner(_random_cam(items, [10]), items) == "item-20"


def test_random_order_reuses_the_same_day_photo_shown_longest_ago():
    items = _album(30, TODAY, {1: None, 10: _ms(2019, 7, 14), 20: _ms(2018, 7, 14)})
    assert _partner(_random_cam(items, [20, 10]), items) == "item-20"
    assert _partner(_random_cam(items, [10, 20]), items) == "item-10"
