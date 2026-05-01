"""iCloud public shared-album client.

Apple lets users publish a Photos album as a public web link of the form::

    https://www.icloud.com/sharedalbum/#B0XXXXXXXXXXXX
    https://www.icloud.com/sharedalbum/en-us/#B0XXXXXXXXXXXX

The fragment after ``#`` is the album token. Apple exposes this album to
the web via two undocumented but long-stable JSON endpoints, hosted on
geo-sharded ``p{NN}-sharedstreams.icloud.com`` nodes:

1. ``POST /{token}/sharedstreams/webstream`` with body ``{"streamCtag": null}``
   returns the album metadata: a list of photos (each with a ``photoGuid``
   and one or more ``derivatives`` keyed by short edge size, each carrying
   a ``checksum``), and may set ``X-Apple-MMe-Host`` to redirect us to the
   correct partition.
2. ``POST /{token}/sharedstreams/webasseturls`` with body
   ``{"photoGuids": ["...", ...]}`` returns ``items`` keyed by the per-
   derivative ``checksum``; each item carries ``url_location`` (host) and
   ``url_path`` (path + signed query string). Concatenate and that's a
   short-lived signed download URL.

This client returns the highest-resolution image derivative URL per photo
(skipping live-photo videos and movies).

Same approach used by the long-running community gist
(https://gist.github.com/fay59/8f719cd81967e0eb2234897491e051ec) and many
forks. No auth, no captcha, no CSRF - it's a public-share endpoint.
"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .coordinator import MediaItem

_LOGGER = logging.getLogger(__name__)

# Default partition Apple's web client starts with; the real partition is
# returned in ``X-Apple-MMe-Host`` if the album lives elsewhere.
_DEFAULT_PARTITION = "p23"

# Regex: pull the token out of an iCloud share URL. We accept either the
# ``#TOKEN`` fragment form (most common) or a query/path style as a
# fallback. Tokens are alphanumeric strings starting with a capital letter
# in current iCloud, but we don't lock that down.
_TOKEN_RE = re.compile(r"#([A-Za-z0-9_-]+)")

_FETCH_TIMEOUT = 30.0
_MAX_BATCH = 25  # photoGuid batches per webasseturls call

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Safari/605.1.15"
)


class ICloudError(Exception):
    """Generic iCloud webstream API error."""


class ICloudInvalidToken(ICloudError):
    """The share token isn't recognized (404 / Invalid response)."""


@dataclass
class _Derivative:
    """One served size of a photo. Apple may return many; we pick max."""

    checksum: str
    width: int | None
    height: int | None
    file_size: int | None


@dataclass
class _Photo:
    photo_guid: str
    caption: str | None
    captured_at_ms: int | None
    media_asset_type: str | None  # "image" / "video"
    derivatives: list[_Derivative]


def parse_share_url(share_url: str) -> str | None:
    """Extract the token from a public iCloud shared-album URL."""
    if not isinstance(share_url, str):
        return None
    parsed = urlparse(share_url.strip())
    host = (parsed.hostname or "").lower()
    if "icloud.com" not in host:
        return None
    # ``#TOKEN`` fragment - the canonical form.
    if parsed.fragment:
        first = parsed.fragment.split("&", 1)[0].split("/", 1)[0]
        if first:
            return first
    # Fallback: try to find ``#TOKEN`` anywhere in the raw string.
    m = _TOKEN_RE.search(share_url)
    return m.group(1) if m else None


def _base_url(partition: str, token: str) -> str:
    return f"https://{partition}-sharedstreams.icloud.com/{token}/sharedstreams"


def _headers() -> dict[str, str]:
    return {
        "Content-Type": "text/plain",  # what the iCloud web client sends
        "Accept": "*/*",
        "Origin": "https://www.icloud.com",
        "Referer": "https://www.icloud.com/",
        "User-Agent": _BROWSER_UA,
    }


async def _post_json(
    session,
    url: str,
    body: dict[str, Any],
) -> dict[str, Any]:
    async def _do() -> dict[str, Any]:
        async with session.post(url, json=body, headers=_headers()) as resp:
            status = resp.status
            text = ""
            try:
                text = await resp.text()
            except Exception:
                pass
            _LOGGER.debug(
                "iCloud: POST %s -> %d (body[:200]=%r)", url, status, text[:200]
            )
            if status == 404:
                raise ICloudInvalidToken(f"Album not found (HTTP 404): {url}")
            if status >= 400:
                raise ICloudError(
                    f"iCloud webstream HTTP {status} on {url} body={text[:200]!r}"
                )
            try:
                import json as _json

                return _json.loads(text) if text else {}
            except Exception as err:
                raise ICloudError(
                    f"iCloud returned non-JSON body on {url}: {err}"
                ) from err

    return await asyncio.wait_for(_do(), timeout=_FETCH_TIMEOUT)


