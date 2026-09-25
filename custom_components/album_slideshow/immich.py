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
- ``GET /api/faces?id={id}`` -> recognised faces and bounding boxes - used to
    keep faces whole in cover-mode crops.
- Image bytes: ``/api/assets/{id}/thumbnail?size=preview|fullsize`` or
    ``/api/assets/{id}/original`` (all require the ``x-api-key`` header).
"""
from __future__ import annotations

import json
import math
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
    out = _filter_image_items(items)
    next_page = assets.get("nextPage")
    if isinstance(next_page, str) and next_page.isdigit():
        next_page = int(next_page)
    if not isinstance(next_page, int):
        next_page = None
    return out, next_page


def parse_random(payload: Any) -> list[dict[str, Any]]:
    """Return image items from a ``/api/search/random`` response.

    ``search/random`` returns a plain list of assets (no pagination wrapper).
    """
    if isinstance(payload, list):
        return _filter_image_items(payload)
    # Some cores wrap it like search/metadata; handle that too.
    if isinstance(payload, dict):
        assets = payload.get("assets")
        if isinstance(assets, dict):
            return _filter_image_items(assets.get("items"))
    return []


def _filter_image_items(items: Any) -> list[dict[str, Any]]:
    """Keep only non-trashed, non-archived image assets with an id."""
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
    return out


def build_search_body(
    selection_type: str, selection_id: str | None, filter_body: dict | None
) -> dict[str, Any]:
    """Build the ``search/metadata`` request body for a selection.

    Always constrains to images. For ``search`` the user-supplied filter is
    used as a base (with ``type`` forced to IMAGE). ``album``/``person`` add
    the id filter; ``favorites`` sets ``isFavorite``; ``all`` adds nothing.
    """
    body: dict[str, Any] = {"type": "IMAGE"}
    if selection_type == "search" and isinstance(filter_body, dict):
        body = dict(filter_body)
        body["type"] = "IMAGE"
    elif selection_type == "album" and selection_id:
        body["albumIds"] = [selection_id]
    elif selection_type == "person" and selection_id:
        body["personIds"] = [selection_id]
    elif selection_type == "favorites":
        body["isFavorite"] = True
    # ``all`` -> no extra filter (whole library).
    return body


def parse_composite_selection(selection_id: str | None) -> dict[str, Any]:
    """Parse a composite selection id into ``{albums, people, favorites}``.

    The id is a JSON object; anything malformed degrades to an empty
    composite (which means "all photos").
    """
    albums: list[str] = []
    people: list[str] = []
    favorites = False
    if selection_id:
        try:
            data = json.loads(selection_id)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            albums = [a for a in data.get("albums", []) if isinstance(a, str) and a]
            people = [p for p in data.get("people", []) if isinstance(p, str) and p]
            favorites = bool(data.get("favorites"))
    return {"albums": albums, "people": people, "favorites": favorites}


def build_composite_bodies(
    selection_id: str | None, filter_body: dict | None = None
) -> list[dict[str, Any]]:
    """Build one ``search/metadata`` body per composite union member.

    Immich has no OR, so each album, person, the favorites flag and any
    custom filter becomes its own image query; the caller unions the
    results. An empty composite yields a single unfiltered query -> the
    whole library ("all photos").
    """
    sel = parse_composite_selection(selection_id)
    bodies: list[dict[str, Any]] = []
    for aid in sel["albums"]:
        bodies.append({"type": "IMAGE", "albumIds": [aid]})
    for pid in sel["people"]:
        bodies.append({"type": "IMAGE", "personIds": [pid]})
    if sel["favorites"]:
        bodies.append({"type": "IMAGE", "isFavorite": True})
    if isinstance(filter_body, dict) and filter_body:
        member = dict(filter_body)
        member["type"] = "IMAGE"
        bodies.append(member)
    if not bodies:
        bodies.append({"type": "IMAGE"})
    return bodies


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


def selected_person_ids(
    selection_type: str | None, selection_id: str | None
) -> set[str]:
    """Return the people explicitly selected for an Immich source.

    Current entries use a composite JSON selection, while older entries can
    still carry the legacy ``person``/``people`` shapes. Album-only, favorite,
    random and custom-search sources intentionally return an empty set: there
    is no user-selected face to prioritise for those sources.
    """
    if selection_type == "person":
        return {selection_id} if selection_id else set()
    if selection_type == "people":
        return {person_id for person_id in (selection_id or "").split(",") if person_id}
    if selection_type == "composite":
        return set(parse_composite_selection(selection_id)["people"])
    return set()


def _normalised_face_box(
    face: Any,
    edits: list[dict[str, Any]] | None = None,
    original_size: tuple[float, float] | None = None,
) -> tuple[float, float, float, float] | None:
    """Return a face's bounding box normalised to 0..1, or None if invalid."""
    if not isinstance(face, dict):
        return None
    width = face.get("imageWidth")
    height = face.get("imageHeight")
    coords = (
        face.get("boundingBoxX1"),
        face.get("boundingBoxY1"),
        face.get("boundingBoxX2"),
        face.get("boundingBoxY2"),
    )
    if (
        any(isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in (width, height, *coords))
        or width <= 0
        or height <= 0
    ):
        return None
    x1, y1, x2, y2 = coords
    if x2 <= x1 or y2 <= y1:
        return None
    box = _original_face_box(
        (x1 / width, y1 / height, x2 / width, y2 / height),
        edits or [], original_size,
    )
    box = tuple(max(0.0, min(1.0, value)) for value in box)
    return box if box[2] > box[0] and box[3] > box[1] else None


