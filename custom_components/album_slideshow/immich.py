"""Immich (direct API) client and pure parsing helpers.

Talks to an Immich server using an API key. HTTP lives in ``ImmichClient``;
the parsing/URL helpers are pure functions so they can be unit-tested without
a live server or aiohttp.

API shape (Immich v1.13x / v3, ``/api`` prefix, ``x-api-key`` header):
- ``GET /api/server/about`` -> ``{version, ...}`` (used to validate URL + key)
- ``GET /api/albums`` -> ``[{id, albumName, assetCount}]``
- ``GET /api/people`` -> ``{people: [{id, name}]}``
- ``POST /api/search/metadata`` ``{albumIds|personIds, type, size, page}``
    -> ``{assets: {items: [...], total, nextPage}}``. List items carry
    ``id``/``type``/``localDateTime``/``fileCreatedAt``/``width``/``height``/
    ``originalFileName`` but NOT ``exifInfo``.
- ``GET /api/assets/{id}`` -> full asset incl ``exifInfo`` (lat/long, city,
    country, description) - used to enrich location/description per asset.
- Image bytes: ``/api/assets/{id}/thumbnail?size=preview|fullsize`` or
    ``/api/assets/{id}/original`` (all require the ``x-api-key`` header).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import async_timeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = 30
_PAGE_SIZE = 1000
_MAX_ASSETS = 20_000


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and a trailing ``/api`` from a base URL."""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/api"):
        u = u[: -len("/api")]
    return u


def build_image_url(base_url: str, asset_id: str, size: str) -> str:
    """Build the image URL for an asset at the requested size.

    ``preview`` / ``fullsize`` map to the thumbnail endpoint; ``original``
    fetches the untouched original file. The API key is NOT included here - it
    is sent as a request header so it never leaks into logs or the camera's
    ``current_url`` attribute.
    """
    base = normalize_base_url(base_url)
    if size == "original":
        return f"{base}/api/assets/{asset_id}/original"
    thumb_size = "fullsize" if size == "fullsize" else "preview"
    return f"{base}/api/assets/{asset_id}/thumbnail?size={thumb_size}"


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


def location_label(city: Any, state: Any, country: Any) -> str | None:
    """Build a short ``"City, Country"`` style label from EXIF place fields.

    Prefers ``city`` for the locality, falling back to ``state``. Appends the
    country when present. Returns ``None`` when nothing usable is available.
    """
    parts: list[str] = []
    locality = None
    for candidate in (city, state):
        if isinstance(candidate, str) and candidate.strip():
            locality = candidate.strip()
            break
    if locality:
        parts.append(locality)
    if isinstance(country, str) and country.strip():
        parts.append(country.strip())
    return ", ".join(parts) if parts else None


def parse_search_page(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    """Return ``(image_items, next_page)`` from a search/metadata response.

    Filters out non-image assets and anything trashed/archived. ``next_page``
    is the page number to request next, or ``None`` when done.
    """
    assets = (payload or {}).get("assets") if isinstance(payload, dict) else None
    if not isinstance(assets, dict):
        return [], None
    items = assets.get("items")
    out: list[dict[str, Any]] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            if str(it.get("type", "")).upper() != "IMAGE":
                continue
            if it.get("isTrashed") or it.get("isArchived"):
                continue
            if not it.get("id"):
                continue
            out.append(it)
    next_page = assets.get("nextPage")
    if isinstance(next_page, str) and next_page.isdigit():
        next_page = int(next_page)
    if not isinstance(next_page, int):
        next_page = None
    return out, next_page


def parse_asset_exif(asset: Any) -> dict[str, Any]:
    """Extract the metadata we surface from a full asset detail response."""
    out: dict[str, Any] = {}
    if not isinstance(asset, dict):
        return out
    exif = asset.get("exifInfo")
    if not isinstance(exif, dict):
        return out
    captured = _to_epoch_ms(exif.get("dateTimeOriginal")) or _to_epoch_ms(
        asset.get("localDateTime")
    )
    if captured is not None:
        out["captured_at"] = captured
    lat = exif.get("latitude")
    lon = exif.get("longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        # Immich returns 0/0 or null when there is no fix; treat 0,0 as none.
        if not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            out["latitude"] = float(lat)
            out["longitude"] = float(lon)
    label = location_label(exif.get("city"), exif.get("state"), exif.get("country"))
    if label:
        out["location"] = label
    desc = exif.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()
    return out


class ImmichClient:
    """Thin async wrapper over the Immich REST API."""

    def __init__(self, hass, base_url: str, api_key: str) -> None:
        self.hass = hass
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key

    @property
    def headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    @property
    def image_headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key}

    async def _get(self, path: str) -> Any:
        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.get(self.base_url + path, headers=self.headers) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def _post(self, path: str, body: dict[str, Any]) -> Any:
        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.post(
                self.base_url + path, headers=self.headers, json=body
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def async_validate(self) -> str | None:
        """Return the server version if the URL + key work, else raise."""
        data = await self._get("/api/server/about")
        return data.get("version") if isinstance(data, dict) else None

    async def async_list_albums(self) -> list[dict[str, Any]]:
        data = await self._get("/api/albums")
        return data if isinstance(data, list) else []

    async def async_list_people(self) -> list[dict[str, Any]]:
        data = await self._get("/api/people")
        if isinstance(data, dict):
            people = data.get("people")
            return people if isinstance(people, list) else []
        return data if isinstance(data, list) else []

    async def async_collect_assets(
        self, selection_type: str, selection_id: str
    ) -> list[dict[str, Any]]:
        """Page through search/metadata collecting image assets."""
        key = "albumIds" if selection_type == "album" else "personIds"
        collected: list[dict[str, Any]] = []
        page: int | None = 1
        while page is not None and len(collected) < _MAX_ASSETS:
            body = {key: [selection_id], "type": "IMAGE", "size": _PAGE_SIZE, "page": page}
            payload = await self._post("/api/search/metadata", body)
            items, next_page = parse_search_page(payload)
            collected.extend(items)
            page = next_page
        return collected

    async def async_get_asset(self, asset_id: str) -> dict[str, Any]:
        return await self._get(f"/api/assets/{asset_id}")
