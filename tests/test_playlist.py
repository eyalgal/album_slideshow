from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.album_slideshow import playlist
from custom_components.album_slideshow.const import (
    DATE_FILTER_CUSTOM,
    DATE_FILTER_LAST_7,
    DATE_FILTER_LAST_30,
    DATE_FILTER_OFF,
    DATE_FILTER_ON_THIS_DAY,
    DATE_FILTER_THIS_MONTH,
    DATE_FILTER_THIS_YEAR,
    MISSING_DATE_EXCLUDE,
    MISSING_DATE_INCLUDE,
    MISSING_DATE_USE_UPLOADED,
    ORDER_ALBUM,
    ORDER_NEWEST_ADDED,
    ORDER_NEWEST_TAKEN,
    ORDER_OLDEST_ADDED,
    ORDER_OLDEST_TAKEN,
    ORDER_RANDOM,
)


@dataclass
class _Item:
    url: str
    captured_at: int | None = None
    uploaded_at: int | None = None


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp() * 1000)


# A fixed "now" used for all date filter tests.
_NOW = datetime(2026, 4, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(("bias", "expected"), [
    (0, [1, 1, 1]), (100, [1, 5.5, 10]), (-100, [10, 5.5, 1]),
    (50, [1, 3.25, 5.5]), (-50, [5.5, 3.25, 1]),
])
def test_age_weights_adjust_in_both_directions(bias, expected):
    items = [_Item("old", captured_at=-1000), _Item("middle", captured_at=0), _Item("new", captured_at=1000)]
    assert playlist.age_weights(items, bias) == pytest.approx(expected)


@pytest.mark.parametrize("bias", [100, -100])
def test_age_weights_keep_undated_photos_neutral(bias):
    items = [_Item("old", captured_at=0), _Item("new", captured_at=1000), _Item("missing")]
    assert playlist.age_weights(items, bias)[2] == 1


def test_age_weights_use_uploaded_date_only_when_requested():
    items = [_Item("old", captured_at=0), _Item("recent", uploaded_at=1000)]
    assert playlist.age_weights(items, 100) == [1, 10]
    assert playlist.age_weights(items, 100, missing_date=MISSING_DATE_INCLUDE) == [1, 1]
    assert playlist.age_weights(items, 100, missing_date=MISSING_DATE_EXCLUDE) == [1, 1]


@pytest.mark.parametrize("items", [[], [_Item("only")], [_Item("a", 1), _Item("b", 1)]])
def test_age_weights_without_a_date_range_are_uniform(items):
    assert playlist.age_weights(items, 100) == [1.0] * len(items)


@pytest.mark.parametrize("bias", [None, True, float("nan"), float("inf"), "newer"])
def test_age_weights_reject_invalid_bias(bias):
    assert playlist.age_weights([_Item("a", 0), _Item("b", 1000)], bias) == [1, 1]


def test_age_weights_clamp_strength_without_excluding_photos():
    items = [_Item("a", 0), _Item("b", 1000)]
    assert playlist.age_weights(items, 1000) == [1, 10]
    assert playlist.age_weights(items, -1000) == [10, 1]


# -- order_items ------------------------------------------------------------

def test_order_random_is_a_passthrough():
    items = [_Item("a", captured_at=1), _Item("b", captured_at=2)]
    assert [it.url for it in playlist.order_items(items, ORDER_RANDOM)] == ["a", "b"]


def test_order_album_is_a_passthrough():
    items = [_Item("c"), _Item("a"), _Item("b")]
    assert [it.url for it in playlist.order_items(items, ORDER_ALBUM)] == ["c", "a", "b"]


def test_order_newest_taken_sorts_desc():
    items = [
        _Item("old", captured_at=_ms(2020, 1, 1)),
        _Item("new", captured_at=_ms(2024, 1, 1)),
        _Item("mid", captured_at=_ms(2022, 6, 15)),
    ]
    out = [it.url for it in playlist.order_items(items, ORDER_NEWEST_TAKEN)]
    assert out == ["new", "mid", "old"]


def test_order_oldest_taken_sorts_asc():
    items = [
        _Item("old", captured_at=_ms(2020, 1, 1)),
        _Item("new", captured_at=_ms(2024, 1, 1)),
    ]
    out = [it.url for it in playlist.order_items(items, ORDER_OLDEST_TAKEN)]
    assert out == ["old", "new"]


def test_order_newest_added_uses_uploaded_at():
    items = [
        _Item("a", captured_at=_ms(2024, 1, 1), uploaded_at=_ms(2020, 1, 1)),
        _Item("b", captured_at=_ms(2020, 1, 1), uploaded_at=_ms(2024, 1, 1)),
    ]
    out = [it.url for it in playlist.order_items(items, ORDER_NEWEST_ADDED)]
    assert out == ["b", "a"]


def test_order_oldest_added_uses_uploaded_at():
    items = [
        _Item("a", uploaded_at=_ms(2024, 1, 1)),
        _Item("b", uploaded_at=_ms(2020, 1, 1)),
    ]
    out = [it.url for it in playlist.order_items(items, ORDER_OLDEST_ADDED)]
    assert out == ["b", "a"]


def test_order_keeps_items_without_timestamp_at_end():
    items = [
        _Item("none1"),
        _Item("dated", captured_at=_ms(2024, 1, 1)),
        _Item("none2"),
    ]
    out = [it.url for it in playlist.order_items(items, ORDER_NEWEST_TAKEN)]
    # Dated item first; items without timestamps preserved at the end in
    # original order.
    assert out == ["dated", "none1", "none2"]


def test_order_unknown_mode_is_a_passthrough():
    items = [_Item("a"), _Item("b")]
    assert [it.url for it in playlist.order_items(items, "weird-mode")] == ["a", "b"]


# -- filter_items -----------------------------------------------------------

def test_filter_off_returns_all():
    items = [_Item("a"), _Item("b", captured_at=_ms(2024, 1, 1))]
    assert len(playlist.filter_items(items, mode=DATE_FILTER_OFF, now=_NOW)) == 2


def test_filter_last_7_days():
    items = [
        _Item("yesterday", captured_at=_ms(2026, 4, 28)),
        _Item("3wkago", captured_at=_ms(2026, 4, 7)),
        _Item("today", captured_at=_ms(2026, 4, 29)),
    ]
    out = [it.url for it in playlist.filter_items(items, mode=DATE_FILTER_LAST_7, now=_NOW)]
    assert out == ["yesterday", "today"]


def test_filter_last_30_days_keeps_items_without_timestamp():
    items = [
        _Item("undated"),
        _Item("3yago", captured_at=_ms(2023, 4, 29)),
        _Item("today", captured_at=_ms(2026, 4, 29)),
    ]
    out = [it.url for it in playlist.filter_items(items, mode=DATE_FILTER_LAST_30, now=_NOW)]
    # "undated" passes through (lenient mode); "3yago" is filtered out.
    assert out == ["undated", "today"]


def test_filter_this_month():
    items = [
        _Item("apr1", captured_at=_ms(2026, 4, 1)),
        _Item("mar31", captured_at=_ms(2026, 3, 31)),
        _Item("apr29", captured_at=_ms(2026, 4, 29)),
    ]
    out = [it.url for it in playlist.filter_items(items, mode=DATE_FILTER_THIS_MONTH, now=_NOW)]
    assert out == ["apr1", "apr29"]


def test_filter_this_year():
    items = [
        _Item("jan1", captured_at=_ms(2026, 1, 1)),
        _Item("dec2025", captured_at=_ms(2025, 12, 31)),
    ]
    out = [it.url for it in playlist.filter_items(items, mode=DATE_FILTER_THIS_YEAR, now=_NOW)]
    assert out == ["jan1"]


def test_filter_on_this_day_drops_undated():
    items = [
        _Item("anniversary", captured_at=_ms(2020, 4, 29)),
        _Item("other", captured_at=_ms(2020, 4, 28)),
        _Item("undated"),
    ]
    out = [it.url for it in playlist.filter_items(items, mode=DATE_FILTER_ON_THIS_DAY, now=_NOW)]
    # On-this-day is strict - undated items can't satisfy it, so they are dropped.
    assert out == ["anniversary"]


_CHICAGO = timezone(timedelta(hours=-5))


def _at(*args: int) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp() * 1000)


