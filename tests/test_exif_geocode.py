"""Tests for the EXIF reader, GPS conversion, geocode cache key, and
Nominatim lookup helpers added in the local-folder enrichment pipeline.

These exercise the module-level helpers in
``custom_components.album_slideshow.coordinator`` without touching Home
Assistant: ``async_timeout`` is replaced with a no-op stub before the
coordinator module is imported, and aiohttp sessions are mocked via small
fake classes that mimic the bits of the API the code uses.
"""
from __future__ import annotations

import asyncio
import io
import sys
import types
from pathlib import Path

import pytest
from PIL import Image


# ── async_timeout shim ─────────────────────────────────────────────────────
# conftest.py installs an empty module stub; replace it with one whose
# ``timeout()`` returns a working async context manager so ``_nominatim_lookup``
# can run under asyncio.run() in tests.

class _NoopTimeout:
    def __init__(self, _seconds: float) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


_fake_async_timeout = types.ModuleType("async_timeout")
_fake_async_timeout.timeout = _NoopTimeout  # type: ignore[attr-defined]
sys.modules["async_timeout"] = _fake_async_timeout


from custom_components.album_slideshow import coordinator as coord  # noqa: E402

# If conftest installed an earlier empty stub for ``async_timeout`` the coord
# module may already have a reference to it; rebind to the working shim so
# ``async with async_timeout.timeout(...)`` works inside ``_nominatim_lookup``.
coord.async_timeout = _fake_async_timeout  # type: ignore[assignment]


# ── _gps_to_decimal ────────────────────────────────────────────────────────
# Real EXIF reads return ``IFDRational`` objects (which respond to ``float``);
# the helper uses ``float(x)`` per element, so feeding plain floats here is
# semantically equivalent and avoids depending on Pillow internals.

def test_gps_to_decimal_north_positive():
    # 47° 37' 0" N  →  +47.616667 (rounded to 6 dp)
    assert coord._gps_to_decimal((47.0, 37.0, 0.0), "N") == pytest.approx(47.616667, abs=1e-6)


def test_gps_to_decimal_south_negative():
    assert coord._gps_to_decimal((33.0, 52.0, 0.0), "S") == pytest.approx(-33.866667, abs=1e-6)


def test_gps_to_decimal_east_positive():
    assert coord._gps_to_decimal((151.0, 12.0, 30.0), "E") == pytest.approx(151.208333, abs=1e-6)


def test_gps_to_decimal_west_negative():
    assert coord._gps_to_decimal((122.0, 20.0, 0.0), "W") == pytest.approx(-122.333333, abs=1e-6)


def test_gps_to_decimal_accepts_ifdrational_like():
    # Simulate Pillow's IFDRational with a minimal stand-in that exposes
    # ``__float__`` - this is the shape the production code actually sees.
    class _Rat:
        def __init__(self, num: int, den: int) -> None:
            self._n, self._d = num, den

        def __float__(self) -> float:
            return self._n / self._d

    assert coord._gps_to_decimal((_Rat(47, 1), _Rat(37, 1), _Rat(0, 1)), "N") == pytest.approx(
        47.616667, abs=1e-6
    )


def test_gps_to_decimal_none_inputs():
    assert coord._gps_to_decimal(None, "N") is None
    assert coord._gps_to_decimal(((1, 1), (2, 1), (3, 1)), None) is None
    assert coord._gps_to_decimal((), "N") is None


def test_gps_to_decimal_bad_inputs():
    # Two-element tuple should fall into the except branch and return None.
    assert coord._gps_to_decimal((1.0, 2.0), "N") is None
    assert coord._gps_to_decimal("not-a-tuple", "N") is None
    # Raw rational tuples are NOT supported (the helper relies on float()
    # on each element, which Pillow's IFDRational implements). Document
    # that behaviour with an explicit test so we notice if it changes.
    assert coord._gps_to_decimal(((1, 1), (2, 1), (3, 1)), "N") is None


# ── _geocode_cache_key ─────────────────────────────────────────────────────

def test_geocode_cache_key_normalises_negative_zero():
    # round(-0.0001, 3) gives -0.0; the helper must collapse that to 0.0 so
    # we don't end up with both "0.0,0.0" and "-0.0,-0.0" pointing at the same
    # tile.
    assert coord._geocode_cache_key(-0.0001, -0.0001) == coord._geocode_cache_key(0.0001, 0.0001)


def test_geocode_cache_key_groups_nearby_points():
    # Both points round to the same tile at 3 dp (~100 m).
    assert coord._geocode_cache_key(47.6166, -122.3331) == coord._geocode_cache_key(47.6168, -122.3333)


def test_geocode_cache_key_distinct_for_far_points():
    assert coord._geocode_cache_key(47.6, -122.3) != coord._geocode_cache_key(-33.9, 151.2)


# ── _read_local_exif ───────────────────────────────────────────────────────

