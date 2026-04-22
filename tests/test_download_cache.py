from __future__ import annotations

import pytest

# _DownloadCache is a module-level helper in camera.py. We import it
# directly to test eviction logic without HA.
from custom_components.album_slideshow.camera import _DownloadCache


def test_put_and_get():
    cache = _DownloadCache(max_bytes=10_000)
    cache.put("http://a", b"hello")
    assert cache.get("http://a") == b"hello"


def test_get_missing_returns_none():
    cache = _DownloadCache(max_bytes=10_000)
    assert cache.get("http://missing") is None


def test_evicts_oldest_when_over_budget():
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://a", b"12345")   # 5 bytes
    cache.put("http://b", b"67890")   # 5 bytes — now at limit
    cache.put("http://c", b"ABCDE")   # 5 bytes — pushes over; evict oldest
    assert cache.get("http://a") is None   # evicted
    assert cache.get("http://b") is not None
    assert cache.get("http://c") is not None


def test_overwrite_updates_byte_count():
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://a", b"12345")   # 5 bytes
    cache.put("http://a", b"XY")      # overwrite with 2 bytes
    assert cache.get("http://a") == b"XY"
    # Total is now 2 bytes; adding 9 more pushes to 11 — evict oldest (a, inserted first)
    cache.put("http://b", b"123456789")  # 9 bytes → total 11, evict a
    assert cache.get("http://a") is None
    assert cache.get("http://b") is not None


def test_resize_evicts_to_fit():
    cache = _DownloadCache(max_bytes=20)
    cache.put("http://a", b"1234567890")  # 10 bytes
    cache.put("http://b", b"1234567890")  # 10 bytes — at limit
    cache.resize(max_bytes=10)             # shrink; a should be evicted
    assert cache.get("http://a") is None
    assert cache.get("http://b") is not None


def test_total_bytes_tracked_correctly():
    cache = _DownloadCache(max_bytes=100)
    cache.put("http://a", b"hello")
    cache.put("http://b", b"world!")
    assert cache.total_bytes == 11


def test_get_moves_entry_to_most_recent():
    # Under the OrderedDict LRU, get() should refresh recency so the next
    # eviction removes the *other* entry, not the just-accessed one.
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://a", b"12345")  # 5 bytes
    cache.put("http://b", b"67890")  # 5 bytes, at limit

    # Touch "a" so it becomes most-recent.
    assert cache.get("http://a") == b"12345"

    cache.put("http://c", b"ABCDE")  # 5 bytes, pushes over
    assert cache.get("http://b") is None  # b was LRU, evicted
    assert cache.get("http://a") == b"12345"
    assert cache.get("http://c") == b"ABCDE"


def test_item_larger_than_budget_is_not_cached():
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://big", b"x" * 20)  # exceeds entire budget
    assert cache.get("http://big") is None
    assert cache.total_bytes == 0


def test_resize_zero_or_negative_clamps_to_one():
    cache = _DownloadCache(max_bytes=100)
    cache.put("http://a", b"hi")
    cache.resize(0)
    # After resize, total must be <= 1 byte; entry must have been evicted.
    assert cache.total_bytes == 0
    assert cache.get("http://a") is None
