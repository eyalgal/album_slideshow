"""Brightwheel media scraper.

Brightwheel does not publish a public API. This module talks to the
internal REST API used by the web/mobile app at
``schools.mybrightwheel.com/api/v1/...``.

Auth flow (reverse-engineered, may change without notice):

1. ``GET  /api/sessions/start``   - sets a CSRF cookie, returns CSRF token
2. ``POST /api/v1/sessions/start`` with email+password
   - 200 with ``2fa_code_required: true``  -> Brightwheel emails a 6-digit
     code; user enters it in the config flow
   - 200 with session info                 -> already trusted (rare)
3. ``POST /api/v1/sessions``      with email+password+code
   -> sets ``_brightwheel_v2`` session cookie

Once authenticated:

* ``GET /api/v1/users/me``                              -> guardian id
* ``GET /api/v1/guardians/{id}/students``               -> student ids
* ``GET /api/v1/students/{id}/activities?action_type=ac_photo,ac_video``
                                                        -> paginated
                                                           activities

Activity payloads expose ``media.image_url`` / ``video_url`` which are
signed S3 URLs that expire after a few hours. The coordinator therefore
re-fetches the metadata more aggressively than for static albums.

A :class:`BrightwheelAuthRequired` exception is raised whenever the API
returns 401/403; the integration surfaces that as a HA reauth flow so
the user can re-enter the 2FA code.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import aiohttp
import async_timeout

from .const import (
    BRIGHTWHEEL_BASE,
    BRIGHTWHEEL_CLIENT_NAME,
    BRIGHTWHEEL_CLIENT_VERSION,
    BRIGHTWHEEL_DEFAULT_PAGE_SIZE,
)

_LOGGER = logging.getLogger(__name__)

_LOGIN_TIMEOUT = 30
_FETCH_TIMEOUT = 60


class BrightwheelError(Exception):
    """Base class for Brightwheel API errors."""


class BrightwheelAuthRequired(BrightwheelError):
    """Authentication is missing or has expired (401/403)."""


class BrightwheelTwoFactorRequired(BrightwheelError):
    """Login succeeded but a 2FA code is needed before issuing a session."""


@dataclass
class BrightwheelSession:
    """Persistable session state.

    Stored in the config entry options; carries the cookie jar contents
    plus the CSRF token. Cookies are stored as a list of (name, value,
    domain, path) tuples so that the cookie jar can be rebuilt across
    Home Assistant restarts.
    """

    csrf_token: str = ""
    cookies: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"csrf_token": self.csrf_token, "cookies": self.cookies}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "BrightwheelSession":
        if not isinstance(data, dict):
            return cls()
        cookies = data.get("cookies") or []
        if not isinstance(cookies, list):
            cookies = []
        return cls(
            csrf_token=str(data.get("csrf_token") or ""),
            cookies=[c for c in cookies if isinstance(c, dict)],
        )


def _default_headers(csrf_token: str | None = None) -> dict[str, str]:
    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "x-client-name": BRIGHTWHEEL_CLIENT_NAME,
        "x-client-version": BRIGHTWHEEL_CLIENT_VERSION,
        "origin": BRIGHTWHEEL_BASE,
        "referer": f"{BRIGHTWHEEL_BASE}/",
    }
    if csrf_token:
        headers["x-csrf-token"] = csrf_token
    return headers


def _serialise_cookies(jar: aiohttp.CookieJar | Any) -> list[dict[str, str]]:
    """Pull cookies out of an aiohttp jar into a JSON-serialisable list."""
    out: list[dict[str, str]] = []
    try:
        for cookie in jar:  # type: ignore[assignment]
            out.append({
                "name": cookie.key,
                "value": cookie.value,
                "domain": cookie.get("domain") or "",
                "path": cookie.get("path") or "/",
            })
    except Exception as err:  # pragma: no cover - defensive
        _LOGGER.debug("Brightwheel: cookie serialise failed: %s", err)
    return out


def _restore_cookies(jar: aiohttp.CookieJar, cookies: list[dict[str, str]]) -> None:
    """Restore previously-serialised cookies into a jar."""
    if not cookies:
        return
    from yarl import URL

    by_url: dict[str, dict[str, str]] = {}
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        domain = (c.get("domain") or "").lstrip(".") or "schools.mybrightwheel.com"
        path = c.get("path") or "/"
        url = URL(f"https://{domain}{path}")
        by_url.setdefault(str(url), {})[name] = value

    for url_str, name_value in by_url.items():
        try:
            jar.update_cookies(name_value, response_url=URL(url_str))
        except Exception as err:  # pragma: no cover - defensive
            _LOGGER.debug("Brightwheel: cookie restore failed for %s: %s", url_str, err)


async def _fetch_csrf(session: aiohttp.ClientSession) -> str:
    """Hit /api/sessions/start to obtain a fresh CSRF token + cookie."""
    url = f"{BRIGHTWHEEL_BASE}/api/sessions/start"
    async with async_timeout.timeout(_LOGIN_TIMEOUT):
        async with session.get(url, headers=_default_headers()) as resp:
            if resp.status >= 400:
                raise BrightwheelError(f"CSRF bootstrap failed: HTTP {resp.status}")
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = {}

    token = ""
    if isinstance(payload, dict):
        token = (
            payload.get("csrf")
            or payload.get("csrf_token")
            or payload.get("authenticity_token")
            or ""
        )
    if not token:
        # Fall back to the cookie value Brightwheel sets on the bootstrap.
        for cookie_name in ("csrf-token", "_brightwheel_csrf"):
            cookie = session.cookie_jar.filter_cookies(BRIGHTWHEEL_BASE).get(cookie_name)
            if cookie:
                token = cookie.value
                break
    if not token:
        raise BrightwheelError("Brightwheel did not return a CSRF token")
    return token


async def login(
    session: aiohttp.ClientSession,
    email: str,
    password: str,
    *,
    code: str | None = None,
) -> BrightwheelSession:
    """Authenticate against Brightwheel.

    Raises :class:`BrightwheelTwoFactorRequired` after the first call when
    a code is needed; the caller should prompt the user and call again
    with ``code`` set.
    """
    csrf_token = await _fetch_csrf(session)

    if code:
        # Final step: include the 2FA code.
        url = f"{BRIGHTWHEEL_BASE}/api/v1/sessions"
        body = {
            "user": {"email": email, "password": password, "2fa_code": code},
        }
    else:
        # First step: validate creds, may trigger a 2FA email.
        url = f"{BRIGHTWHEEL_BASE}/api/v1/sessions/start"
        body = {"user": {"email": email, "password": password}}

    async with async_timeout.timeout(_LOGIN_TIMEOUT):
        async with session.post(url, json=body, headers=_default_headers(csrf_token)) as resp:
            status = resp.status
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                payload = {}

    if status in (401, 403):
        raise BrightwheelAuthRequired(
            f"Brightwheel rejected credentials (HTTP {status})"
        )
    if status >= 400:
        raise BrightwheelError(f"Brightwheel login failed: HTTP {status}")

    if isinstance(payload, dict) and payload.get("2fa_code_required"):
        # No session cookie yet - bubble up so the config flow can prompt.
        raise BrightwheelTwoFactorRequired(
            "Brightwheel emailed a 6-digit verification code"
        )

    # Success: return the cookie jar + csrf for persistence.
    return BrightwheelSession(
        csrf_token=csrf_token,
        cookies=_serialise_cookies(session.cookie_jar),
    )


async def _get_json(
    session: aiohttp.ClientSession,
    url: str,
    csrf_token: str,
) -> Any:
    async with async_timeout.timeout(_FETCH_TIMEOUT):
        async with session.get(url, headers=_default_headers(csrf_token)) as resp:
            if resp.status in (401, 403):
                raise BrightwheelAuthRequired(
                    f"Brightwheel session expired (HTTP {resp.status})"
                )
            if resp.status >= 400:
                raise BrightwheelError(
                    f"Brightwheel API error {resp.status} on {url}"
                )
            return await resp.json(content_type=None)


async def fetch_guardian_id(
    session: aiohttp.ClientSession,
    csrf_token: str,
) -> str:
    payload = await _get_json(
        session, f"{BRIGHTWHEEL_BASE}/api/v1/users/me", csrf_token
    )
    if not isinstance(payload, dict):
        raise BrightwheelError("Unexpected /users/me response shape")
    # Brightwheel nests the guardian under "object_id" or "id" depending on
    # the deployment; try both.
    obj_id = payload.get("object_id") or payload.get("id")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else None
    if not obj_id and user:
        obj_id = user.get("object_id") or user.get("id")
    if not isinstance(obj_id, str) or not obj_id:
        raise BrightwheelError("Could not determine Brightwheel guardian id")
    return obj_id


async def fetch_students(
    session: aiohttp.ClientSession,
    csrf_token: str,
    guardian_id: str,
) -> list[dict[str, Any]]:
    """Return [{id, name}, ...] for all students linked to the guardian."""
    url = f"{BRIGHTWHEEL_BASE}/api/v1/guardians/{quote(guardian_id, safe='')}/students"
    payload = await _get_json(session, url, csrf_token)

    students_raw: list[Any] = []
    if isinstance(payload, list):
        students_raw = payload
    elif isinstance(payload, dict):
        for key in ("students", "data", "results"):
            v = payload.get(key)
            if isinstance(v, list):
                students_raw = v
                break

    out: list[dict[str, Any]] = []
    for raw in students_raw:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("object_id") or raw.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        first = raw.get("first_name") or ""
        last = raw.get("last_name") or ""
        name = (first + " " + last).strip() or raw.get("name") or sid
        out.append({"id": sid, "name": name})
    return out


def _parse_iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _parse_activity(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Normalise a Brightwheel activity into a media-item dict.

    Returns ``None`` for activities that aren't photo/video, lack a usable
    URL, or are explicitly videos (we skip videos like Google Photos).
    """
    if not isinstance(raw, dict):
        return None

    action_type = raw.get("action_type") or raw.get("type") or ""
    if isinstance(action_type, str) and action_type.startswith("ac_video"):
        return None

    media = raw.get("media") if isinstance(raw.get("media"), dict) else raw
    image_url = (
        media.get("image_url")
        or media.get("url")
        or raw.get("image_url")
    )
    if not isinstance(image_url, str) or not image_url.startswith("http"):
        return None

    if media.get("video_url") or raw.get("video_url"):
        # Photo+video combos - keep the still image only.
        pass

    captured_at = _parse_iso_to_ms(
        raw.get("event_date")
        or raw.get("created_at")
        or media.get("created_at")
    )
    uploaded_at = _parse_iso_to_ms(raw.get("created_at") or media.get("created_at"))

    activity_id = raw.get("object_id") or raw.get("id")
    filename = None
    if isinstance(activity_id, str):
        filename = f"brightwheel_{activity_id}.jpg"

    return {
        "url": image_url,
        "width": _safe_int(media.get("width")) or _safe_int(raw.get("image_width")),
        "height": _safe_int(media.get("height")) or _safe_int(raw.get("image_height")),
        "mime_type": "image/jpeg",
        "filename": filename,
        "captured_at": captured_at,
        "uploaded_at": uploaded_at,
        "byte_size": _safe_int(media.get("file_size")) or _safe_int(raw.get("file_size")),
    }


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


