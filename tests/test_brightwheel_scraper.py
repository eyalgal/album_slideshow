"""Tests for the Brightwheel scraper.

The integration uses an unofficial REST API; tests mock aiohttp responses
so we can validate the parsing layer and auth flow without hitting the
network.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from custom_components.album_slideshow import brightwheel_scraper as bw


# ---------------------------------------------------------------------------
# Tiny async-aiohttp test double.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status: int, payload: Any) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self, content_type=None):
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        try:
            return json.dumps(self._payload)
        except Exception:
            return ""


class _FakeCookieJar:
    def __init__(self) -> None:
        self._cookies: list[dict[str, str]] = []

    def update_cookies(self, cookies: dict[str, str], response_url=None) -> None:
        for k, v in cookies.items():
            self._cookies.append(
                {
                    "name": k,
                    "value": v,
                    "domain": "schools.mybrightwheel.com",
                    "path": "/",
                }
            )

    def filter_cookies(self, url):
        # Mimic aiohttp's API: returns a Morsel-like dict.
        class _M:
            def __init__(self, value: str) -> None:
                self.value = value

        out: dict[str, _M] = {}
        for c in self._cookies:
            out[c["name"]] = _M(c["value"])
        return out

    def __iter__(self):
        for c in self._cookies:
            yield _FakeMorsel(c["name"], c["value"], c["domain"], c["path"])


class _FakeMorsel:
    def __init__(self, key: str, value: str, domain: str, path: str) -> None:
        self.key = key
        self.value = value
        self._meta = {"domain": domain, "path": path}

    def get(self, k: str, default=None):
        return self._meta.get(k, default)


class FakeSession:
    """Records every request and replays scripted responses by URL prefix."""

    def __init__(self) -> None:
        self.cookie_jar = _FakeCookieJar()
        # script: dict[(method, url_substring)] = callable(body) -> _FakeResponse
        self._handlers: dict[tuple[str, str], Any] = {}
        self.calls: list[dict[str, Any]] = []

    def on(self, method: str, url_substring: str, handler) -> None:
        self._handlers[(method.upper(), url_substring)] = handler

    def _resolve(self, method: str, url: str):
        for (m, sub), handler in self._handlers.items():
            if m == method and sub in url:
                return handler
        raise AssertionError(f"No handler for {method} {url}")

    def get(self, url, headers=None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        handler = self._resolve("GET", url)
        return handler(url, kwargs)

    def post(self, url, json=None, headers=None, **kwargs):
        self.calls.append(
            {"method": "POST", "url": url, "headers": headers, "json": json}
        )
        handler = self._resolve("POST", url)
        return handler(url, {"json": json, **kwargs})

    def request(self, method, url, json=None, headers=None, **kwargs):
        self.calls.append(
            {"method": method.upper(), "url": url, "headers": headers, "json": json}
        )
        handler = self._resolve(method.upper(), url)
        return handler(url, {"json": json, **kwargs})


# ---------------------------------------------------------------------------
# _parse_activity
# ---------------------------------------------------------------------------


def test_parse_activity_extracts_image_and_timestamps():
    raw = {
        "object_id": "act-123",
        "action_type": "ac_photo",
        "event_date": "2025-09-12T15:30:00Z",
        "created_at": "2025-09-12T15:31:00Z",
        "media": {
            "image_url": "https://bw-cdn.example/photos/abc.jpg?sig=xyz",
            "width": 1920,
            "height": 1280,
            "file_size": 412300,
        },
    }
    parsed = bw._parse_activity(raw)
    assert parsed is not None
    assert parsed["url"].startswith("https://bw-cdn.example/")
    assert parsed["width"] == 1920 and parsed["height"] == 1280
    assert parsed["captured_at"] == 1757691000000
    assert parsed["uploaded_at"] == 1757691060000
    assert parsed["byte_size"] == 412300
    assert parsed["filename"] == "brightwheel_act-123.jpg"
    assert parsed["mime_type"] == "image/jpeg"


def test_parse_activity_skips_videos():
    raw = {
        "action_type": "ac_video",
        "media": {"image_url": "https://bw-cdn.example/v/thumb.jpg"},
    }
    assert bw._parse_activity(raw) is None


def test_parse_activity_handles_missing_url():
    assert bw._parse_activity({"action_type": "ac_photo", "media": {}}) is None


def test_parse_activity_falls_back_to_top_level_url():
    parsed = bw._parse_activity(
        {
            "action_type": "ac_photo",
            "image_url": "https://bw-cdn.example/p.jpg",
        }
    )
    assert parsed is not None
    assert parsed["url"] == "https://bw-cdn.example/p.jpg"


# ---------------------------------------------------------------------------
# Cookie persistence helpers
# ---------------------------------------------------------------------------


def test_brightwheel_session_round_trips_cookies():
    s = bw.BrightwheelSession(
        csrf_token="abc",
        cookies=[
            {"name": "_brightwheel_v2", "value": "x", "domain": "schools.mybrightwheel.com", "path": "/"},
        ],
    )
    again = bw.BrightwheelSession.from_dict(s.to_dict())
    assert again.csrf_token == "abc"
    assert again.cookies == s.cookies


def test_brightwheel_session_from_dict_handles_garbage():
    assert bw.BrightwheelSession.from_dict(None).csrf_token == ""
    assert bw.BrightwheelSession.from_dict({"cookies": "not a list"}).cookies == []


# ---------------------------------------------------------------------------
# Login flow
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def test_login_first_call_raises_two_factor_required(event_loop):
    session = FakeSession()

    def _post_session(url, kw):
        return _FakeResponse(412, {"error": "otp_required"})

    session.on("POST", "/api/v1/sessions", _post_session)

    with pytest.raises(bw.BrightwheelTwoFactorRequired):
        event_loop.run_until_complete(
            bw.login(session, "guardian@example.com", "hunter2")
        )

    methods = [(c["method"], c["url"]) for c in session.calls]
    assert any(m == "POST" and "/api/v1/sessions" in u for m, u in methods)


def test_login_first_call_with_json_flag_raises_two_factor_required(event_loop):
    session = FakeSession()
    session.on(
        "POST",
        "/api/v1/sessions",
        lambda url, kw: _FakeResponse(200, {"otp_required": True}),
    )
    with pytest.raises(bw.BrightwheelTwoFactorRequired):
        event_loop.run_until_complete(
            bw.login(session, "guardian@example.com", "hunter2")
        )


def test_login_with_code_returns_session(event_loop):
    session = FakeSession()

    def _patch_session(url, kw):
        session.cookie_jar.update_cookies(
            {"_brightwheel_v2": "session-cookie", "csrf-token": "rotated-csrf"}
        )
        return _FakeResponse(200, {"user": {"object_id": "guard-1"}})

    session.on("PATCH", "/api/v1/sessions", _patch_session)

    bw_session = event_loop.run_until_complete(
        bw.login(session, "guardian@example.com", "hunter2", code="123456")
    )
    assert bw_session.csrf_token == "rotated-csrf"
    assert any(c["name"] == "_brightwheel_v2" for c in bw_session.cookies)


def test_login_invalid_credentials_raises_auth_required(event_loop):
    session = FakeSession()
    session.on(
        "POST",
        "/api/v1/sessions",
        lambda url, kw: _FakeResponse(401, {"error": "bad credentials"}),
    )
    with pytest.raises(bw.BrightwheelAuthRequired):
        event_loop.run_until_complete(
            bw.login(session, "guardian@example.com", "wrong")
        )


# ---------------------------------------------------------------------------
# fetch_album end-to-end (with mocked endpoints)
# ---------------------------------------------------------------------------


def test_fetch_album_aggregates_photos_for_all_students(event_loop):
    session = FakeSession()

    session.on(
        "GET",
        "/api/v1/users/me",
        lambda url, kw: _FakeResponse(200, {"object_id": "guard-1"}),
    )
    session.on(
        "GET",
        "/api/v1/guardians/guard-1/students",
        lambda url, kw: _FakeResponse(
            200,
            {
                "students": [
                    {"object_id": "stu-A", "first_name": "Ada", "last_name": "Lovelace"},
                    {"object_id": "stu-B", "first_name": "Bob", "last_name": "Builder"},
                ]
            },
        ),
    )

    page_state: dict[str, int] = {"stu-A": 0, "stu-B": 0}

    def _activities(student_key: str, photos_per_page: int):
        def _handler(url, kw):
            page = page_state[student_key]
            page_state[student_key] = page + 1
            if page == 0:
                return _FakeResponse(
                    200,
                    {
                        "activities": [
                            {
                                "object_id": f"{student_key}-act-{i}",
                                "action_type": "ac_photo",
                                "event_date": "2025-09-01T12:00:00Z",
                                "media": {
                                    "image_url": f"https://bw-cdn.example/{student_key}-{i}.jpg"
                                },
                            }
                            for i in range(photos_per_page)
                        ]
                    },
                )
            return _FakeResponse(200, {"activities": []})

        return _handler

    session.on("GET", "/api/v1/students/stu-A/activities", _activities("stu-A", 3))
    session.on("GET", "/api/v1/students/stu-B/activities", _activities("stu-B", 2))

    bw_session = bw.BrightwheelSession(
        csrf_token="tok",
        cookies=[
            {
                "name": "_brightwheel_v2",
                "value": "x",
                "domain": "schools.mybrightwheel.com",
                "path": "/",
            }
        ],
    )

    title, items, refreshed = event_loop.run_until_complete(
        bw.fetch_album(session, bw_session)
    )

    assert title == "Ada Lovelace, Bob Builder"
    assert len(items) == 5
    # Refreshed session reuses the same csrf and includes restored cookies.
    assert refreshed.csrf_token == "tok"


def test_fetch_album_filters_by_student_ids(event_loop):
    session = FakeSession()
    session.on(
        "GET",
        "/api/v1/users/me",
        lambda url, kw: _FakeResponse(200, {"object_id": "g"}),
    )
    session.on(
        "GET",
        "/api/v1/guardians/g/students",
        lambda url, kw: _FakeResponse(
            200,
            {
                "students": [
                    {"object_id": "stu-A", "first_name": "Ada", "last_name": "L"},
                    {"object_id": "stu-B", "first_name": "Bob", "last_name": "B"},
                ]
            },
        ),
    )
    session.on(
        "GET",
        "/api/v1/students/stu-A/activities",
        lambda url, kw: _FakeResponse(
            200,
            {
                "activities": [
                    {
                        "action_type": "ac_photo",
                        "media": {"image_url": "https://bw-cdn.example/A.jpg"},
                    }
                ]
            },
        ),
    )

    bw_session = bw.BrightwheelSession(
        csrf_token="tok",
        cookies=[
            {
                "name": "_brightwheel_v2",
                "value": "x",
                "domain": "schools.mybrightwheel.com",
                "path": "/",
            }
        ],
    )

    title, items, _ = event_loop.run_until_complete(
        bw.fetch_album(session, bw_session, student_ids=["stu-A"])
    )
    assert title == "Ada L"
    assert len(items) == 1
    # Confirm we never hit stu-B's activities endpoint.
    assert all("stu-B" not in c["url"] for c in session.calls if "/activities" in c["url"])


def test_fetch_album_raises_auth_required_on_401(event_loop):
    session = FakeSession()
    session.on(
        "GET",
        "/api/v1/users/me",
        lambda url, kw: _FakeResponse(401, {"error": "expired"}),
    )
    bw_session = bw.BrightwheelSession(
        csrf_token="tok",
        cookies=[
            {
                "name": "_brightwheel_v2",
                "value": "x",
                "domain": "schools.mybrightwheel.com",
                "path": "/",
            }
        ],
    )
    with pytest.raises(bw.BrightwheelAuthRequired):
        event_loop.run_until_complete(bw.fetch_album(session, bw_session))


def test_fetch_album_requires_initialised_session(event_loop):
    session = FakeSession()
    bw_session = bw.BrightwheelSession()
    with pytest.raises(bw.BrightwheelAuthRequired):
        event_loop.run_until_complete(bw.fetch_album(session, bw_session))


def test_fetch_album_dedupes_urls(event_loop):
    session = FakeSession()
    session.on(
        "GET",
        "/api/v1/users/me",
        lambda url, kw: _FakeResponse(200, {"object_id": "g"}),
    )
    session.on(
        "GET",
        "/api/v1/guardians/g/students",
        lambda url, kw: _FakeResponse(
            200,
            {
                "students": [
                    {"object_id": "stu-A", "first_name": "Ada", "last_name": "L"},
                ]
            },
        ),
    )
    session.on(
        "GET",
        "/api/v1/students/stu-A/activities",
        lambda url, kw: _FakeResponse(
            200,
            {
                "activities": [
                    {
                        "action_type": "ac_photo",
                        "media": {"image_url": "https://bw-cdn.example/dup.jpg"},
                    },
                    {
                        "action_type": "ac_photo",
                        "media": {"image_url": "https://bw-cdn.example/dup.jpg"},
                    },
                ]
            },
        ),
    )
    bw_session = bw.BrightwheelSession(
        csrf_token="tok",
        cookies=[
            {
                "name": "_brightwheel_v2",
                "value": "x",
                "domain": "schools.mybrightwheel.com",
                "path": "/",
            }
        ],
    )
    _, items, _ = event_loop.run_until_complete(
        bw.fetch_album(session, bw_session, student_ids=["stu-A"])
    )
    assert len(items) == 1
