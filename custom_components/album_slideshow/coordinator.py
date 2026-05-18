from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
from typing import Any

import async_timeout
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    PUBLICALBUM_ENDPOINT,
    CONF_PROVIDER,
    CONF_ALBUM_URL,
    CONF_LOCAL_PATH,
    CONF_RECURSIVE,
    CONF_REVERSE_GEOCODE,
    DEFAULT_REVERSE_GEOCODE,
    DOMAIN,
    PROVIDER_GOOGLE_SHARED,
    PROVIDER_LOCAL_FOLDER,
)
from .store import SlideshowStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class MediaItem:
    url: str
    width: int | None
    height: int | None
    mime_type: str | None
    filename: str | None
    # Epoch milliseconds; UTC. ``captured_at`` is when the photo was taken
    # (EXIF-style), ``uploaded_at`` is when it was added to the album.
    captured_at: int | None = None
    uploaded_at: int | None = None
    # File size of the original asset in bytes, when known.
    byte_size: int | None = None
    # GPS coordinates from EXIF (local-folder provider only); decimal
    # degrees, signed (negative = South / West). ``location`` is a
    # human-readable reverse-geocoded label such as
    # ``"Lisbon, Portugal"`` and may be ``None`` even when coordinates
    # are present (cache miss, opt-out, or geocoder offline).
    latitude: float | None = None
    longitude: float | None = None
    location: str | None = None
    # True once the local-folder EXIF reader has visited this file.
    # Prevents re-reading EXIF on every coordinator refresh and lets the
    # background enrichment task skip already-processed files even after
    # an HA restart (the flag round-trips through the items cache).
    exif_scanned: bool = False


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".m2ts"}

_SKIP_DIR_PREFIXES = (".", "@", "#")