def _write_jpeg(path: Path, *, exif: Image.Exif | None = None) -> None:
    img = Image.new("RGB", (8, 8), color=(123, 200, 50))
    if exif is None:
        img.save(path, format="JPEG")
    else:
        img.save(path, format="JPEG", exif=exif.tobytes())


def _build_exif(*, date: str | None = None, gps: tuple[float, float] | None = None) -> Image.Exif:
    exif = Image.Exif()
    if date is not None:
        exif[36867] = date  # DateTimeOriginal
    if gps is not None:
        lat, lon = gps
        gps_ifd = exif.get_ifd(34853)
        # Pillow's TIFF writer expects float-like RATIONAL values for the
        # GPS DMS entries; plain floats round-trip correctly.
        gps_ifd[1] = "N" if lat >= 0 else "S"
        deg = float(int(abs(lat)))
        minutes = float(int((abs(lat) - deg) * 60))
        gps_ifd[2] = (deg, minutes, 0.0)
        gps_ifd[3] = "E" if lon >= 0 else "W"
        deg = float(int(abs(lon)))
        minutes = float(int((abs(lon) - deg) * 60))
        gps_ifd[4] = (deg, minutes, 0.0)
    return exif


def test_read_local_exif_unsupported_extension(tmp_path: Path):
    p = tmp_path / "note.txt"
    p.write_text("hello", encoding="utf-8")
    assert coord._read_local_exif(p) == (None, None, None)


def test_read_local_exif_video_extension_is_skipped(tmp_path: Path):
    # .mp4 isn't in _IMAGE_EXTS so the helper must short-circuit.
    p = tmp_path / "clip.mp4"
    p.write_bytes(b"not really an mp4")
    assert coord._read_local_exif(p) == (None, None, None)


def test_read_local_exif_jpeg_no_exif(tmp_path: Path):
    p = tmp_path / "no_exif.jpg"
    _write_jpeg(p)
    captured_at, lat, lon = coord._read_local_exif(p)
    assert captured_at is None
    assert lat is None
    assert lon is None


def test_read_local_exif_jpeg_with_date(tmp_path: Path):
    p = tmp_path / "dated.jpg"
    _write_jpeg(p, exif=_build_exif(date="2024:06:15 12:30:00"))
    captured_at, lat, lon = coord._read_local_exif(p)
    assert isinstance(captured_at, int)
    # Whatever timezone interpretation the code uses, the result must be in
    # epoch milliseconds and within +/-24h of midnight UTC on the same date.
    one_day_ms = 24 * 60 * 60 * 1000
    target = 1718454600000  # 2024-06-15T12:30:00Z
    assert abs(captured_at - target) <= one_day_ms
    assert lat is None
    assert lon is None


def test_read_local_exif_jpeg_with_gps(tmp_path: Path):
    p = tmp_path / "geo.jpg"
    _write_jpeg(p, exif=_build_exif(gps=(47.6, -122.3)))
    _, lat, lon = coord._read_local_exif(p)
    # We can't rely on exact equality through PIL's GPS round-trip, but the
    # signs and magnitudes must be right.
    assert lat is not None and lon is not None
    assert lat > 0 and lon < 0
    assert 46 <= lat <= 49
    assert -124 <= lon <= -121


def test_read_local_exif_corrupt_jpeg_returns_none(tmp_path: Path):
    p = tmp_path / "broken.jpg"
    p.write_bytes(b"\xff\xd8not a real jpeg")
    assert coord._read_local_exif(p) == (None, None, None)


def test_read_local_exif_png_without_exif_returns_none(tmp_path: Path):
    p = tmp_path / "shot.png"
    img = Image.new("RGB", (8, 8), color=(0, 128, 255))
    img.save(p, format="PNG")
    # PNGs without an eXIf chunk should yield all None - extension is allowed
    # now, but there's simply nothing to read.
    assert coord._read_local_exif(p) == (None, None, None)


# ── _nominatim_lookup ──────────────────────────────────────────────────────

class _FakeResp:
    def __init__(self, status: int, body: dict | None) -> None:
        self.status = status
        self._body = body or {}

    async def json(self, content_type=None):
        return self._body


class _FakeSession:
    def __init__(self, resp: _FakeResp) -> None:
        self._resp = resp
        self.last_url: str | None = None
        self.last_headers: dict | None = None

    async def get(self, url, headers=None):
        self.last_url = url
        self.last_headers = headers
        return self._resp


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_nominatim_lookup_formats_city_state_country():
    body = {"address": {"city": "Sydney", "state": "New South Wales", "country": "Australia"}}
    sess = _FakeSession(_FakeResp(200, body))
    result = _run(coord._nominatim_lookup(sess, -33.87, 151.21))
    assert result == "Sydney, New South Wales, Australia"
    # Sanity: the call must include the App's User-Agent so OSM can identify us.
    assert "AlbumSlideshow/" in (sess.last_headers or {}).get("User-Agent", "")


