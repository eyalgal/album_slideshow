"""Mock tests for the iCloud public shared-album scraper.

These tests exercise the parsing + multi-step request flow against
fixture JSON modelled on Apple's actual webstream/webasseturls responses.
No live network calls.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from custom_components.album_slideshow import icloud_scraper as ic


# -- parse_share_url --------------------------------------------------------


def test_parse_share_url_fragment():
    assert (
        ic.parse_share_url("https://www.icloud.com/sharedalbum/#B0XXXX1234")
        == "B0XXXX1234"
    )


def test_parse_share_url_with_locale():
    assert (
        ic.parse_share_url("https://www.icloud.com/sharedalbum/en-us/#B0Token99")
        == "B0Token99"
    )


def test_parse_share_url_rejects_non_icloud():
    assert ic.parse_share_url("https://photos.app.goo.gl/abcd") is None


def test_parse_share_url_returns_none_when_no_token():
    assert ic.parse_share_url("https://www.icloud.com/sharedalbum/") is None


# -- Fake aiohttp double ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        try:
            return json.dumps(self._payload)
        except Exception:
            return ""


class _FakeSession:
    def __init__(self) -> None:
        # url-substring -> handler(body) -> _FakeResponse
        self._handlers: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []

    def on(self, url_substring: str, handler) -> None:
        self._handlers[url_substring] = handler

    def post(self, url, json=None, headers=None, **kwargs):  # noqa: A002
        self.calls.append({"url": url, "json": json})
        for sub, handler in self._handlers.items():
            if sub in url:
                return handler(url, json or {})
        raise AssertionError(f"No handler for POST {url}")


# -- Fixture builders -------------------------------------------------------


def _photo(
    guid: str,
    *,
    derivatives: dict[str, dict[str, Any]],
    media_type: str = "image",
    date: str | None = "2024-09-15T13:42:09Z",
) -> dict[str, Any]:
    return {
        "photoGuid": guid,
        "mediaAssetType": media_type,
        "dateCreated": date,
        "derivatives": derivatives,
    }


def _webstream_payload(photos: list[dict[str, Any]], stream_name: str = "My Album") -> dict[str, Any]:
    return {"streamName": stream_name, "photos": photos}


def _webasseturls_payload(checksum_to_loc: dict[str, tuple[str, str]]) -> dict[str, Any]:
    return {
        "items": {
            chk: {"url_location": loc, "url_path": path}
            for chk, (loc, path) in checksum_to_loc.items()
        }
    }


# -- fetch_album end-to-end -------------------------------------------------


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_fetch_album_picks_largest_derivative_per_photo():
    session = _FakeSession()
    photos = [
        _photo(
            "GUID-1",
            derivatives={
                "405": {"checksum": "small1", "width": 405, "height": 270, "fileSize": 50_000},
                "1200": {"checksum": "med1", "width": 1200, "height": 800, "fileSize": 250_000},
                "2880": {"checksum": "big1", "width": 2880, "height": 1920, "fileSize": 900_000},
            },
        ),
        _photo(
            "GUID-2",
            derivatives={
                "1200": {"checksum": "med2", "width": 1200, "height": 800, "fileSize": 220_000},
                "2880": {"checksum": "big2", "width": 2880, "height": 1920, "fileSize": 880_000},
            },
        ),
    ]

    session.on(
        "/webstream",
        lambda url, body: _FakeResponse(200, _webstream_payload(photos)),
    )
    session.on(
        "/webasseturls",
        lambda url, body: _FakeResponse(
            200,
            _webasseturls_payload(
                {
                    "big1": ("cvws.icloud-content.com", "/Bx/big1?o=foo"),
                    "big2": ("cvws.icloud-content.com", "/Bx/big2?o=bar"),
                }
            ),
        ),
    )

    title, items = _run(
        ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#B0AAA1111")
    )
    assert title == "My Album"
    assert len(items) == 2
    urls = sorted(it.url for it in items)
    assert urls == [
        "https://cvws.icloud-content.com/Bx/big1?o=foo",
        "https://cvws.icloud-content.com/Bx/big2?o=bar",
    ]
    # Largest derivative dimensions are propagated.
    assert all(it.width == 2880 and it.height == 1920 for it in items)
    # Apple ISO timestamp parsed to epoch ms.
    assert all(it.captured_at and it.captured_at > 1_000_000_000_000 for it in items)


def test_fetch_album_skips_videos_and_live_photos():
    session = _FakeSession()
    photos = [
        _photo(
            "G1",
            derivatives={"1": {"checksum": "c1", "width": 1, "height": 1, "fileSize": 1}},
            media_type="image",
        ),
        _photo(
            "G2",
            derivatives={"2": {"checksum": "c2", "width": 1, "height": 1, "fileSize": 1}},
            media_type="video",
        ),
    ]
    session.on("/webstream", lambda url, body: _FakeResponse(200, _webstream_payload(photos)))
    session.on(
        "/webasseturls",
        lambda url, body: _FakeResponse(
            200, _webasseturls_payload({"c1": ("h", "/p?x")})
        ),
    )

    _, items = _run(
        ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#B0AAA1111")
    )
    assert [it.url for it in items] == ["https://h/p?x"]


def test_fetch_album_follows_xapple_mme_host_redirect():
    session = _FakeSession()
    photos = [
        _photo(
            "G1",
            derivatives={"1": {"checksum": "c1", "width": 1, "height": 1, "fileSize": 1}},
        ),
    ]

    call_count = {"webstream": 0}

    def webstream_handler(url, body):
        call_count["webstream"] += 1
        if "p23-sharedstreams" in url:
            # First call: tell client to retry on a different partition.
            payload = {"X-Apple-MMe-Host": "p52-sharedstreams.icloud.com", "photos": []}
        else:
            payload = _webstream_payload(photos)
        return _FakeResponse(200, payload)

    session.on("/webstream", webstream_handler)
    session.on(
        "/webasseturls",
        lambda url, body: _FakeResponse(
            200, _webasseturls_payload({"c1": ("h", "/p")})
        ),
    )

    title, items = _run(
        ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#B0AAA1111")
    )
    assert call_count["webstream"] == 2
    assert len(items) == 1
    # Both calls hit the right token.
    assert all("B0AAA1111" in c["url"] for c in session.calls)
    # Second call went to p52, not p23.
    second = session.calls[1]["url"]
    assert "p52-sharedstreams" in second


def test_fetch_album_raises_invalid_token_on_404():
    session = _FakeSession()
    session.on("/webstream", lambda url, body: _FakeResponse(404, {"error": "not found"}))
    with pytest.raises(ic.ICloudInvalidToken):
        _run(ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#bogus"))


def test_fetch_album_returns_empty_when_album_is_empty():
    session = _FakeSession()
    session.on(
        "/webstream",
        lambda url, body: _FakeResponse(200, {"streamName": "Empty", "photos": []}),
    )
    title, items = _run(
        ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#B0Empty00")
    )
    assert title == "Empty"
    assert items == []


def test_fetch_album_batches_webasseturls_when_many_photos():
    session = _FakeSession()
    photos = [
        _photo(
            f"G{i}",
            derivatives={"1": {"checksum": f"c{i}", "width": 100, "height": 100, "fileSize": 1}},
        )
        for i in range(60)
    ]
    session.on("/webstream", lambda url, body: _FakeResponse(200, _webstream_payload(photos)))

    asseturls_call_count = {"n": 0, "guids_seen": []}

    def asseturls_handler(url, body):
        asseturls_call_count["n"] += 1
        guids = body.get("photoGuids") or []
        asseturls_call_count["guids_seen"].extend(guids)
        return _FakeResponse(
            200,
            _webasseturls_payload(
                {f"c{int(g[1:])}": ("h", f"/{g}") for g in guids}
            ),
        )

    session.on("/webasseturls", asseturls_handler)

    _, items = _run(
        ic.fetch_album(session, "https://www.icloud.com/sharedalbum/#B0BatchOK")
    )
    assert len(items) == 60
    # 60 photos, default batch size 25 => 3 webasseturls calls.
    assert asseturls_call_count["n"] == 3
    assert sorted(asseturls_call_count["guids_seen"]) == sorted(p["photoGuid"] for p in photos)
