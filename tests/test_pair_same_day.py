from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.const import (
    DEFAULT_PAIR_PARTNER,
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


def test_local_year_day_reads_the_calendar_day():
    assert camera._local_year_day(_ms(2023, 1, 1), timezone.utc) == (2023, 1)
    assert camera._local_year_day(_ms(2023, 12, 31), timezone.utc) == (2023, 365)
    assert camera._local_year_day(_ms(2020, 12, 31), timezone.utc) == (2020, 365)
    assert camera._local_year_day(_ms(2019, 7, 14), timezone.utc) == (2019, _day_of(7, 14))


def test_local_year_day_files_february_29_as_february_28():
    assert camera._local_year_day(_ms(2024, 2, 29), timezone.utc) == (2024, _day_of(2, 28))
    assert camera._local_year_day(_ms(2024, 3, 1), timezone.utc) == (2024, _day_of(3, 1))


def test_local_year_day_uses_the_given_time_zone():
    # 03:00 UTC on 15 July is still the evening of 14 July in Los Angeles.
    taken = _ms(2023, 7, 15, hour=3)
    assert camera._local_year_day(taken, timezone.utc) == (2023, _day_of(7, 15))
    assert camera._local_year_day(taken, LA) == (2023, _day_of(7, 14))
    # 03:00 UTC on 1 January is still New Year's Eve in Los Angeles.
    assert camera._local_year_day(_ms(2024, 1, 1, hour=3), LA) == (2023, 365)


def test_local_year_day_without_a_date():
    assert camera._local_year_day(None, timezone.utc) is None
    assert camera._local_year_day(10**20, timezone.utc) is None


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
    assert index == {_day_of(7, 14): [(0, 2019), (2, 2023)], _day_of(7, 15): [(3, 2023)]}


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
    assert cam._same_day_index(items) == {_day_of(7, 14): [(0, 2023)]}