def _parse_apple_iso_ms(value: Any) -> int | None:
    """Apple uses ISO 8601 strings like ``2024-09-15T13:42:09Z``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime, timezone

        # Apple sometimes emits ``...+0000`` or ``...Z``. Normalise.
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _parse_photos(payload: dict[str, Any]) -> list[_Photo]:
    photos: list[_Photo] = []
    for raw in payload.get("photos") or []:
        if not isinstance(raw, dict):
            continue
        guid = raw.get("photoGuid")
        if not isinstance(guid, str) or not guid:
            continue

        derivatives_raw = raw.get("derivatives") or {}
        if not isinstance(derivatives_raw, dict):
            continue

        derivatives: list[_Derivative] = []
        for _key, der in derivatives_raw.items():
            if not isinstance(der, dict):
                continue
            checksum = der.get("checksum")
            if not isinstance(checksum, str) or not checksum:
                continue
            try:
                width = int(der.get("width")) if der.get("width") is not None else None
            except (TypeError, ValueError):
                width = None
            try:
                height = (
                    int(der.get("height")) if der.get("height") is not None else None
                )
            except (TypeError, ValueError):
                height = None
            try:
                file_size = (
                    int(der.get("fileSize")) if der.get("fileSize") is not None else None
                )
            except (TypeError, ValueError):
                file_size = None
            derivatives.append(
                _Derivative(
                    checksum=checksum,
                    width=width,
                    height=height,
                    file_size=file_size,
                )
            )

        if not derivatives:
            continue

        media_type = raw.get("mediaAssetType")
        if not isinstance(media_type, str):
            media_type = None

        captured = _parse_apple_iso_ms(raw.get("dateCreated")) or _parse_apple_iso_ms(
            raw.get("batchDateCreated")
        )

        caption = raw.get("caption") if isinstance(raw.get("caption"), str) else None

        photos.append(
            _Photo(
                photo_guid=guid,
                caption=caption,
                captured_at_ms=captured,
                media_asset_type=media_type,
                derivatives=derivatives,
            )
        )

    return photos


def _pick_largest(derivatives: list[_Derivative]) -> _Derivative:
    """Pick the highest-resolution derivative we can.

    Prefer one with both width and height; fall back to fileSize ordering
    when dimensions aren't reported.
    """
    def _score(d: _Derivative) -> tuple[int, int]:
        area = (d.width or 0) * (d.height or 0)
        return (area, d.file_size or 0)

    return max(derivatives, key=_score)


async def fetch_album(session, share_url: str) -> tuple[str | None, list[MediaItem]]:
    """Fetch a public iCloud shared album and return ``(title, items)``.

    Movies and live-photo video sidecars are skipped - only still images
    end up in the returned list.
    """
    token = parse_share_url(share_url)
    if not token:
        raise ICloudError(
            "Could not extract album token from URL. Expected a link like "
            "https://www.icloud.com/sharedalbum/#B0XXXXXXXXXX"
        )

    base = _base_url(_DEFAULT_PARTITION, token)
    payload = await _post_json(session, f"{base}/webstream", {"streamCtag": None})

    # Apple may redirect us to the correct partition.
    new_host = payload.get("X-Apple-MMe-Host")
    if isinstance(new_host, str) and new_host:
        new_partition = new_host.split("-sharedstreams", 1)[0]
        if new_partition and new_partition != _DEFAULT_PARTITION:
            _LOGGER.debug(
                "iCloud: redirecting to partition %s (was %s)",
                new_partition,
                _DEFAULT_PARTITION,
            )
            base = _base_url(new_partition, token)
            payload = await _post_json(
                session, f"{base}/webstream", {"streamCtag": None}
            )

    photos = _parse_photos(payload)
    title = payload.get("streamName") if isinstance(payload.get("streamName"), str) else None
    if not photos:
        return title, []

    # We want the largest-derivative checksum per photo.
    selected: dict[str, _Derivative] = {}
    for photo in photos:
        if photo.media_asset_type and photo.media_asset_type.lower() != "image":
            # Skip movies and live-photo video sidecars.
            continue
        selected[photo.photo_guid] = _pick_largest(photo.derivatives)

    if not selected:
        return title, []

    # Resolve signed URLs in batches via webasseturls.
    checksum_to_url: dict[str, str] = {}
    guids = list(selected.keys())
    for i in range(0, len(guids), _MAX_BATCH):
        batch = guids[i : i + _MAX_BATCH]
        urls_resp = await _post_json(
            session, f"{base}/webasseturls", {"photoGuids": batch}
        )
        items = urls_resp.get("items")
        if not isinstance(items, dict):
            continue
        for checksum, info in items.items():
            if not isinstance(info, dict):
                continue
            location = info.get("url_location")
            path = info.get("url_path")
            if not isinstance(location, str) or not isinstance(path, str):
                continue
            checksum_to_url[checksum] = f"https://{location}{path}"

    out: list[MediaItem] = []
    for photo in photos:
        if photo.photo_guid not in selected:
            continue
        chosen = selected[photo.photo_guid]
        url = checksum_to_url.get(chosen.checksum)
        if not url:
            continue
        out.append(
            MediaItem(
                url=url,
                width=chosen.width,
                height=chosen.height,
                mime_type="image/jpeg",
                filename=None,
                captured_at=photo.captured_at_ms,
                uploaded_at=None,
                byte_size=chosen.file_size,
            )
        )

    return title, out