def test_on_this_day_uses_the_local_date():
    # 02:00 UTC on July 15 is still the evening of July 14 in Chicago.
    now = datetime(2026, 7, 15, 2, 0, tzinfo=timezone.utc)
    items = [
        _Item("evening", captured_at=_at(2020, 7, 15, 3, 0)),  # July 14, 22:00 local
        _Item("afternoon", captured_at=_at(2020, 7, 14, 20, 0)),
        _Item("next_morning", captured_at=_at(2020, 7, 15, 12, 0)),
    ]

    local = playlist.filter_items(items, mode=DATE_FILTER_ON_THIS_DAY, now=now, tz=_CHICAGO)
    utc = playlist.filter_items(items, mode=DATE_FILTER_ON_THIS_DAY, now=now)

    assert [it.url for it in local] == ["evening", "afternoon"]
    assert [it.url for it in utc] == ["evening", "next_morning"]


@pytest.mark.parametrize("mode", [DATE_FILTER_THIS_MONTH, DATE_FILTER_THIS_YEAR])
def test_month_and_year_start_at_local_midnight(mode):
    # New Year in UTC, but still December 31 in Chicago.
    now = datetime(2027, 1, 1, 3, 0, tzinfo=timezone.utc)
    items = [_Item("december", captured_at=_at(2026, 12, 20, 12, 0))]

    assert playlist.filter_items(items, mode=mode, now=now, tz=_CHICAGO) == items
    assert playlist.filter_items(items, mode=mode, now=now) == []