def parse_face_boxes(
    faces: Any,
    person_ids: set[str] | None = None,
    *,
    edits: list[dict[str, Any]] | None = None,
    original_size: tuple[float, float] | None = None,
) -> list[list[float | bool]]:
    """Return normalised ``[x1, y1, x2, y2, area, selected]`` face boxes.

    Immich reports face boxes in the coordinate space described by each
    face's ``imageWidth``/``imageHeight``. Normalising each box makes it
    independent of whether Album Slideshow downloads a preview, full-size
    derivative or original. Faces Immich has not linked to a named person
    are included. Explicit person selection is a separate priority from area.
    """
    if not isinstance(faces, list):
        return []
    boxes: list[list[float | bool]] = []
    for face in faces:
        box = _normalised_face_box(face, edits, original_size)
        if box is None:
            continue
        x1, y1, x2, y2 = box
        weight = (x2 - x1) * (y2 - y1)
        person = face.get("person")
        selected = bool(
            person_ids and isinstance(person, dict) and person.get("id") in person_ids
        )
        boxes.append([x1, y1, x2, y2, weight, selected])
    return boxes


def _original_face_box(
    box: tuple[float, float, float, float],
    edits: list[dict[str, Any]],
    original_size: tuple[float, float] | None,
) -> tuple[float, float, float, float]:
    points = [(box[0], box[1]), (box[2], box[3])]
    crop = None
    for edit in reversed(edits):
        if not isinstance(edit, dict) or not isinstance(edit.get("parameters"), dict):
            raise ValueError("Invalid Immich edit metadata")
        params = edit["parameters"]
        action = edit.get("action")
        if action == "rotate":
            angle = params.get("angle")
            if angle == 90:
                points = [(vertical, 1 - horizontal) for horizontal, vertical in points]
            elif angle == 180:
                points = [(1 - horizontal, 1 - vertical) for horizontal, vertical in points]
            elif angle == 270:
                points = [(1 - vertical, horizontal) for horizontal, vertical in points]
            elif angle != 0:
                raise ValueError("Unsupported Immich rotation")
        elif action == "mirror":
            axis = params.get("axis")
            if axis == "horizontal":
                points = [(horizontal, 1 - vertical) for horizontal, vertical in points]
            elif axis == "vertical":
                points = [(1 - horizontal, vertical) for horizontal, vertical in points]
            else:
                raise ValueError("Unsupported Immich mirror axis")
        elif action == "crop" and crop is None:
            crop = params
        else:
            raise ValueError("Unsupported Immich edit")
    if crop is not None:
        values = [crop.get(name) for name in ("x", "y", "width", "height")]
        if original_size is None or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in values
        ):
            raise ValueError("Missing Immich crop dimensions")
        crop_left, crop_top, crop_width, crop_height = values
        original_width, original_height = original_size
        if (
            crop_left < 0 or crop_top < 0 or crop_width <= 0 or crop_height <= 0
            or crop_left + crop_width > original_width
            or crop_top + crop_height > original_height
        ):
            raise ValueError("Invalid Immich crop dimensions")
        points = [
            ((horizontal * crop_width + crop_left) / original_width,
             (vertical * crop_height + crop_top) / original_height)
            for horizontal, vertical in points
        ]
    return (
        min(point[0] for point in points), min(point[1] for point in points),
        max(point[0] for point in points), max(point[1] for point in points),
    )


def original_image_size(asset: Any) -> tuple[float, float] | None:
    info = asset.get("exifInfo") if isinstance(asset, dict) else None
    if not isinstance(info, dict):
        return None
    width, height = info.get("exifImageWidth"), info.get("exifImageHeight")
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value) or value <= 0 for value in (width, height)
    ):
        return None
    if str(info.get("orientation")) in {"5", "6", "7", "8", "-90", "90"}:
        return (float(height), float(width))
    return (float(width), float(height))


