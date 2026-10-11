"""Video slides: playback timing and an authenticated streaming proxy.

The camera entity only ever serves still JPEGs, so a video slide is rendered
as its poster frame and the dashboard card plays the clip on top of it. The
browser can't fetch the clip from the provider directly (Immich needs an
``x-api-key`` header we must not leak), so this view streams it through Home
Assistant. ``Range`` requests are forwarded: Safari/iOS refuse to play video
without them, and seeking/looping depend on them everywhere.

The ``<video>`` element can't send a bearer token either, so the camera hands
the card a URL signed with ``authSig`` (the same mechanism Home Assistant uses
for media-source and Cast URLs). Only videos in the entry's current playlist
are served; the view is not a general-purpose proxy.
"""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from aiohttp import ClientError, ClientPayloadError, ClientTimeout, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN, MIN_VIDEO_HOLD_SECONDS

_LOGGER = logging.getLogger(__name__)

VIDEO_URL_PREFIX = "/api/album_slideshow/video"

# The card starts playback only after the frame is committed and the clip has
# buffered, so give the slide a little longer than the clip itself.
_START_GRACE_SECONDS = 1.0
# Long enough to cover a paused slideshow sitting on a looping clip.
_SIGNATURE_LIFETIME = timedelta(hours=24)
_CHUNK_SIZE = 64 * 1024
_FORWARD_REQUEST_HEADERS = ("Range", "If-Range")
_FORWARD_RESPONSE_HEADERS = (
    "Content-Type",
    "Content-Length",
    "Content-Range",
    "Accept-Ranges",
    "ETag",
    "Last-Modified",
)
_ACCEPTED_CONTENT_TYPES = ("video/", "application/octet-stream")


def video_hold_seconds(
    duration_ms: int | None, max_seconds: int, fallback_seconds: int
) -> float:
    """How long a video slide stays up.

    The clip plays once (plus a short start-up grace), never shorter than
    ``MIN_VIDEO_HOLD_SECONDS`` and never longer than ``max_seconds``. A clip
    of unknown length falls back to the normal slide interval.
    """
    if duration_ms is None or duration_ms <= 0:
        return float(min(fallback_seconds, max_seconds))
    held = max(MIN_VIDEO_HOLD_SECONDS, duration_ms / 1000 + _START_GRACE_SECONDS)
    return float(min(max_seconds, held))


def video_path(entry_id: str, photo_id: str) -> str:
    return f"{VIDEO_URL_PREFIX}/{entry_id}/{photo_id}"


def sign_video_path(hass: Any, path: str) -> str:
    """Return ``path`` with an ``authSig`` so a plain ``<video src>`` works."""
    try:
        from homeassistant.components.http.auth import async_sign_path
    except ImportError:  # pragma: no cover - http always present
        return path
    for kwargs in ({"use_content_user": True}, {}):
        try:
            return async_sign_path(hass, path, _SIGNATURE_LIFETIME, **kwargs)
        except TypeError:
            # Core without ``use_content_user``.
            continue
        except Exception as err:  # noqa: BLE001 - never break the slide itself
            # Unsigned, the card's request is refused and the poster stays up.
            _LOGGER.debug("Album Slideshow: failed to sign %s: %s", path, err)
            return path
    return path


def find_video_item(coordinator: Any, photo_id: str) -> Any | None:
    """Return the playlist's video item with ``photo_id``, if any."""
    data = getattr(coordinator, "data", None) or {}
    for item in data.get("items") or []:
        if getattr(item, "photo_id", None) == photo_id and getattr(item, "video_url", None):
            return item
    return None


def upstream_request_headers(coordinator: Any, request_headers: Any) -> dict[str, str]:
    """Provider auth headers plus the browser's range headers."""
    headers = dict(getattr(coordinator, "image_request_headers", None) or {})
    for name in _FORWARD_REQUEST_HEADERS:
        value = request_headers.get(name)
        if value:
            headers[name] = value
    return headers


class AlbumSlideshowVideoView(HomeAssistantView):
    """Stream a slideshow video from its provider to the dashboard card."""

    url = VIDEO_URL_PREFIX + "/{entry_id}/{photo_id}"
    name = "api:album_slideshow:video"
    requires_auth = True

    def __init__(self, hass: Any) -> None:
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, photo_id: str
    ) -> web.StreamResponse:
        entry_data = self.hass.data.get(DOMAIN, {}).get(entry_id)
        coordinator = entry_data.get("coordinator") if isinstance(entry_data, dict) else None
        item = find_video_item(coordinator, photo_id) if coordinator else None
        if item is None:
            return web.Response(status=404)

        session = async_get_clientsession(
            self.hass,
            verify_ssl=getattr(coordinator, "image_request_verify_ssl", True),
        )
        try:
            upstream = await session.get(
                item.video_url,
                headers=upstream_request_headers(coordinator, request.headers),
                params=getattr(coordinator, "image_request_params", None) or None,
                # No total timeout: a long clip can legitimately stream for minutes.
                timeout=ClientTimeout(total=None, sock_connect=15, sock_read=60),
            )
        except (ClientError, TimeoutError) as err:
            _LOGGER.debug("Album Slideshow: video upstream failed for %s: %s", photo_id, err)
            return web.Response(status=502)

        async with upstream:
            if upstream.status == 416:
                return web.Response(
                    status=416,
                    headers={
                        name: upstream.headers[name]
                        for name in ("Content-Range",)
                        if name in upstream.headers
                    },
                )
            if upstream.status not in (200, 206):
                _LOGGER.debug(
                    "Album Slideshow: video upstream returned HTTP %s for %s",
                    upstream.status, photo_id,
                )
                return web.Response(status=502)
            content_type = upstream.headers.get("Content-Type", "").lower()
            if not content_type.startswith(_ACCEPTED_CONTENT_TYPES):
                _LOGGER.debug(
                    "Album Slideshow: video upstream sent %r for %s", content_type, photo_id
                )
                return web.Response(status=502)

            response = web.StreamResponse(status=upstream.status)
            for name in _FORWARD_RESPONSE_HEADERS:
                if name in upstream.headers:
                    response.headers[name] = upstream.headers[name]
            response.headers["Cache-Control"] = "private, max-age=3600"
            await response.prepare(request)
            try:
                async for chunk in upstream.content.iter_chunked(_CHUNK_SIZE):
                    await response.write(chunk)
            except ConnectionResetError:
                # The browser routinely abandons a range request mid-stream
                # when it seeks or has buffered enough.
                return response
            except (ClientPayloadError, ClientError, TimeoutError) as err:
                _LOGGER.debug("Album Slideshow: video stream for %s ended early: %s", photo_id, err)
                return response
            await response.write_eof()
            return response


def async_register_video_view(hass: Any) -> None:
    """Register the proxy view once per Home Assistant session."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("video_view_registered"):
        return
    hass.http.register_view(AlbumSlideshowVideoView(hass))
    domain_data["video_view_registered"] = True