@pytest.mark.parametrize("days", [1, 7, 30, 365, 1825, 36500])
def test_custom_lookback_includes_exact_cutoff(days):
    cutoff = int((_NOW - timedelta(days=days)).timestamp() * 1000)
    items = [
        _Item("too_old", captured_at=cutoff - 1),
        _Item("boundary", captured_at=cutoff),
        _Item("recent", captured_at=int(_NOW.timestamp() * 1000)),
    ]

    result = playlist.filter_items(items, mode=DATE_FILTER_CUSTOM, lookback_days=days, now=_NOW)

    assert [item.url for item in result] == ["boundary", "recent"]


@pytest.mark.parametrize(("missing_date", "expected"), [
    (MISSING_DATE_INCLUDE, ["no_date", "old_upload", "recent_upload", "dated"]),
    (MISSING_DATE_USE_UPLOADED, ["no_date", "recent_upload", "dated"]),
    (MISSING_DATE_EXCLUDE, ["dated"]),
])
def test_custom_lookback_respects_missing_date_policy(missing_date, expected):
    items = [
        _Item("no_date"), _Item("old_upload", uploaded_at=_ms(1995, 1, 1)),
        _Item("recent_upload", uploaded_at=_ms(2024, 1, 1)),
        _Item("dated", captured_at=_ms(2023, 1, 1)),
    ]

    result = playlist.filter_items(
        items, mode=DATE_FILTER_CUSTOM, lookback_days=1825, missing_date=missing_date, now=_NOW,
    )

    assert [item.url for item in result] == expected


def test_custom_lookback_does_not_override_presets():
    items = [_Item("last_year", captured_at=_ms(2025, 1, 1))]
    assert playlist.filter_items(items, mode=DATE_FILTER_LAST_7, lookback_days=1825, now=_NOW) == []
    assert playlist.filter_items(items, mode=DATE_FILTER_OFF, lookback_days=1, now=_NOW) == items