def parse_face_focus(
    faces: Any,
    person_ids: set[str],
    *,
    edits: list[dict[str, Any]] | None = None,
    original_size: tuple[float, float] | None = None,
) -> tuple[float, float] | None:
    """Return a normalised crop focus for the selected people in an asset.

    Immich reports face boxes in the coordinate space described by each
    face's ``imageWidth``/``imageHeight``. Normalising each box makes the
    focus independent of resolution. Edited coordinates are mapped back to the
    unedited image before clamping. When several selected people appear in the
    same photo, focus on the centre of their combined region.
    """
    if not isinstance(faces, list) or not person_ids:
        return None
    selected_faces = [
        face for face in faces
        if isinstance(face, dict) and isinstance(face.get("person"), dict)
        and face["person"].get("id") in person_ids
    ]
    boxes = parse_face_boxes(selected_faces, edits=edits, original_size=original_size)
    if not boxes:
        return None
    left = min(box[0] for box in boxes)
    top = min(box[1] for box in boxes)
    right = max(box[2] for box in boxes)
    bottom = max(box[3] for box in boxes)
    return ((left + right) / 2, (top + bottom) / 2)


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
        self,
        selection_type: str,
        selection_id: str | None = None,
        filter_body: dict | None = None,
    ) -> list[dict[str, Any]]:
        """Collect image assets for a selection.

        ``random`` uses ``/api/search/random`` (a single, unpaginated batch).
        Everything else pages through ``/api/search/metadata`` with a body
        built from the selection.
        """
        if selection_type == "random":
            body = {"size": min(_PAGE_SIZE, 250), "type": "IMAGE"}
            if isinstance(filter_body, dict):
                merged = dict(filter_body)
                merged.update(body)
                body = merged
            payload = await self._post("/api/search/random", body)
            return parse_random(payload)

        if selection_type == "people":
            # Immich treats multiple personIds in one query as AND (only photos
            # where everyone appears together). To get OR (any of the people),
            # query each person separately and union by asset id. See #19.
            ids = [p for p in (selection_id or "").split(",") if p]
            bodies = [{"type": "IMAGE", "personIds": [p]} for p in ids]
            return await self._collect_union(bodies)

        if selection_type == "albums":
            # Same OR behavior for a set of albums: query each album on its own
            # and union the results, deduped by asset id.
            ids = [a for a in (selection_id or "").split(",") if a]
            bodies = [{"type": "IMAGE", "albumIds": [a]} for a in ids]
            return await self._collect_union(bodies)

        if selection_type == "composite":
            # A mix of albums, people, favorites and/or a custom filter. Each
            # is queried on its own and unioned; an empty composite means the
            # whole library. See #19.
            bodies = build_composite_bodies(selection_id, filter_body)
            return await self._collect_union(bodies)

        base = build_search_body(selection_type, selection_id, filter_body)
        return await self._collect_metadata(base)

    async def _collect_metadata(self, base: dict[str, Any]) -> list[dict[str, Any]]:
        """Page through ``search/metadata`` for a prebuilt body."""
        collected: list[dict[str, Any]] = []
        page: int | None = 1
        while page is not None and len(collected) < _MAX_ASSETS:
            body = dict(base)
            body["size"] = _PAGE_SIZE
            body["page"] = page
            payload = await self._post("/api/search/metadata", body)
            items, next_page = parse_search_page(payload)
            collected.extend(items)
            page = next_page
        return collected

    async def _collect_union(
        self, bodies: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Union several ``search/metadata`` queries (OR), deduped by asset id.

        Each body is queried on its own so the results are a union (any of),
        not Immich's default AND (only assets that match every filter at
        once). See #19.
        """
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for body in bodies:
            if len(out) >= _MAX_ASSETS:
                break
            items = await self._collect_metadata(body)
            for it in items:
                aid = it.get("id")
                if aid and aid not in seen:
                    seen.add(aid)
                    out.append(it)
        return out

    async def async_get_asset(self, asset_id: str) -> dict[str, Any]:
        return await self._get(f"/api/assets/{asset_id}")

    async def async_get_faces(self, asset_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/faces?id={asset_id}")
        if not isinstance(data, list):
            raise ValueError("Invalid Immich face response")
        return data

    async def async_get_asset_edits(self, asset_id: str) -> list[dict[str, Any]]:
        data = await self._get(f"/api/assets/{asset_id}/edits")
        if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
            raise ValueError("Invalid Immich edit response")
        return data["edits"]