async def fetch_activities(
    session: aiohttp.ClientSession,
    csrf_token: str,
    student_id: str,
    *,
    page_size: int = BRIGHTWHEEL_DEFAULT_PAGE_SIZE,
    max_pages: int = 200,
    earliest_event_date: str | None = None,
) -> list[dict[str, Any]]:
    """Walk a student's photo activities; returns raw activity dicts."""
    activities: list[dict[str, Any]] = []
    page = 0
    while page < max_pages:
        params = [
            ("page", str(page)),
            ("page_size", str(page_size)),
            ("action_type", "ac_photo"),
        ]
        if earliest_event_date:
            params.append(("earliest_event_date", earliest_event_date))
        query = "&".join(f"{k}={quote(v, safe='')}" for k, v in params)
        url = (
            f"{BRIGHTWHEEL_BASE}/api/v1/students/{quote(student_id, safe='')}"
            f"/activities?{query}"
        )

        payload = await _get_json(session, url, csrf_token)

        page_items: list[Any] = []
        if isinstance(payload, list):
            page_items = payload
        elif isinstance(payload, dict):
            for key in ("activities", "data", "results"):
                v = payload.get(key)
                if isinstance(v, list):
                    page_items = v
                    break

        if not page_items:
            break

        activities.extend([it for it in page_items if isinstance(it, dict)])

        # Brightwheel returns full pages until it runs out; stop when we
        # see a partial page.
        if len(page_items) < page_size:
            break
        page += 1

    return activities