def test_nominatim_lookup_falls_back_through_locality_keys():
    # Rural coordinates often have no city/town/village - the helper has to
    # fall back to municipality / county.
    body = {"address": {"municipality": "Tiny Hamlet", "country": "Iceland"}}
    sess = _FakeSession(_FakeResp(200, body))
    assert _run(coord._nominatim_lookup(sess, 64.0, -22.0)) == "Tiny Hamlet, Iceland"


def test_nominatim_lookup_returns_none_for_non_200():
    sess = _FakeSession(_FakeResp(429, None))
    assert _run(coord._nominatim_lookup(sess, 1.0, 2.0)) is None


def test_nominatim_lookup_returns_none_for_empty_address():
    sess = _FakeSession(_FakeResp(200, {"address": {}}))
    assert _run(coord._nominatim_lookup(sess, 1.0, 2.0)) is None


# ── geocode-cache poisoning regression ─────────────────────────────────────
# Direct end-to-end behaviour test for the "don't cache None" guard added on
# top of PR #11. Uses a tiny stand-in coordinator that owns just the bits
# ``_geocode_items_background`` touches, so we can exercise the loop without
# spinning up a real DataUpdateCoordinator.

class _StubStore:
    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def async_load(self):
        return None

    async def async_save(self, data):
        self.saved.append(dict(data))


class _StubCoordinator:
    """Just enough surface for ``_geocode_items_background`` to run."""

    def __init__(self, lookup_results: list[str | None]) -> None:
        self._lookup_results = list(lookup_results)
        self._geocode_cache: dict[str, str | None] = {}
        self._geocode_cache_store = _StubStore()
        self.geocode_done = 0
        self.geocode_total = 0
        self.geocode_complete = False
        self.data: dict | None = {"items": []}
        self.hass = types.SimpleNamespace()  # unused
        self.updated = 0

    def async_set_updated_data(self, _data):
        self.updated += 1


async def _run_geocode_with_results(coord_mod, items, results):
    """Drive the real ``_geocode_items_background`` against a stub coordinator,
    patching ``_nominatim_lookup`` and ``asyncio.sleep`` to keep tests fast."""
    stub = _StubCoordinator(results)
    real_lookup = coord_mod._nominatim_lookup
    real_get_session = coord_mod.async_get_clientsession
    real_sleep = coord_mod.asyncio.sleep
    seq = iter(results)

    async def fake_lookup(_session, _lat, _lon):
        return next(seq)

    async def fast_sleep(_t):
        return None

    coord_mod._nominatim_lookup = fake_lookup  # type: ignore[assignment]
    coord_mod.async_get_clientsession = lambda _hass: None  # type: ignore[assignment]
    coord_mod.asyncio.sleep = fast_sleep  # type: ignore[assignment]
    try:
        await coord_mod.AlbumCoordinator._geocode_items_background(stub, items)
    finally:
        coord_mod._nominatim_lookup = real_lookup
        coord_mod.async_get_clientsession = real_get_session
        coord_mod.asyncio.sleep = real_sleep
    return stub


def test_geocode_does_not_cache_none_results():
    items = [
        coord.MediaItem(
            url="file:///x.jpg",
            width=None, height=None, mime_type=None, filename="x.jpg",
            latitude=10.0, longitude=20.0,
        ),
        coord.MediaItem(
            url="file:///y.jpg",
            width=None, height=None, mime_type=None, filename="y.jpg",
            latitude=30.0, longitude=40.0,
        ),
    ]
    stub = _run(_run_geocode_with_results(coord, items, ["Found, Place", None]))

    # First lookup succeeded - its key must be cached. Second returned None -
    # its key must NOT be in the cache, so a future restart can retry.
    key1 = coord._geocode_cache_key(10.0, 20.0)
    key2 = coord._geocode_cache_key(30.0, 40.0)
    assert stub._geocode_cache.get(key1) == "Found, Place"
    assert key2 not in stub._geocode_cache

    # The MediaItem still reflects the failed lookup as ``None`` so the
    # camera attributes don't lie.
    assert items[0].location == "Found, Place"
    assert items[1].location is None


def test_geocode_uses_cache_hit_without_calling_lookup():
    items = [
        coord.MediaItem(
            url="file:///a.jpg",
            width=None, height=None, mime_type=None, filename="a.jpg",
            latitude=5.0, longitude=5.0,
        )
    ]
    # Pre-populate cache; pass an empty results list so any actual call would
    # raise StopIteration.
    pre_key = coord._geocode_cache_key(5.0, 5.0)
    pre_cache = {pre_key: "Cached, Place"}

    async def _drive():
        stub = _StubCoordinator([])
        stub._geocode_cache = dict(pre_cache)
        real_get_session = coord.async_get_clientsession
        coord.async_get_clientsession = lambda _hass: None  # type: ignore[assignment]
        try:
            await coord.AlbumCoordinator._geocode_items_background(stub, items)
        finally:
            coord.async_get_clientsession = real_get_session
        return stub

    stub = _run(_drive())
    assert items[0].location == "Cached, Place"
    assert stub.geocode_complete is True
    assert stub.geocode_total == 1
    assert stub.geocode_done == 1
