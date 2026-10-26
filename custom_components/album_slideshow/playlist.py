"""Playlist construction: ordering and date filtering.

Pure, dependency-free helpers so the camera and tests can share a single
implementation. Operates on ``MediaItem``-like objects that expose
``captured_at`` / ``uploaded_at`` (epoch ms or ``None``).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
import math
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
    DEFAULT_CUSTOM_LOOKBACK_DAYS,
    DEFAULT_MISSING_DATE_MODE,
    MAX_CUSTOM_LOOKBACK_DAYS,
    MISSING_DATE_EXCLUDE,
    MISSING_DATE_USE_UPLOADED,
    ORDER_ALBUM,
    ORDER_NEWEST_ADDED,
    ORDER_NEWEST_TAKEN,
    ORDER_OLDEST_ADDED,
    ORDER_OLDEST_TAKEN,
    ORDER_RANDOM,
)

T = TypeVar("T")


def age_weights(
    items: list[T],
    bias: float,
    *,
    missing_date: str = DEFAULT_MISSING_DATE_MODE,
) -> list[float]:
    """Return relative draw weights, from 1 to 10, favoring newer (+) or older (-)."""
    if isinstance(bias, bool) or not isinstance(bias, (int, float)) or not math.isfinite(bias):
        bias = 0
    bias = max(-100, min(100, bias))
    if not bias:
        return [1.0] * len(items)
    timestamps = []
    for item in items:
        timestamp = getattr(item, "captured_at", None)
        if type(timestamp) is not int and missing_date == MISSING_DATE_USE_UPLOADED:
            timestamp = getattr(item, "uploaded_at", None)
        timestamps.append(timestamp if type(timestamp) is int else None)
    dated = [timestamp for timestamp in timestamps if timestamp is not None]
    if not dated or min(dated) == max(dated):
        return [1.0] * len(items)
    oldest, newest = min(dated), max(dated)
    strength = 9 * abs(bias) / 100
    weights = []
    for timestamp in timestamps:
        if timestamp is None:
            weights.append(1.0)
            continue
        position = (timestamp - oldest) / (newest - oldest)
        weights.append(1 + strength * (position if bias > 0 else 1 - position))
    return weights


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
    missing_date: str = DEFAULT_MISSING_DATE_MODE,
    lookback_days: int = DEFAULT_CUSTOM_LOOKBACK_DAYS,
    now: datetime | None = None,
    tz: tzinfo | None = None,
) -> list[T]:
    """Filter items by date according to ``mode``.

    ``missing_date`` decides what happens to photos that have no EXIF
    ``captured_at``:

    - ``use_uploaded_at`` - fall back to ``uploaded_at`` for the comparison
      so date windows stay meaningful. Photos with neither timestamp are
      kept for window filters and dropped for the strict ``on_this_day``.
    - ``include`` - keep undated photos for window filters (the legacy
      behaviour); the strict ``on_this_day`` still drops them.
    - ``exclude`` - drop undated photos entirely.

    Calendar modes (this month, this year, on this day) use ``tz``, Home
    Assistant's time zone, and UTC when it is omitted. ``now`` is
    overridable for deterministic tests.
    """
    if not mode or mode == DATE_FILTER_OFF:
        return list(items)

    today = (now or datetime.now(timezone.utc)).astimezone(tz or timezone.utc)

    pred, strict = _build_predicate(mode, today, lookback_days)
    if pred is None:
        return list(items)

    out: list[T] = []
    for it in items:
        ts = getattr(it, "captured_at", None)
        if isinstance(ts, int):
            if pred(ts):
                out.append(it)
            continue

        # No capture date: behaviour depends on ``missing_date``.
        if missing_date == MISSING_DATE_EXCLUDE:
            continue
        if missing_date == MISSING_DATE_USE_UPLOADED:
            up = getattr(it, "uploaded_at", None)
            if isinstance(up, int):
                if pred(up):
                    out.append(it)
                continue
            # No upload date either: fall through to the lenient default.

        # ``include`` (or ``use_uploaded_at`` with no usable date): keep for
        # window filters, drop for strict modes like ``on_this_day``.
        if not strict:
            out.append(it)
    return out


def _build_predicate(
    mode: str,
    today: datetime,
    lookback_days: int = DEFAULT_CUSTOM_LOOKBACK_DAYS,
):
    """Return (predicate, strict). ``strict`` drops items without timestamps."""
    if mode == DATE_FILTER_CUSTOM:
        days = lookback_days if type(lookback_days) is int else DEFAULT_CUSTOM_LOOKBACK_DAYS
        days = max(1, min(MAX_CUSTOM_LOOKBACK_DAYS, days))
        cutoff = int((today - timedelta(days=days)).timestamp() * 1000)
        return (lambda timestamp: timestamp >= cutoff), False
    if mode == DATE_FILTER_LAST_7:
        cutoff = int((today - timedelta(days=7)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_LAST_30:
        cutoff = int((today - timedelta(days=30)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_LAST_365:
        cutoff = int((today - timedelta(days=365)).timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_THIS_MONTH:
        start = today.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        cutoff = int(start.timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_THIS_YEAR:
        start = today.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        cutoff = int(start.timestamp() * 1000)
        return (lambda ts: ts >= cutoff), False
    if mode == DATE_FILTER_ON_THIS_DAY:
        # Match items whose local month+day equals today's. Useful for daily
        # "memories"-style rotation across all years.
        today_md = (today.month, today.day)

        def _on_this_day(ts: int) -> bool:
            d = datetime.fromtimestamp(ts / 1000, tz=today.tzinfo)
            return (d.month, d.day) == today_md

        return _on_this_day, True

    return None, False
