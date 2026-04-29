"""Playlist construction: ordering and date filtering.

Pure, dependency-free helpers so the camera and tests can share a single
implementation. Operates on ``MediaItem``-like objects that expose
``captured_at`` / ``uploaded_at`` (epoch ms or ``None``).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Iterable, TypeVar

from .const import (
    DATE_FILTER_CUSTOM,
    DATE_FILTER_LAST_7,
    DATE_FILTER_LAST_30,
    DATE_FILTER_LAST_365,
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

T = TypeVar("T")


def order_items(items: list[T], order_mode: str) -> list[T]:
    """Return a new list ordered per ``order_mode``.

    ``random`` and ``album_order`` are no-ops here - random shuffling lives
    in the camera so it can dedupe recent slides; ``album_order`` keeps the
    source order untouched. The taken/added orderings are stable; items
    without the required timestamp keep their relative position at the end.
    """
    if order_mode == ORDER_RANDOM or order_mode == ORDER_ALBUM:
        return list(items)

    key_attr, reverse = _order_key(order_mode)
    if key_attr is None:
        return list(items)

    with_ts: list[tuple[int, int, T]] = []
    without_ts: list[tuple[int, T]] = []
    for idx, it in enumerate(items):
        ts = getattr(it, key_attr, None)
        if isinstance(ts, int):
            with_ts.append((ts, idx, it))
        else:
            without_ts.append((idx, it))

    with_ts.sort(key=lambda t: (t[0], t[1]), reverse=reverse)
    return [it for _, _, it in with_ts] + [it for _, it in without_ts]


def _order_key(order_mode: str) -> tuple[str | None, bool]:
    if order_mode == ORDER_NEWEST_TAKEN:
        return "captured_at", True
    if order_mode == ORDER_OLDEST_TAKEN:
        return "captured_at", False
    if order_mode == ORDER_NEWEST_ADDED:
        return "uploaded_at", True
    if order_mode == ORDER_OLDEST_ADDED:
        return "uploaded_at", False
    return None, False


def filter_items(
    items: Iterable[T],
    *,
    mode: str,
    custom_from: str = "",
    custom_to: str = "",
    now: datetime | None = None,
) -> list[T]:
    """Filter items by ``captured_at`` according to ``mode``.

    Items with no ``captured_at`` are kept by default unless the mode is
    ``custom_range`` with both bounds supplied (treated as a strict filter).

    ``now`` is overridable for deterministic tests.
    """
    if not mode or mode == DATE_FILTER_OFF:
        return list(items)

    today_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)

    pred, strict = _build_predicate(mode, today_utc, custom_from, custom_to)
    if pred is None:
        return list(items)

    out: list[T] = []
    for it in items:
        ts = getattr(it, "captured_at", None)
        if not isinstance(ts, int):
            if not strict:
                out.append(it)
            continue
        if pred(ts):
            out.append(it)
    return out


def _build_predicate(
    mode: str,
    today_utc: datetime,
    custom_from: str,
    custom_to: str,
):
    """Return (predicate, strict). ``strict`` drops items without timestamps."""
    if mode == DATE_FILTER_LAST_7:
        cutoff = int((today_utc - timedelta(days=7)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_LAST_30:
        cutoff = int((today_utc - timedelta(days=30)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_LAST_365:
        cutoff = int((today_utc - timedelta(days=365)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_THIS_MONTH:
        start = today_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cutoff = int(start.timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_THIS_YEAR:
        start = today_utc.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        cutoff = int(start.timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_ON_THIS_DAY:
        # Match items whose UTC month+day equals today's. Useful for daily
        # "memories"-style rotation across all years.
        today_md = (today_utc.month, today_utc.day)

        def _on_this_day(ts: int) -> bool:
            d = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
            return (d.month, d.day) == today_md

        return _on_this_day, True
    if mode == DATE_FILTER_CUSTOM:
        from_ms = _parse_iso_date_to_ms(custom_from, end_of_day=False)
        to_ms = _parse_iso_date_to_ms(custom_to, end_of_day=True)
        if from_ms is None and to_ms is None:
            return None, False

        def _in_range(ts: int) -> bool:
            if from_ms is not None and ts < from_ms:
                return False
            if to_ms is not None and ts > to_ms:
                return False
            return True

        # When the user supplies a date filter, items lacking timestamps
        # can't satisfy it - drop them rather than masking the filter.
        return _in_range, True

    return None, False


def _parse_iso_date_to_ms(value: str, *, end_of_day: bool) -> int | None:
    if not value:
        return None
    try:
        d = date.fromisoformat(value.strip())
    except ValueError:
        return None
    if end_of_day:
        dt = datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=timezone.utc)
    else:
        dt = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)
