from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from custom_components.album_slideshow import playlist
from custom_components.album_slideshow.const import (
    DATE_FILTER_LAST_7,
    DATE_FILTER_LAST_30,
    DATE_FILTER_OFF,
    DATE_FILTER_ON_THIS_DAY,
    DATE_FILTER_THIS_MONTH,
    DATE_FILTER_THIS_YEAR,
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
