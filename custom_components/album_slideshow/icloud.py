"""iCloud Shared Album client and pure parsing helpers.

Reads a public iCloud shared photo album. No account or password is involved;
the album's share token (from the share link) is the only credential. Apple
serves shared albums through two different public backends depending on when
the album was created, and this module speaks both:

Legacy "shared streams" backend (``www.icloud.com/sharedalbum/#TOKEN``):
- ``POST {base}/webstream`` ``{"streamCtag": null}`` -> ``{streamName, photos:
    [{photoGuid, derivatives:{<height>:{checksum,width,height,fileSize}},
    dateCreated, caption, width, height}]}``. May first answer with a
    ``330`` redirect carrying an ``X-Apple-MMe-Host`` header pointing at the
    correct partition host; retry there.
- ``POST {base}/webasseturls`` ``{"photoGuids": [...]}`` -> ``{items:
    {<checksum>: {url_location, url_path, url_expiry}}}``. Build the image URL
    as ``https://{url_location}{url_path}``; it is a signed CDN link that
    expires after roughly a day, so it is refreshed on every album refresh.

CloudKit backend (``photos.icloud.com/shared/album/TOKEN``, iOS 26/macOS 26+):
- ``POST ckdatabasews.icloud.com/.../public/records/resolve?shortGUID=TOKEN``
    resolves the short link to a shared CloudKit zone and hands back an
    anonymous ``publicAccessAuthToken`` plus the partition host to talk to.
- ``POST {partition}/.../shared/records/query`` with the anonymous token and
    ``sharing_url_key`` returns ``CPLMaster``/``CPLAsset`` records; each
    ``CPLMaster`` carries signed ``downloadURL`` derivatives directly.

Both backends are POST with ``Content-Type: text/plain`` and
``Origin: https://www.icloud.com``. Metadata (capture date, caption) is inline;
Apple strips GPS from shared-album web data, so there is no location.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

import async_timeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = 30
_MAX_ASSETS = 20_000
# webasseturls request batch size. A single call handled 40+ guids fine in
# testing; chunking keeps request bodies bounded for very large albums.
_URL_BATCH = 25

_BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Which iCloud backend serves an album. These string values match the
# ``ICLOUD_BACKEND_*`` constants in ``const.py``; they are duplicated here to
# keep this module free of a hard dependency on ``const``.
BACKEND_SHAREDSTREAMS = "sharedstreams"
BACKEND_CLOUDKIT = "cloudkit"

# Headers Apple's web endpoints expect for the shared-streams API.
_API_HEADERS = {
    "Content-Type": "text/plain",
    "Origin": "https://www.icloud.com",
    "Accept": "application/json",
}

# --- CloudKit backend constants -------------------------------------------
_CK_RESOLVE_HOST = "https://ckdatabasews.icloud.com"
_CK_CONTAINER = "com.apple.photos.cloud"
_CK_BUILD = "2626"
# Sorted, non-hidden, non-deleted assets. Returns both CPLMaster (image
# resources) and CPLAsset (metadata) records in one query.
_CK_RECORD_TYPE = "CPLAssetAndMasterByAssetDateWithoutHiddenOrDeleted"
_CK_PAGE = 200

_CK_HEADERS = {
    "Content-Type": "text/plain",
    "Origin": "https://www.icloud.com",
    "Referer": "https://www.icloud.com/",
    "Accept": "application/json",
}

# Image resource fields on a CPLMaster, in ascending size order, paired with
# their width/height sibling fields.
_CK_IMAGE_RES = (
    ("resJPEGThumbRes", "resJPEGThumbWidth", "resJPEGThumbHeight"),
    ("resJPEGMedRes", "resJPEGMedWidth", "resJPEGMedHeight"),
    ("resJPEGLargeRes", "resJPEGLargeWidth", "resJPEGLargeHeight"),
    ("resOriginalRes", "resOriginalWidth", "resOriginalHeight"),
)
# Original item types a browser can display directly. The JPEG derivatives are
# always browser-safe; the original is only used as a fallback when it is one
# of these.
_CK_BROWSER_SAFE_ORIGINAL = {"public.jpeg", "public.png"}
# itemType substrings that mark a master as a video (skipped in a slideshow).
_CK_VIDEO_HINTS = ("movie", "video", "mpeg-4", "quicktime")


def _base62_to_int(value: str) -> int:
    result = 0
    for ch in value:
        result = result * 62 + _BASE62.index(ch)
    return result


def parse_share_link(url: str) -> str | None:
    """Extract the album share token from a pasted iCloud link.

    Accepts a legacy ``https://www.icloud.com/sharedalbum/#TOKEN`` link, a new
    ``https://photos.icloud.com/shared/album/TOKEN`` link, or a bare token.
    Returns ``None`` if nothing token-like is found.
    """
    if not url:
        return None
    text = url.strip()
    # Legacy links carry the token in the fragment; new links carry it as the
    # last path segment.
    if "#" in text:
        text = text.rsplit("#", 1)[1]
    elif "/" in text:
        text = text.rstrip("/").rsplit("/", 1)[1]
    # Drop any leftover query string.
    text = text.split("?", 1)[0]
    token = text.strip()
    # Tokens are base62. Legacy tokens start with an uppercase letter; the new
    # CloudKit short GUIDs may start with a digit, so no leading-char check.
    if token and all(ch in _BASE62 for ch in token):
        return token
    return None


def detect_backend(url: str) -> str:
    """Return which iCloud backend a pasted share link belongs to.

    New ``photos.icloud.com/shared/album/...`` links use the CloudKit backend;
    everything else (including bare tokens) defaults to the legacy shared
    streams backend, preserving behaviour for links created before CloudKit.
    """
    text = (url or "").lower()
    if "/shared/album/" in text or "photos.icloud.com" in text:
        return BACKEND_CLOUDKIT
    return BACKEND_SHAREDSTREAMS


def parse_share(url: str) -> tuple[str, str] | None:
    """Parse a share link into ``(token, backend)`` or ``None`` if invalid."""
    token = parse_share_link(url)
    if not token:
        return None
    return token, detect_backend(url)



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


# --- CloudKit backend helpers ---------------------------------------------


def _ck_value(fields: dict[str, Any], name: str) -> Any:
    """Return the inner ``value`` of a CloudKit field, or ``None``."""
    field = fields.get(name) if isinstance(fields, dict) else None
    return field.get("value") if isinstance(field, dict) else None


def _ck_decode_filename(fields: dict[str, Any]) -> str | None:
    """Decode the (base64) ``filenameEnc`` field of a CPLMaster, if present."""
    raw = _ck_value(fields, "filenameEnc")
    if isinstance(raw, str) and raw:
        try:
            return base64.b64decode(raw).decode("utf-8", "replace")
        except (ValueError, TypeError):
            return None
    return None


def _ck_is_video(master_fields: dict[str, Any]) -> bool:
    """Return ``True`` when a CPLMaster describes a video (not a still image).

    Only the ``itemType`` is used: Live Photos keep an image ``itemType`` (and
    carry a non-zero asset duration), so they are treated as stills and shown.
    """
    item_type = _ck_value(master_fields, "itemType")
    if isinstance(item_type, str):
        lowered = item_type.lower()
        return any(hint in lowered for hint in _CK_VIDEO_HINTS)
    return False


def pick_ck_resource(
    master_fields: dict[str, Any], size: str
) -> tuple[str, Any, Any] | None:
    """Pick a downloadable image derivative for the requested display ``size``.

    Returns ``(download_url, width, height)``. ``full`` selects the largest
    available derivative (best for a slideshow); ``preview`` selects the
    smallest. JPEG derivatives are always browser-safe; the original is only
    considered when it is itself a browser-displayable format.
    """
    if not isinstance(master_fields, dict):
        return None
    item_type = _ck_value(master_fields, "itemType")
    item_type = item_type.lower() if isinstance(item_type, str) else ""
    original_safe = item_type in _CK_BROWSER_SAFE_ORIGINAL

    candidates: list[tuple[int, str, Any, Any]] = []
    for res_key, w_key, h_key in _CK_IMAGE_RES:
        res = master_fields.get(res_key)
        if not isinstance(res, dict):
            continue
        val = res.get("value")
        if not isinstance(val, dict):
            continue
        download_url = val.get("downloadURL")
        if not download_url:
            continue
        if res_key == "resOriginalRes" and not original_safe:
            continue
        width = _ck_value(master_fields, w_key)
        height = _ck_value(master_fields, h_key)
        try:
            rank = int(width)
        except (TypeError, ValueError):
            rank = 0
        candidates.append((rank, download_url, width, height))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0])
    _rank, download_url, width, height = candidates[0 if size == "preview" else -1]
    return download_url, width, height


def build_ck_image_url(download_url: str | None, filename: str | None = None) -> str | None:
    """Fill the ``${f}`` filename placeholder in a CloudKit download URL."""
    if not download_url:
        return None
    return download_url.replace("${f}", quote(filename or "image", safe=""))


def _ck_share_title(resolve_result: dict[str, Any]) -> str | None:
    """Return the album title from a resolve result's share record, if any."""
    share = resolve_result.get("share") if isinstance(resolve_result, dict) else None
    fields = share.get("fields") if isinstance(share, dict) else None
    title = _ck_value(fields, "cloudkit.title") if isinstance(fields, dict) else None
    if isinstance(title, str) and title.strip():
        return title.strip()
    return None