def _pick_url(item: dict[str, Any]) -> str | None:
    for key in ("baseUrl", "url", "downloadUrl", "productUrl"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _pick_int(d: dict[str, Any], *path: str) -> int | None:
    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    try:
        return int(cur)
    except Exception:
        return None


def _pick_timestamp_ms(d: dict[str, Any], *path: str) -> int | None:
    """Pull a timestamp from a JSON path and normalise to epoch milliseconds.

    Accepts ints (seconds or milliseconds based on magnitude) and ISO-8601
    strings. Returns ``None`` if the value can't be parsed.
    """
    from datetime import datetime, timezone

    cur: Any = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    if cur is None:
        return None
    if isinstance(cur, int):
        # Heuristic: < 1e12 means seconds, otherwise milliseconds.
        return cur * 1000 if cur < 10**12 else cur
    if isinstance(cur, str):
        try:
            iso = cur.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except Exception:
            return None
    return None


def _find_largest_item_list(obj: Any) -> list[dict[str, Any]]:
    """Find the largest list of dicts that looks like media items.

    First pass: look for lists under well-known key names.
    Second pass: fall back to any list of dicts containing URL-like values.
    """
    best: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []

    _KNOWN_KEYS = {"mediaItems", "items", "photos", "images", "results", "data"}

    def _has_url(d: dict[str, Any]) -> bool:
        for v in d.values():
            if isinstance(v, str) and (
                v.startswith("http://") or v.startswith("https://")
            ):
                return True
        return False

    def _walk(node: Any) -> None:
        nonlocal best, fallback
        if isinstance(node, dict):
            for key, val in node.items():
                if isinstance(val, list):
                    cleaned = [it for it in val if isinstance(it, dict)]
                    if len(cleaned) >= 1:
                        if key in _KNOWN_KEYS:
                            if len(cleaned) > len(best):
                                best = cleaned
                        elif any(_has_url(it) for it in cleaned[:5]):
                            if len(cleaned) > len(fallback):
                                fallback = cleaned
                _walk(val)
        elif isinstance(node, list):
            for val in node:
                _walk(val)

    _walk(obj)
    return best or fallback


def _looks_like_video(raw: dict[str, Any]) -> bool:
    mime = raw.get("mimeType")
    if isinstance(mime, str) and mime.startswith("video/"):
        return True

    meta = raw.get("mediaMetadata")
    if isinstance(meta, dict):
        if "video" in meta:
            return True
        if isinstance(meta.get("mediaType"), str) and meta.get("mediaType", "").upper() == "VIDEO":
            return True

    media_type = raw.get("mediaType") or raw.get("type")
    if isinstance(media_type, str) and media_type.upper() == "VIDEO":
        return True

    filename = raw.get("filename") or raw.get("name")
    if isinstance(filename, str):
        suffix = Path(filename).suffix.lower()
        if suffix in _VIDEO_EXTS:
            return True

    url = _pick_url(raw)
    if isinstance(url, str):
        base_url = url.split("?", 1)[0]
        suffix = Path(base_url).suffix.lower()
        if suffix in _VIDEO_EXTS:
            return True

        lowered = base_url.lower()
        if any(marker in lowered for marker in ("/video", "=dv", "video-") ):
            return True

    def _has_video_markers(node: Any) -> bool:
        if isinstance(node, dict):
            for k, v in node.items():
                key = str(k).lower()
                if key in {
                    "isvideo",
                    "hasvideo",
                    "videometadata",
                    "videovariant",
                    "videostreams",
                    "duration",
                    "durationmillis",
                    "playbackuri",
                }:
                    return True
                if isinstance(v, str) and (v.lower() == "video" or v.lower().startswith("video/")):
                    return True
                if _has_video_markers(v):
                    return True
        elif isinstance(node, list):
            for item in node:
                if _has_video_markers(item):
                    return True
        return False

    if _has_video_markers(raw):
        return True

    return False


# ---------------------------------------------------------------------------
# Local-folder EXIF + reverse-geocode helpers
# ---------------------------------------------------------------------------

# EXIF tag ids we care about. These are stable IANA-registered numbers, so
# we can use them directly without depending on ``PIL.ExifTags.TAGS``
# label lookups (which change wording across Pillow versions).
_EXIF_TAG_DATETIME_ORIGINAL = 36867       # DateTimeOriginal
_EXIF_TAG_OFFSET_TIME_ORIGINAL = 36881    # OffsetTimeOriginal (e.g. "+02:00")
_EXIF_TAG_DATETIME = 306                  # DateTime (modification time)
_EXIF_TAG_GPS_IFD = 34853                 # Pointer to the GPS IFD
_EXIF_GPS_LAT_REF = 1                     # "N" / "S"
_EXIF_GPS_LAT = 2                         # rational tuple
_EXIF_GPS_LON_REF = 3                     # "E" / "W"
_EXIF_GPS_LON = 4                         # rational tuple

# Nominatim (OpenStreetMap) is free but rate-limited to 1 request/second
# per IP and asks integrators to identify themselves with a descriptive
# User-Agent. See https://operations.osmfoundation.org/policies/nominatim/
_NOMINATIM_ENDPOINT = "https://nominatim.openstreetmap.org/reverse"
_NOMINATIM_MIN_INTERVAL_S = 1.1
_NOMINATIM_TIMEOUT_S = 20

# How many EXIF reads to perform between cache flushes. Bigger batches
# mean fewer Store writes but more rework if HA is killed mid-scan.
_EXIF_BATCH_SAVE = 25
_GEOCODE_BATCH_SAVE = 10

# Inserted between background-enrichment iterations so the event loop
# stays responsive on the fast path (items that are already scanned and
# need zero work).
_ENRICHMENT_FAST_PATH_YIELD_EVERY = 100


def _gps_to_decimal(dms: Any, ref: Any) -> float | None:
    """Convert an EXIF GPS ``(deg, min, sec)`` rational tuple to a signed decimal.

    ``ref`` is the matching reference tag (``"N"``/``"S"`` for latitude or
    ``"E"``/``"W"`` for longitude). Returns ``None`` when the inputs are
    malformed or out of the legal coordinate range.
    """
    if not isinstance(dms, (tuple, list)) or len(dms) != 3:
        return None
    try:
        deg = float(dms[0])
        minutes = float(dms[1])
        sec = float(dms[2])
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    value = deg + minutes / 60.0 + sec / 3600.0
    if isinstance(ref, bytes):
        try:
            ref = ref.decode("ascii", errors="ignore")
        except Exception:
            ref = ""
    if isinstance(ref, str) and ref.strip().upper() in {"S", "W"}:
        value = -value
    if not (-180.0 <= value <= 180.0):
        return None
    return value


def _geocode_cache_key(lat: float, lon: float) -> str:
    """Return a stable cache key for a coordinate pair.

    Coordinates are rounded to three decimal places (~111 m precision),
    which is more than enough resolution for a city/neighbourhood label
    while still folding hundreds of nearby photos into a single
    Nominatim call. ``-0.0`` is normalised to ``0.0`` so the cache key
    is identical regardless of which sign of zero the EXIF rounded into.
    """
    lat_r = round(float(lat), 3)
    lon_r = round(float(lon), 3)
    # ``round`` can yield ``-0.0`` for tiny negative inputs.
    if lat_r == 0:
        lat_r = 0.0
    if lon_r == 0:
        lon_r = 0.0
    return f"{lat_r:.3f},{lon_r:.3f}"


def _parse_exif_datetime(raw: Any, offset_raw: Any) -> int | None:
    """Parse an EXIF ``DateTimeOriginal``/``DateTime`` string to epoch ms.

    EXIF stores datetimes as ``"YYYY:MM:DD HH:MM:SS"`` without a timezone.
    If the companion ``OffsetTimeOriginal`` tag is present (introduced
    with EXIF 2.31, common on modern cameras and phones) we use it. When
    it's missing we treat the value as the host's local time, which is
    the closest thing to "what the user expects" for cameras that don't
    record an offset.
    """
    from datetime import datetime, timezone

    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if not raw or raw.startswith(("0000", "    ")):
        return None
    try:
        dt = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None

    offset_str: str | None = None
    if isinstance(offset_raw, bytes):
        try:
            offset_str = offset_raw.decode("ascii", errors="ignore").strip()
        except Exception:
            offset_str = None
    elif isinstance(offset_raw, str):
        offset_str = offset_raw.strip()

    if offset_str:
        try:
            offset_dt = datetime.fromisoformat(f"2000-01-01T00:00:00{offset_str}")
            dt = dt.replace(tzinfo=offset_dt.tzinfo)
        except ValueError:
            dt = dt.astimezone().replace(tzinfo=None).astimezone()
    else:
        # Interpret as the host's local time.
        dt = dt.astimezone()

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _read_local_exif(path: Path) -> dict[str, Any]:
    """Read EXIF metadata for a local file.

    Returns a dict with any of the keys ``captured_at`` (epoch ms),
    ``latitude`` and ``longitude``. All keys are optional - missing
    metadata is just omitted. ``captured_at`` always falls back to the
    file's modification time so date-sorting works even for screenshots
    and scans without EXIF.

    Designed to be called from an executor thread: it does only
    synchronous Pillow + filesystem I/O.
    """
    out: dict[str, Any] = {}

    # mtime fallback for captured_at. Cameras that don't record EXIF
    # (screenshots, scans, certain Android camera apps) still need a
    # plausible "taken on" timestamp for the date-filter sensors and
    # ordering. The EXIF parse below overrides this when present.
    try:
        out["captured_at"] = int(path.stat().st_mtime * 1000)
    except OSError:
        pass

    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow ships with HA core
        return out

    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return out

            dt_raw = exif.get(_EXIF_TAG_DATETIME_ORIGINAL) or exif.get(
                _EXIF_TAG_DATETIME
            )
            offset_raw = exif.get(_EXIF_TAG_OFFSET_TIME_ORIGINAL)
            parsed = _parse_exif_datetime(dt_raw, offset_raw)
            if parsed is not None:
                out["captured_at"] = parsed

            gps = None
            try:
                gps = exif.get_ifd(_EXIF_TAG_GPS_IFD) or None
            except Exception:
                gps = None
            if gps:
                lat = _gps_to_decimal(
                    gps.get(_EXIF_GPS_LAT), gps.get(_EXIF_GPS_LAT_REF)
                )
                lon = _gps_to_decimal(
                    gps.get(_EXIF_GPS_LON), gps.get(_EXIF_GPS_LON_REF)
                )
                if lat is not None and lon is not None:
                    # Null Island guard: GPS chips and some editors stamp
                    # ``(0, 0)`` when the fix is invalid. Treat that as no
                    # location rather than dropping every such photo onto
                    # the equator off the African coast.
                    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
                        return out
                    out["latitude"] = lat
                    out["longitude"] = lon
    except Exception as err:
        _LOGGER.debug("EXIF: failed to read %s: %s", path, err)

    return out


def _format_nominatim_location(payload: dict[str, Any]) -> str | None:
    """Turn a Nominatim reverse-geocode response into a short label.

    Prefers ``city`` > ``town`` > ``village`` > ``municipality`` >
    ``county`` for the locality portion, plus ``country`` when present.
    Falls back to Nominatim's pre-formatted ``display_name`` (truncated
    to the first two comma-separated parts) when no recognisable
    locality fields are present.
    """
    if not isinstance(payload, dict):
        return None
    address = payload.get("address") if isinstance(payload.get("address"), dict) else {}
    locality = None
    for key in ("city", "town", "village", "hamlet", "municipality", "county", "state"):
        val = address.get(key)
        if isinstance(val, str) and val.strip():
            locality = val.strip()
            break
    country = address.get("country") if isinstance(address.get("country"), str) else None

    if locality and country:
        return f"{locality}, {country}"
    if locality:
        return locality
    if isinstance(country, str) and country.strip():
        return country.strip()

    display = payload.get("display_name")
    if isinstance(display, str) and display.strip():
        parts = [p.strip() for p in display.split(",") if p.strip()]
        if len(parts) >= 2:
            return ", ".join(parts[:2])
        if parts:
            return parts[0]
    return None


async def _nominatim_lookup(
    session: Any,
    lat: float,
    lon: float,
    user_agent: str,
) -> str | None:
    """Reverse-geocode a coordinate pair via Nominatim.

    Returns a human-readable label or ``None`` if the lookup failed or
    the response had no useful address. Never raises - geocoding is
    best-effort and a failure must never stop the slideshow.
    """
    params = {
        "format": "jsonv2",
        "lat": f"{lat:.5f}",
        "lon": f"{lon:.5f}",
        "zoom": "10",      # Roughly city-level - we don't need street precision.
        "addressdetails": "1",
    }
    headers = {
        "User-Agent": user_agent,
        "Accept": "application/json",
        "Accept-Language": "en",
    }
    try:
        async with async_timeout.timeout(_NOMINATIM_TIMEOUT_S):
            resp = await session.get(
                _NOMINATIM_ENDPOINT, params=params, headers=headers
            )
            if resp.status == 429:
                _LOGGER.warning(
                    "Nominatim rate-limited the integration; pausing geocode"
                )
                # Honour the policy by sleeping a full second before the
                # caller schedules another request.
                await asyncio.sleep(_NOMINATIM_MIN_INTERVAL_S)
                return None
            if resp.status >= 400:
                _LOGGER.debug("Nominatim returned HTTP %s", resp.status)
                return None
            try:
                payload = await resp.json(content_type=None)
            except Exception:
                return None
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Nominatim lookup failed (%s, %s): %s", lat, lon, err)
        return None

    return _format_nominatim_location(payload)


def _read_manifest_version(integration_dir: Path) -> str:
    """Best-effort read of ``manifest.json`` version. Returns ``""`` on error."""
    try:
        manifest_path = integration_dir / "manifest.json"
        with manifest_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        version = data.get("version")
        if isinstance(version, str):
            return version
    except Exception:
        pass
    return ""


def _merge_prior_enrichment(
    new_items: list[MediaItem], prior_items: list[MediaItem]
) -> None:
    """Copy EXIF + geocode metadata from prior coordinator items by URL.

    Local-folder scans rebuild the ``MediaItem`` list from scratch on
    every refresh, but reading EXIF + reverse-geocoding the same files
    again would be wasteful (and would re-bill the user against the
    Nominatim rate limit). This in-place merge preserves anything we
    learned about files whose URL (and therefore path) hasn't changed.
    """
    if not prior_items:
        return
    prior_by_url: dict[str, MediaItem] = {it.url: it for it in prior_items}
    for item in new_items:
        prev = prior_by_url.get(item.url)
        if prev is None:
            continue
        if prev.captured_at is not None and item.captured_at is None:
            item.captured_at = prev.captured_at
        if prev.latitude is not None and item.latitude is None:
            item.latitude = prev.latitude
        if prev.longitude is not None and item.longitude is None:
            item.longitude = prev.longitude
        if prev.location and not item.location:
            item.location = prev.location
        if prev.exif_scanned:
            item.exif_scanned = True


class AlbumCoordinator(DataUpdateCoordinator):
    # Bump when the persisted item shape changes incompatibly.
    _ITEM_CACHE_VERSION = 2
    # Bump independently of the items cache - the geocode cache is
    # keyed by coordinate and is safe to keep across item-shape changes.
    _GEOCODE_CACHE_VERSION = 1

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store

        self.provider: str = entry.data.get(CONF_PROVIDER, PROVIDER_GOOGLE_SHARED)
        self.album_url: str | None = entry.data.get(CONF_ALBUM_URL)
        self.local_path: str | None = entry.data.get(CONF_LOCAL_PATH)
        self.recursive: bool = bool(entry.data.get(CONF_RECURSIVE, True))

        # Persist the most recent successful album fetch so that a transient
        # network/Google failure doesn't blank the slideshow on restart.
        self._items_cache_store: Store = Store(
            hass,
            self._ITEM_CACHE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.items",
        )
        self._items_cache_loaded: bool = False

        # Reverse-geocode cache. Coordinates rounded to ~100 m via
        # ``_geocode_cache_key``; mapping value -> label string. Shared
        # across all items in the album, persisted independently of the
        # items cache so changes to one don't invalidate the other.
        self._geocode_cache_store: Store = Store(
            hass,
            self._GEOCODE_CACHE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.geocode",
        )
        self._geocode_cache: dict[str, str] = {}
        self._geocode_cache_loaded: bool = False
        # Per-coordinator User-Agent so OSM operators can track us if we
        # ever misbehave. Resolved lazily because __init__ is sync.
        self._integration_version: str | None = None
        # Background EXIF + geocode worker. Cancelled and replaced on
        # every refresh so a stalled run from a previous interval never
        # accumulates.
        self._enrichment_task: asyncio.Task | None = None
        # Progress data exposed to the diagnostic sensor. ``phase`` is
        # ``None``/``"exif"``/``"geocoding"``/``"done"``.
        self._enrich_progress: dict[str, Any] = {
            "phase": None,
            "exif_total": 0,
            "exif_done": 0,
            "geocode_total": 0,
            "geocode_done": 0,
        }

        super().__init__(
            hass,
            _LOGGER,
            name=f"{entry.title} media list",
            update_interval=timedelta(hours=int(store.refresh_hours)),
        )

        def _on_store_change() -> None:
            self.update_interval = timedelta(hours=int(self.store.refresh_hours))

        store.add_listener(_on_store_change)

    async def _async_update_data(self) -> dict[str, Any]:
        # Cancel any in-flight enrichment from a previous interval before
        # the new scan runs - it would otherwise race against the new
        # item list and risk re-saving stale data over fresh state.
        await self._cancel_enrichment()

        try:
            if self.provider == PROVIDER_LOCAL_FOLDER:
                data = await self._update_local_folder()
            elif self.provider == PROVIDER_GOOGLE_SHARED:
                data = await self._update_google_shared()
            else:
                raise UpdateFailed(f"Unsupported provider: {self.provider}")
        except UpdateFailed:
            cached = await self._load_cached_items()
            if cached and cached.get("items"):
                _LOGGER.info(
                    "Album scraper: refresh failed; serving %d cached items from disk",
                    len(cached["items"]),
                )
                return cached
            raise

        items = data.get("items") or []
        if self.provider == PROVIDER_LOCAL_FOLDER and items:
            # Carry forward EXIF/geocode metadata for files we've already
            # scanned this session; new files get filled in by the
            # background worker below.
            prior_items = (self.data or {}).get("items") if isinstance(self.data, dict) else None
            if prior_items:
                _merge_prior_enrichment(items, prior_items)
            self._schedule_enrichment(data)

        if items:
            # Only persist non-empty results so we never overwrite a good
            # cache with a transient empty fetch.
            await self._save_cached_items(data)
        else:
            cached = await self._load_cached_items()
            if cached and cached.get("items"):
                _LOGGER.info(
                    "Album scraper: refresh returned 0 items; serving %d cached items from disk",
                    len(cached["items"]),
                )
                return cached

        return data

    async def _cancel_enrichment(self) -> None:
        """Cancel and reap any in-flight background enrichment task."""
        task = self._enrichment_task
        if task is None or task.done():
            self._enrichment_task = None
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        finally:
            self._enrichment_task = None

    def _schedule_enrichment(self, data: dict[str, Any]) -> None:
        """Kick off the background EXIF + geocode worker if there's work."""
        items: list[MediaItem] = data.get("items") or []
        unscanned = [it for it in items if not it.exif_scanned]
        if not unscanned:
            # Even with nothing to do, mark the phase as ``done`` so the
            # diagnostic sensor stops reporting an in-progress run from
            # the previous refresh.
            self._enrich_progress = {
                "phase": "done",
                "exif_total": len(items),
                "exif_done": len(items),
                "geocode_total": 0,
                "geocode_done": 0,
            }
            return
        self._enrich_progress = {
            "phase": "exif",
            "exif_total": len(items),
            "exif_done": len(items) - len(unscanned),
            "geocode_total": 0,
            "geocode_done": 0,
        }
        self._enrichment_task = self.hass.async_create_background_task(
            self._enrich_items_background(data),
            name=f"album_slideshow_enrich_{self.entry.entry_id}",
        )

    async def _load_cached_items(self) -> dict[str, Any] | None:
        try:
            payload = await self._items_cache_store.async_load()
        except Exception as err:  # pragma: no cover - storage layer
            _LOGGER.debug("Album cache: failed to load (%s)", err)
            return None
        if not isinstance(payload, dict):
            return None

        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return None

        items: list[MediaItem] = []
        for raw in raw_items:
            if not isinstance(raw, dict) or not raw.get("url"):
                continue
            try:
                items.append(MediaItem(
                    url=raw["url"],
                    width=raw.get("width"),
                    height=raw.get("height"),
                    mime_type=raw.get("mime_type"),
                    filename=raw.get("filename"),
                    captured_at=raw.get("captured_at"),
                    uploaded_at=raw.get("uploaded_at"),
                    byte_size=raw.get("byte_size"),
                    latitude=raw.get("latitude"),
                    longitude=raw.get("longitude"),
                    location=raw.get("location"),
                    exif_scanned=bool(raw.get("exif_scanned", False)),
                ))
            except Exception:
                continue

        return {
            "title": payload.get("title"),
            "items": items,
        }

    async def _save_cached_items(self, data: dict[str, Any]) -> None:
        items = data.get("items") or []
        # Local-folder URLs are absolute paths on the host; persisting them
        # is fine but they don't survive a host reformat. Persist anyway,
        # the URL check at load time will skip any stale entries.
        payload = {
            "title": data.get("title"),
            "items": [
                {
                    "url": it.url,
                    "width": it.width,
                    "height": it.height,
                    "mime_type": it.mime_type,
                    "filename": it.filename,
                    "captured_at": it.captured_at,
                    "uploaded_at": it.uploaded_at,
                    "byte_size": it.byte_size,
                    "latitude": it.latitude,
                    "longitude": it.longitude,
                    "location": it.location,
                    "exif_scanned": it.exif_scanned,
                }
                for it in items
            ],
        }
        try:
            await self._items_cache_store.async_save(payload)
        except Exception as err:  # pragma: no cover - storage layer
            _LOGGER.debug("Album cache: failed to save (%s)", err)

    async def _update_local_folder(self) -> dict[str, Any]:
        if not self.local_path:
            raise UpdateFailed("Missing local path")

        root = Path(self.local_path)
        if not root.exists() or not root.is_dir():
            raise UpdateFailed("Local path does not exist or is not a directory")

        def _scan() -> list[Path]:
            it = root.rglob("*") if self.recursive else root.glob("*")
            files: list[Path] = []
            seen: set[str] = set()
            for p in it:
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(root).parts
                if any(
                    part.startswith(_SKIP_DIR_PREFIXES)
                    for part in rel_parts[:-1]
                ):
                    continue
                if p.name.startswith("."):
                    continue
                ext = p.suffix.lower()
                if ext in _VIDEO_EXTS:
                    continue
                if ext not in _IMAGE_EXTS:
                    continue
                # Deduplicate by resolved path (symlinks)
                resolved = str(p.resolve())
                if resolved in seen:
                    continue
                seen.add(resolved)
                files.append(p)
            files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            return files

        try:
            paths = await self.hass.async_add_executor_job(_scan)
        except Exception as err:
            raise UpdateFailed(f"Error scanning local folder: {err}") from err

        items: list[MediaItem] = []
        for p in paths:
            items.append(
                MediaItem(
                    url=f"file://{p.as_posix()}",
                    width=None,
                    height=None,
                    mime_type=None,
                    filename=p.name,
                )
            )

        return {
            "title": root.name,
            "items": items,
        }

    async def _enrich_items_background(self, data: dict[str, Any]) -> None:
        """Read EXIF for unscanned local files, then reverse-geocode.

        Runs as a background task so the coordinator's first update can
        return immediately with the bare file list. Pushes incremental
        updates to listeners after each EXIF batch so attributes appear
        on the camera as soon as they are known.

        Cancellation-safe: any progress made before a cancel is
        persisted via the ``finally`` clause.
        """
        items: list[MediaItem] = data.get("items") or []
        if not items:
            return

        scanned_since_save = 0
        try:
            for idx, item in enumerate(items):
                if item.exif_scanned:
                    # Fast path: yield occasionally so we don't starve
                    # the event loop when (re-)visiting a long list of
                    # already-processed items.
                    if idx % _ENRICHMENT_FAST_PATH_YIELD_EVERY == 0:
                        await asyncio.sleep(0)
                    continue

                url = item.url
                if not url.startswith("file://"):
                    item.exif_scanned = True
                    continue

                path = Path(url[len("file://"):])
                try:
                    info = await self.hass.async_add_executor_job(
                        _read_local_exif, path
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("EXIF: executor error for %s: %s", path, err)
                    info = {}

                if "captured_at" in info:
                    item.captured_at = info["captured_at"]
                if "latitude" in info and "longitude" in info:
                    item.latitude = info["latitude"]
                    item.longitude = info["longitude"]
                item.exif_scanned = True

                scanned_since_save += 1
                self._enrich_progress["exif_done"] = (
                    self._enrich_progress.get("exif_done", 0) + 1
                )

                if scanned_since_save >= _EXIF_BATCH_SAVE:
                    scanned_since_save = 0
                    await self._save_cached_items(data)
                    self.async_set_updated_data(data)

            # Flush any tail items before the geocode phase.
            if scanned_since_save:
                await self._save_cached_items(data)
                self.async_set_updated_data(data)

            await self._geocode_items_background(data)
        except asyncio.CancelledError:
            _LOGGER.debug("Album enrichment: cancelled, persisting progress")
            raise
        finally:
            # Always flush whatever progress we made, even on cancel.
            try:
                await self._save_cached_items(data)
            except Exception:  # pragma: no cover - storage layer
                pass
            # Phase ``done`` lets the diagnostic sensor settle at 100%.
            self._enrich_progress["phase"] = "done"

    async def _geocode_items_background(self, data: dict[str, Any]) -> None:
        """Reverse-geocode items with EXIF GPS but no location label yet.

        Honours the ``reverse_geocode`` option (default on) so privacy-
        conscious users can disable the Nominatim calls without losing
        the GPS coordinates themselves. Successful labels are written
        back into the items in-place and persisted via the items cache.
        """
        if not bool(
            self.entry.options.get(CONF_REVERSE_GEOCODE, DEFAULT_REVERSE_GEOCODE)
            if hasattr(self.entry, "options") and self.entry.options is not None
            else DEFAULT_REVERSE_GEOCODE
        ):
            _LOGGER.debug("Reverse geocode disabled via options; skipping")
            return

        items: list[MediaItem] = data.get("items") or []
        candidates: list[MediaItem] = [
            it
            for it in items
            if it.latitude is not None
            and it.longitude is not None
            and not it.location
        ]
        if not candidates:
            return

        await self._ensure_geocode_cache_loaded()

        self._enrich_progress["phase"] = "geocoding"
        self._enrich_progress["geocode_total"] = len(candidates)
        self._enrich_progress["geocode_done"] = 0

        session = async_get_clientsession(self.hass)
        user_agent = await self._async_user_agent()

        # Last network call wall-time; used to throttle Nominatim to the
        # 1 req/sec policy without sleeping for cached lookups.
        last_call: float = 0.0
        loop = asyncio.get_event_loop()
        unsaved = 0

        try:
            for item in candidates:
                key = _geocode_cache_key(item.latitude, item.longitude)
                cached_label = self._geocode_cache.get(key)
                if cached_label:
                    item.location = cached_label
                    self._enrich_progress["geocode_done"] += 1
                    continue

                # Respect the 1 req/sec Nominatim usage policy.
                elapsed = loop.time() - last_call
                if elapsed < _NOMINATIM_MIN_INTERVAL_S:
                    await asyncio.sleep(_NOMINATIM_MIN_INTERVAL_S - elapsed)

                label = await _nominatim_lookup(
                    session, item.latitude, item.longitude, user_agent
                )
                last_call = loop.time()

                if label:
                    self._geocode_cache[key] = label
                    item.location = label
                    unsaved += 1

                self._enrich_progress["geocode_done"] += 1

                if unsaved >= _GEOCODE_BATCH_SAVE:
                    unsaved = 0
                    await self._save_geocode_cache()
                    await self._save_cached_items(data)
                    self.async_set_updated_data(data)
        finally:
            if unsaved:
                try:
                    await self._save_geocode_cache()
                except Exception:  # pragma: no cover - storage layer
                    pass
            try:
                await self._save_cached_items(data)
            except Exception:  # pragma: no cover - storage layer
                pass
            self.async_set_updated_data(data)

    async def _ensure_geocode_cache_loaded(self) -> None:
        if self._geocode_cache_loaded:
            return
        try:
            payload = await self._geocode_cache_store.async_load()
        except Exception as err:  # pragma: no cover - storage layer
            _LOGGER.debug("Geocode cache: failed to load (%s)", err)
            payload = None
        if isinstance(payload, dict):
            entries = payload.get("entries")
            if isinstance(entries, dict):
                self._geocode_cache = {
                    str(k): str(v)
                    for k, v in entries.items()
                    if isinstance(v, str) and v
                }
        self._geocode_cache_loaded = True

    async def _save_geocode_cache(self) -> None:
        try:
            await self._geocode_cache_store.async_save(
                {"entries": self._geocode_cache}
            )
        except Exception as err:  # pragma: no cover - storage layer
            _LOGGER.debug("Geocode cache: failed to save (%s)", err)

    async def _async_user_agent(self) -> str:
        """Return a Nominatim-friendly User-Agent string.

        OSM operations explicitly require integrators to identify
        themselves; using a generic Python/aiohttp UA gets you blocked.
        We bundle the integration version (from ``manifest.json``) so
        operators can correlate issues to releases.
        """
        if self._integration_version is None:
            try:
                self._integration_version = await self.hass.async_add_executor_job(
                    _read_manifest_version, Path(__file__).parent
                )
            except Exception:
                self._integration_version = ""
        version = self._integration_version or "dev"
        return (
            f"album_slideshow/{version} "
            "(+https://github.com/eyalgal/album_slideshow)"
        )

    async def _call_publicalbum(
        self,
        session,
        max_items: int,
    ) -> dict[str, Any]:
        """Call the publicalbum.org API with automatic fallback on param errors.

        Tries with image dimensions first (1920×1080), then without them
        if the API rejects the size parameters.
        """
        param_sets: list[dict[str, Any]] = [
            {
                "sharedLink": self.album_url,
                "imageWidth": 1920,
                "imageHeight": 1080,
                "includeThumbnails": False,
                "videoQuality": "1080p",
                "attachMetadata": True,
                "maxResults": max_items,
            },
            {
                "sharedLink": self.album_url,
                "includeThumbnails": False,
                "attachMetadata": True,
                "maxResults": max_items,
            },
        ]

        last_error: str | None = None
        for params in param_sets:
            payload = {
                "method": "getGooglePhotosAlbum",
                "params": params,
                "id": 1,
            }
            try:
                async with async_timeout.timeout(60):
                    resp = await session.post(
                        PUBLICALBUM_ENDPOINT,
                        json=payload,
                        headers={
                            "accept": "application/json",
                            "content-type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    data = await resp.json()
            except Exception as err:
                raise UpdateFailed(f"Error fetching album: {err}") from err

            rpc_error = data.get("error") if isinstance(data, dict) else None
            if isinstance(rpc_error, dict):
                err_msg = rpc_error.get("message", "Unknown error")
                last_error = err_msg
                _LOGGER.debug(
                    "publicalbum.org API error with params %s: %s (code: %s), "
                    "will retry with different params",
                    list(params.keys()),
                    err_msg,
                    rpc_error.get("code"),
                )
                continue

            return data

        raise UpdateFailed(
            f"publicalbum.org API error: {last_error or 'Unknown error'}"
        )

    async def _update_google_shared(self) -> dict[str, Any]:
        if not self.album_url:
            raise UpdateFailed("Missing album URL")

        session = async_get_clientsession(self.hass)

        # Try direct scrape first - paginates via Google's batchexecute RPC
        # to bypass publicalbum.org's ~300 cap.
        scraped_items: list[MediaItem] = []
        scraped_title: str | None = None
        try:
            from . import google_scraper

            scraped_title, scraped_items = await google_scraper.fetch_album(
                session, self.album_url
            )
        except Exception as err:  # never let scrape failure break the integration
            _LOGGER.debug(
                "Google shared album: scrape raised %s; falling back to publicalbum.org",
                err,
            )

        # Run publicalbum.org in parallel-ish: only call it if scrape was thin.
        # Threshold: if scrape returns >= 250 items we trust it; otherwise we
        # also call publicalbum.org and use whichever returns more.
        if len(scraped_items) >= 250:
            _LOGGER.info(
                "Album scraper: source=batchexecute items=%d",
                len(scraped_items),
            )
            return {
                "title": scraped_title or self.entry.title,
                "items": scraped_items,
            }

        try:
            data = await self._call_publicalbum(session, 500)
        except UpdateFailed:
            if scraped_items:
                _LOGGER.info(
                    "Album scraper: source=batchexecute items=%d (publicalbum.org failed)",
                    len(scraped_items),
                )
                return {
                    "title": scraped_title or self.entry.title,
                    "items": scraped_items,
                }
            raise

        result = data.get("result") if isinstance(data, dict) else {}
        if not isinstance(result, dict):
            result = {}

        page_items = _find_largest_item_list(result) or _find_largest_item_list(data)
        api_items: list[MediaItem] = []
        if page_items:
            seen_urls: set[str] = set()
            for raw in page_items:
                if _looks_like_video(raw):
                    continue

                url = _pick_url(raw)
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                filename = raw.get("filename") or raw.get("name")
                width = _pick_int(raw, "mediaMetadata", "width") or _pick_int(raw, "width")
                height = _pick_int(raw, "mediaMetadata", "height") or _pick_int(raw, "height")
                mime = raw.get("mimeType") if isinstance(raw.get("mimeType"), str) else None
                captured_at = _pick_timestamp_ms(
                    raw, "mediaMetadata", "creationTime"
                ) or _pick_timestamp_ms(raw, "creationTime")
                byte_size = _pick_int(raw, "fileSize") or _pick_int(raw, "size")

                api_items.append(MediaItem(
                    url=url, width=width, height=height,
                    mime_type=mime, filename=filename,
                    captured_at=captured_at,
                    uploaded_at=None,
                    byte_size=byte_size,
                ))

        # Pick the source with more items; prefer publicalbum.org on a tie
        # because its URLs come pre-decorated with size hints.
        if len(scraped_items) > len(api_items):
            _LOGGER.info(
                "Album scraper: source=batchexecute items=%d (publicalbum.org=%d)",
                len(scraped_items), len(api_items),
            )
            return {
                "title": scraped_title or result.get("title") or self.entry.title,
                "items": scraped_items,
            }

        if not api_items and not scraped_items:
            try:
                raw_json = json.dumps(data, default=str)
                _LOGGER.warning(
                    "Google shared album API returned no recognizable items. "
                    "Response (first 2000 chars): %s",
                    raw_json[:2000],
                )
            except Exception:
                _LOGGER.warning(
                    "Google shared album API returned no items. "
                    "Response keys: %s, result keys: %s",
                    list(data.keys()) if isinstance(data, dict) else type(data).__name__,
                    list(result.keys()) if isinstance(result, dict) else type(result).__name__,
                )
            raise UpdateFailed("No photos returned from Google shared album")

        _LOGGER.info(
            "Album scraper: source=publicalbum items=%d (batchexecute=%d)",
            len(api_items), len(scraped_items),
        )
        return {
            "title": result.get("title") or self.entry.title,
            "items": api_items,
        }