@pytest.mark.parametrize("invalid", [None, "many", True, float("inf")])
def test_invalid_custom_lookback_uses_one_year(invalid):
    items = [_Item("old", captured_at=_ms(2024, 1, 1)), _Item("new", captured_at=_ms(2026, 1, 1))]
    result = playlist.filter_items(items, mode=DATE_FILTER_CUSTOM, lookback_days=invalid, now=_NOW)
    assert [item.url for item in result] == ["new"]


# -- filter_items: missing capture date -------------------------------------

def test_missing_date_use_uploaded_at_applies_window_to_upload_date():
    items = [
        _Item("recent_upload", uploaded_at=_ms(2026, 4, 28)),
        _Item("old_upload", uploaded_at=_ms(2023, 1, 1)),
        _Item("dated", captured_at=_ms(2026, 4, 29)),
    ]
    out = [
        it.url
        for it in playlist.filter_items(
            items,
            mode=DATE_FILTER_LAST_7,
            missing_date=MISSING_DATE_USE_UPLOADED,
            now=_NOW,
        )
    ]
    # Undated photo is dated by its upload date: recent one passes, old one
    # is filtered out.
    assert out == ["recent_upload", "dated"]


def test_missing_date_use_uploaded_at_keeps_fully_undated_for_windows():
    items = [
        _Item("nodates"),
        _Item("old_upload", uploaded_at=_ms(2023, 1, 1)),
    ]
    out = [
        it.url
        for it in playlist.filter_items(
            items,
            mode=DATE_FILTER_LAST_30,
            missing_date=MISSING_DATE_USE_UPLOADED,
            now=_NOW,
        )
    ]
    # No usable date at all -> lenient for window filters; old upload dropped.
    assert out == ["nodates"]


def test_missing_date_use_uploaded_at_is_default():
    items = [_Item("old_upload", uploaded_at=_ms(2023, 1, 1))]
    # Default missing_date should behave like use_uploaded_at.
    out = playlist.filter_items(items, mode=DATE_FILTER_LAST_7, now=_NOW)
    assert out == []


def test_missing_date_exclude_drops_undated():
    items = [
        _Item("undated"),
        _Item("upload_only", uploaded_at=_ms(2026, 4, 29)),
        _Item("dated", captured_at=_ms(2026, 4, 29)),
    ]
    out = [
        it.url
        for it in playlist.filter_items(
            items,
            mode=DATE_FILTER_LAST_7,
            missing_date=MISSING_DATE_EXCLUDE,
            now=_NOW,
        )
    ]
    # Only the photo with a real capture date survives.
    assert out == ["dated"]


def test_missing_date_include_keeps_undated_for_windows():
    items = [
        _Item("undated"),
        _Item("old_upload", uploaded_at=_ms(2020, 1, 1)),
        _Item("dated", captured_at=_ms(2026, 4, 29)),
    ]
    out = [
        it.url
        for it in playlist.filter_items(
            items,
            mode=DATE_FILTER_LAST_7,
            missing_date=MISSING_DATE_INCLUDE,
            now=_NOW,
        )
    ]
    # include ignores upload date and keeps every undated photo for windows.
    assert out == ["undated", "old_upload", "dated"]


def test_missing_date_use_uploaded_at_strict_on_this_day():
    items = [
        _Item("anniv_upload", uploaded_at=_ms(2019, 4, 29)),
        _Item("wrong_day_upload", uploaded_at=_ms(2019, 4, 28)),
        _Item("nodates"),
    ]
    out = [
        it.url
        for it in playlist.filter_items(
            items,
            mode=DATE_FILTER_ON_THIS_DAY,
            missing_date=MISSING_DATE_USE_UPLOADED,
            now=_NOW,
        )
    ]
    # Upload date can satisfy on_this_day; fully undated photos are dropped.
    assert out == ["anniv_upload"]
