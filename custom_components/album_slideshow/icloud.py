"""iCloud Shared Album client and pure parsing helpers.

Talks to Apple's public "shared streams" web API for a shared photo album -
the same undocumented JSON endpoints the iCloud web album viewer uses. No
account or password is involved; the album's share token (the part after
``#`` in the share link) is the only credential.

API shape (POST, ``Content-Type: text/plain``, ``Origin: https://www.icloud.com``):
- ``POST {base}/webstream`` ``{"streamCtag": null}`` -> ``{streamName, photos:
    [{photoGuid, derivatives:{<height>:{checksum,width,height,fileSize}},
    dateCreated, caption, width, height}]}``. May first answer with a
    ``330`` redirect carrying an ``X-Apple-MMe-Host`` header pointing at the
    correct partition host; retry there.
- ``POST {base}/webasseturls`` ``{"photoGuids": [...]}`` -> ``{items:
    {<checksum>: {url_location, url_path, url_expiry}}}``. Build the image URL
    as ``https://{url_location}{url_path}``; it is a signed CDN link that
    expires after roughly a day, so it is refreshed on every album refresh.

Metadata: capture date (``dateCreated``) and caption are inline. Apple strips
GPS from shared-album web data, so there is no location.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import async_timeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = 30
_MAX_ASSETS = 20_000
# webasseturls request batch size. A single call handled 40+ guids fine in
# testing; chunking keeps request bodies bounded for very large albums.
_URL_BATCH = 25

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Headers Apple's web endpoints expect for the shared-streams API.
_API_HEADERS = {
    "Content-Type": "text/plain",
    "Origin": "https://www.icloud.com",
    "Accept": "application/json",
}


def _base62_to_int(value: str) -> int:
    result = 0
    for ch in value:
        result = result * 62 + _BASE62.index(ch)
    return result


def parse_share_link(url: str) -> str | None:
    """Extract the album share token from a pasted iCloud link.

    Accepts a full ``https://www.icloud.com/sharedalbum/#TOKEN`` link or a
    bare token. Returns ``None`` if nothing token-like is found.
    """
    if not url:
        return None
    text = url.strip()
    if "#" in text:
        text = text.rsplit("#", 1)[1]
    elif "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[1]
    # Tokens are base62 and start with an uppercase letter (A, B, ...).
    token = text.strip()
    if token and all(ch in _BASE62 for ch in token):
        return token
    return None


def partition_host(token: str) -> str:
    """Derive the shared-streams partition host for a token.

    Apple encodes the server partition in the first characters of the token:
    one char after a leading ``A``, otherwise the first two chars.
    """
    if not token:
        return ""
    if token[0] == "A":
        partition = _base62_to_int(token[1:2])
    else:
        partition = _base62_to_int(token[1:3])
    return f"p{partition:02d}-sharedstreams.icloud.com"


def base_url(token: str, host: str | None = None) -> str:
    """Return the ``/sharedstreams/`` base URL for a token (and optional host)."""
    host = host or partition_host(token)
    return f"https://{host}/{token}/sharedstreams"


def _to_epoch_ms(value: Any) -> int | None:
    """Parse an ISO-8601 timestamp to epoch milliseconds, or ``None``."""
    if not isinstance(value, str) or not value:
        return None
    try:
        iso = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def parse_webstream(payload: Any) -> list[dict[str, Any]]:
    """Return the list of photo dicts from a ``webstream`` response."""
    if not isinstance(payload, dict):
        return []
    photos = payload.get("photos")
    if not isinstance(photos, list):
        return []
    out: list[dict[str, Any]] = []
    for p in photos:
        if isinstance(p, dict) and p.get("photoGuid") and isinstance(
            p.get("derivatives"), dict
        ) and p["derivatives"]:
            out.append(p)
    return out


def pick_checksum(photo: dict[str, Any], size: str) -> str | None:
    """Pick the derivative checksum for the requested display ``size``.

    Derivatives are keyed by their long edge (e.g. ``"342"``, ``"2049"``).
    ``full`` selects the largest available (best for a slideshow); ``preview``
    selects the smallest (fastest / least bandwidth).
    """
    derivatives = photo.get("derivatives")
    if not isinstance(derivatives, dict) or not derivatives:
        return None

    def edge(item: tuple[str, Any]) -> int:
        key, val = item
        try:
            return int(key)
        except (TypeError, ValueError):
            pass
        if isinstance(val, dict):
            try:
                return int(val.get("fileSize") or 0)
            except (TypeError, ValueError):
                return 0
        return 0

    entries = list(derivatives.items())
    chosen = min(entries, key=edge) if size == "preview" else max(entries, key=edge)
    val = chosen[1]
    return val.get("checksum") if isinstance(val, dict) else None


def build_image_url(item: dict[str, Any]) -> str | None:
    """Build a fetchable image URL from a ``webasseturls`` item entry."""
    if not isinstance(item, dict):
        return None
    location = item.get("url_location")
    path = item.get("url_path")
    if not location or not path:
        return None
    scheme = item.get("scheme") or "https"
    return f"{scheme}://{location}{path}"


def parse_photo_meta(photo: dict[str, Any]) -> dict[str, Any]:
    """Extract the metadata we surface from a webstream photo item."""
    out: dict[str, Any] = {}
    captured = _to_epoch_ms(photo.get("dateCreated")) or _to_epoch_ms(
        photo.get("batchDateCreated")
    )
    if captured is not None:
        out["captured_at"] = captured
    caption = photo.get("caption")
    if isinstance(caption, str) and caption.strip():
        out["description"] = caption.strip()
    return out


class IcloudClient:
    """Thin async wrapper over the iCloud shared-streams web API."""

    def __init__(self, hass, token: str) -> None:
        self.hass = hass
        self.token = token
        # Resolved after the first webstream call (Apple may redirect us to a
        # different partition host via X-Apple-MMe-Host).
        self._host: str | None = None

    @property
    def base_url(self) -> str:
        return base_url(self.token, self._host)

    async def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.post(
                self.base_url + path, json=body, headers=_API_HEADERS
            ) as resp:
                return resp.status, dict(resp.headers), await resp.read()

    async def async_get_photos(self) -> list[dict[str, Any]]:
        """Fetch the album's photo list, following a partition redirect once."""
        import json as _json

        status, headers, raw = await self._post("/webstream", {"streamCtag": None})
        redirect_host = headers.get("X-Apple-MMe-Host")
        if redirect_host and redirect_host != self._host:
            # Our partition guess was wrong; Apple told us the right host.
            self._host = redirect_host
            status, headers, raw = await self._post("/webstream", {"streamCtag": None})
        if status != 200:
            raise RuntimeError(f"iCloud webstream failed: HTTP {status}")
        payload = _json.loads(raw)
        return parse_webstream(payload)[:_MAX_ASSETS]

    async def async_get_asset_urls(
        self, guids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Resolve signed asset URLs for photo guids, keyed by checksum."""
        import json as _json

        out: dict[str, dict[str, Any]] = {}
        for start in range(0, len(guids), _URL_BATCH):
            chunk = guids[start : start + _URL_BATCH]
            status, _headers, raw = await self._post(
                "/webasseturls", {"photoGuids": chunk}
            )
            if status != 200:
                raise RuntimeError(f"iCloud webasseturls failed: HTTP {status}")
            payload = _json.loads(raw)
            items = payload.get("items") if isinstance(payload, dict) else None
            if isinstance(items, dict):
                out.update(items)
        return out

    async def async_validate(self) -> str | None:
        """Return the album name if the token works, else raise."""
        import json as _json

        status, headers, raw = await self._post("/webstream", {"streamCtag": None})
        redirect_host = headers.get("X-Apple-MMe-Host")
        if redirect_host and redirect_host != self._host:
            self._host = redirect_host
            status, headers, raw = await self._post("/webstream", {"streamCtag": None})
        if status != 200:
            raise RuntimeError(f"iCloud webstream failed: HTTP {status}")
        payload = _json.loads(raw)
        return payload.get("streamName") if isinstance(payload, dict) else None