def _to_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def parse_ck_records(records: list[Any], size: str) -> list[dict[str, Any]]:
    """Join CPLMaster + CPLAsset records into normalized photo dicts.

    Each returned dict has ``source_id``, ``url``, ``width``, ``height``,
    ``captured_at`` (epoch ms) and ``description``.
    """
    masters: dict[str, dict[str, Any]] = {}
    assets: list[dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("recordType") == "CPLMaster":
            masters[rec.get("recordName")] = rec.get("fields") or {}
        elif rec.get("recordType") == "CPLAsset":
            assets.append(rec)

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for asset in assets:
        af = asset.get("fields") or {}
        if _ck_value(af, "isHidden") or _ck_value(af, "trashReason"):
            continue
        ref = af.get("masterRef")
        ref_val = ref.get("value") if isinstance(ref, dict) else None
        master_name = ref_val.get("recordName") if isinstance(ref_val, dict) else None
        master_fields = masters.get(master_name)
        if not master_fields or _ck_is_video(master_fields):
            continue
        picked = pick_ck_resource(master_fields, size)
        if not picked:
            continue
        download_url, width, height = picked
        url = build_ck_image_url(download_url, _ck_decode_filename(master_fields))
        if not url:
            continue
        source_id = master_name or asset.get("recordName")
        if not source_id or source_id in seen:
            continue
        seen.add(source_id)
        out.append(
            {
                "source_id": source_id,
                "url": url,
                "width": _to_int(width),
                "height": _to_int(height),
                "captured_at": _to_int(_ck_value(af, "assetDate")),
                "description": None,
            }
        )
    return out


class IcloudCloudKitClient:
    """Async client for the CloudKit-backed iCloud shared album backend.

    Everything is anonymous: a ``resolve`` call turns the short share token
    into a shared CloudKit zone plus a short-lived anonymous access token, and
    a ``records/query`` call returns the album's photos with signed image URLs
    already embedded.
    """

    def __init__(self, hass, token: str) -> None:
        self.hass = hass
        self.token = token
        # Cached (zone, base_url, access_token, title) from the resolve call.
        self._resolved: tuple[dict[str, Any], str, str, str | None] | None = None

    async def _post(self, url: str, body: dict[str, Any]) -> tuple[int, bytes]:
        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.post(url, json=body, headers=_CK_HEADERS) as resp:
                return resp.status, await resp.read()

    async def _resolve(self) -> tuple[dict[str, Any], str, str, str | None]:
        if self._resolved is not None:
            return self._resolved
        import json as _json

        url = (
            f"{_CK_RESOLVE_HOST}/database/1/{_CK_CONTAINER}/production"
            f"/public/records/resolve?ckjsBuildVersion={_CK_BUILD}"
            f"&shortGUID={self.token}"
        )
        status, raw = await self._post(url, {"shortGUIDs": [{"value": self.token}]})
        if status != 200:
            raise RuntimeError(f"iCloud resolve failed: HTTP {status}")
        payload = _json.loads(raw)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not results:
            raise RuntimeError("iCloud share link did not resolve to an album")
        result = results[0]
        zone = result.get("zoneID")
        access = result.get("anonymousPublicAccess") or {}
        access_token = access.get("token")
        partition = access.get("databasePartition")
        if not zone or not access_token or not partition:
            raise RuntimeError("iCloud shared album is not publicly accessible")
        base = f"{partition}/database/1/{_CK_CONTAINER}/production"
        self._resolved = (zone, base, access_token, _ck_share_title(result))
        return self._resolved

    def _query_url(self, base: str, access_token: str) -> str:
        query = urlencode(
            {
                "remapEnums": "true",
                "getCurrentSyncToken": "true",
                "sharing_url_key": self.token,
                "publicAccessAuthToken": access_token,
            }
        )
        return f"{base}/shared/records/query?{query}"

    async def async_validate(self) -> str | None:
        """Return the album title if the share link resolves, else raise."""
        _zone, _base, _token, title = await self._resolve()
        return title

    async def async_get_items(self, size: str) -> list[dict[str, Any]]:
        """Return normalized photo dicts for the album (see ``parse_ck_records``)."""
        import json as _json

        zone, base, access_token, _title = await self._resolve()
        url = self._query_url(base, access_token)
        records: list[Any] = []
        continuation: str | None = None
        while len(records) < _MAX_ASSETS:
            body: dict[str, Any] = {
                "zoneID": zone,
                "query": {"recordType": _CK_RECORD_TYPE},
                "resultsLimit": _CK_PAGE,
            }
            if continuation:
                body["continuationMarker"] = continuation
            status, raw = await self._post(url, body)
            if status != 200:
                raise RuntimeError(f"iCloud records query failed: HTTP {status}")
            payload = _json.loads(raw)
            batch = payload.get("records") if isinstance(payload, dict) else None
            if not batch:
                break
            records.extend(batch)
            continuation = payload.get("continuationMarker") if isinstance(payload, dict) else None
            if not continuation:
                break
        return parse_ck_records(records, size)