async def fetch_album(
    session: aiohttp.ClientSession,
    bw_session: BrightwheelSession,
    *,
    student_ids: list[str] | None = None,
    earliest_event_date: str | None = None,
) -> tuple[str | None, list[dict[str, Any]], BrightwheelSession]:
    """Top-level entry point used by the coordinator.

    Returns ``(album_title, items, refreshed_session)`` where ``items`` is
    a list of dicts compatible with :class:`coordinator.MediaItem`. The
    session is returned because the cookie jar may have been rotated.
    """
    if not bw_session.csrf_token or not bw_session.cookies:
        raise BrightwheelAuthRequired("Brightwheel session not initialised")

    _restore_cookies(session.cookie_jar, bw_session.cookies)
    csrf = bw_session.csrf_token

    guardian_id = await fetch_guardian_id(session, csrf)
    students = await fetch_students(session, csrf, guardian_id)
    if not students:
        raise BrightwheelError("Brightwheel returned no students for this guardian")

    if student_ids:
        wanted = set(student_ids)
        students = [s for s in students if s["id"] in wanted]
        if not students:
            raise BrightwheelError("None of the configured student ids were found")

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for student in students:
        raw_activities = await fetch_activities(
            session,
            csrf,
            student["id"],
            earliest_event_date=earliest_event_date,
        )
        for raw in raw_activities:
            parsed = _parse_activity(raw)
            if not parsed:
                continue
            if parsed["url"] in seen_urls:
                continue
            seen_urls.add(parsed["url"])
            items.append(parsed)

    title = ", ".join(s["name"] for s in students) or None

    refreshed = BrightwheelSession(
        csrf_token=csrf,
        cookies=_serialise_cookies(session.cookie_jar),
    )
    return title, items, refreshed
