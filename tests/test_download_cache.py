from __future__ import annotations

import pytest

# _DownloadCache and _BytesCache are module-level in camera.py.
# We import them directly to test eviction logic without HA.
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
