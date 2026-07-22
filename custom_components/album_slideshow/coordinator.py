from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import json
import logging
from pathlib import Path
import re
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
    CONF_MEDIA_CONTENT_ID,
    CONF_RECURSIVE,
    CONF_REVERSE_GEOCODE,
    CONF_IMMICH_URL,
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_SELECTION_TYPE,
    CONF_IMMICH_SELECTION_ID,
    CONF_IMMICH_IMAGE_SIZE,
    CONF_IMMICH_FILTER,
    DEFAULT_IMMICH_IMAGE_SIZE,
    CONF_PHOTOPRISM_URL,
    CONF_PHOTOPRISM_AUTH_METHOD,
    CONF_PHOTOPRISM_TOKEN,
    CONF_PHOTOPRISM_USERNAME,
    CONF_PHOTOPRISM_PASSWORD,
    CONF_PHOTOPRISM_SELECTION_TYPE,
    CONF_PHOTOPRISM_SELECTION_ID,
    CONF_PHOTOPRISM_IMAGE_SIZE,
    CONF_PHOTOPRISM_FILTER,
    DEFAULT_PHOTOPRISM_IMAGE_SIZE,
    CONF_ICLOUD_TOKEN,
    CONF_ICLOUD_IMAGE_SIZE,
    DEFAULT_ICLOUD_IMAGE_SIZE,
    CONF_SYNOLOGY_URL,
    CONF_SYNOLOGY_USERNAME,
    CONF_SYNOLOGY_PASSWORD,
    CONF_SYNOLOGY_DEVICE_ID,
    CONF_SYNOLOGY_SPACE,
    CONF_SYNOLOGY_ALBUM_ID,
    CONF_SYNOLOGY_IMAGE_SIZE,
    DEFAULT_SYNOLOGY_IMAGE_SIZE,
    SYNOLOGY_SPACE_PERSONAL,
    CONF_NEXTCLOUD_URL,
    CONF_NEXTCLOUD_USERNAME,
    CONF_NEXTCLOUD_PASSWORD,
    CONF_NEXTCLOUD_FOLDER,
    CONF_NEXTCLOUD_RECURSIVE,
    CONF_NEXTCLOUD_IMAGE_SIZE,
    DEFAULT_NEXTCLOUD_IMAGE_SIZE,
    NEXTCLOUD_IMAGE_ORIGINAL,
    NEXTCLOUD_PREVIEW_PX,
    DEFAULT_REVERSE_GEOCODE,
    DOMAIN,
    PROVIDER_GOOGLE_SHARED,
    PROVIDER_LOCAL_FOLDER,
    PROVIDER_MEDIA_SOURCE,
    PROVIDER_IMMICH,
    PROVIDER_PHOTOPRISM,
    PROVIDER_ICLOUD,
    PROVIDER_SYNOLOGY,
    PROVIDER_NEXTCLOUD,
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
    # Free-text photo description / caption (local-folder provider only),
    # read from EXIF ImageDescription, IPTC Caption-Abstract, or XMP
    # dc:description. Used by the card's caption overlay when enabled.
    description: str | None = None
    # Provider-specific source identifier (e.g. the Immich asset id) used by
    # background enrichment to fetch per-item metadata.
    source_id: str | None = None
    # True once the local-folder EXIF reader has visited this file.
    # Prevents re-reading EXIF on every coordinator refresh and lets the
    # background enrichment task skip already-processed files even after
    # an HA restart (the flag round-trips through the items cache).
    exif_scanned: bool = False


_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}
_VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".mts", ".m2ts"}

# Media Source browsing limits: cap total collected images and recursion
# depth so a huge or self-referential tree can't hang the coordinator or
# exhaust memory.
_MEDIA_SOURCE_MAX_ITEMS = 5000
_MEDIA_SOURCE_MAX_DEPTH = 8

# System/metadata folders and files that some sources (notably Synology)
# expose but which are never user photos.
_SKIP_MEDIA_TITLES = {"@eadir", ".ds_store", "thumbs.db", "@syno", "#recycle"}
# Image formats a browser cannot render inline; skip so we don't queue
# guaranteed fetch failures.
_NON_WEB_IMAGE_EXTS = {
    ".psd", ".tif", ".tiff", ".heic", ".heif",
    ".cr2", ".nef", ".arw", ".dng", ".raw", ".orf", ".rw2",
}


def _is_junk_media_title(title: Any) -> bool:
    """Return True for system folders / non-renderable files to skip."""
    if not isinstance(title, str):
        return False
    t = title.strip().lower()
    if not t:
        return False
    if t in _SKIP_MEDIA_TITLES:
        return True
    if t.startswith("@") or t.startswith("."):
        return True
    dot = t.rfind(".")
    if dot != -1 and t[dot:] in _NON_WEB_IMAGE_EXTS:
        return True
    return False


def _media_node_is_image(media_class: Any, media_content_type: Any) -> bool:
    """Return True if a browsed media node looks like a still image.

    ``media_class`` is Home Assistant's coarse category (e.g. ``image``,
    ``video``, ``directory``); ``media_content_type`` is the MIME type when
    known. We accept a node when either signals an image, and explicitly
    reject anything that declares a video type.
    """
    mc = str(media_class or "").lower()
    mime = str(media_content_type or "").lower()
    if mc == "video" or mime.startswith("video/"):
        return False
    if mc == "image" or mime.startswith("image/"):
        return True
    return False


def _normalize_resolved_url(url: str, base_url: str) -> str:
    """Make a resolved media URL absolute so the fetch layer can load it.

    ``async_resolve_media`` returns either an absolute ``http(s)`` URL or a
    site-relative path such as ``/media/local/...`` (often already signed via
    an ``authSig`` query param). Relative paths are prefixed with the
    instance's internal base URL; absolute URLs pass through unchanged.
    """
    if not isinstance(url, str) or not url:
        return url
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/") and base_url:
        return f"{base_url.rstrip('/')}{url}"
    return url

_SKIP_DIR_PREFIXES = (".", "@", "#")


def _pick_url(item: dict[str, Any]) -> str | None:
    for key in ("baseUrl", "url", "downloadUrl", "productUrl"):
        v = item.get(key)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


# Matches Google's ``=w1920-h1080`` (and variants) size suffix so two URLs for
# the same photo at different sizes collapse to one stable key.
_PHOTO_SIZE_SUFFIX_RE = re.compile(r"=[a-z0-9-]+$", re.IGNORECASE)


def _photo_base_key(url: str | None) -> str | None:
    """Return a stable per-photo key from a Google CDN URL.

    Both album sources (``batchexecute`` and publicalbum.org) hand back
    ``lh3.googleusercontent.com/<id>=w...-h...`` URLs for the same photo, just
    at different sizes and sometimes with query params. Dropping the query
    string and the size suffix leaves the shared ``<id>`` portion, which lets
    us match a publicalbum item to its dated batchexecute twin.
    """
    if not isinstance(url, str) or not url:
        return None
    base = url.split("?", 1)[0]
    base = _PHOTO_SIZE_SUFFIX_RE.sub("", base)
    return base or None


def _enrich_missing_dates(
    api_items: list["MediaItem"], scraped_items: list["MediaItem"]
) -> int:
    """Backfill missing dates on ``api_items`` from dated ``scraped_items``.

    Matches photos across the two Google album sources by their stable
    per-photo URL key and fills in any ``captured_at`` / ``uploaded_at`` that
    the publicalbum item is missing. Mutates ``api_items`` in place and returns
    how many items were touched. See issue #18.
    """
    if not api_items or not scraped_items:
        return 0
    dates_by_key: dict[str, tuple[int | None, int | None]] = {}
    for it in scraped_items:
        key = _photo_base_key(it.url)
        if key and (it.captured_at is not None or it.uploaded_at is not None):
            dates_by_key.setdefault(key, (it.captured_at, it.uploaded_at))
    if not dates_by_key:
        return 0
    enriched = 0
    for it in api_items:
        if it.captured_at is not None and it.uploaded_at is not None:
            continue
        twin = dates_by_key.get(_photo_base_key(it.url))
        if not twin:
            continue
        cap, up = twin
        touched = False
        if it.captured_at is None and cap is not None:
            it.captured_at = cap
            touched = True
        if it.uploaded_at is None and up is not None:
            it.uploaded_at = up
            touched = True
        if touched:
            enriched += 1
    return enriched


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
_EXIF_TAG_IMAGE_DESCRIPTION = 270         # ImageDescription (free text)
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

# Cap a single Nextcloud enrichment download. EXIF/IPTC/XMP live in the first
# blocks of the file, but we read the whole thing since Pillow needs a complete
# image; this bounds memory for pathological files. 64 MB matches the camera.
_NEXTCLOUD_ENRICH_MAX_BYTES = 64 * 1024 * 1024

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


def _clean_description(value: Any) -> str | None:
    """Normalise a raw description value to trimmed text or ``None``."""
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="ignore")
        except Exception:
            return None
    if not isinstance(value, str):
        return None
    text = value.strip().replace("\x00", "")
    return text or None


def _read_photo_description(img: Any, exif: Any) -> str | None:
    """Extract a photo description from EXIF, IPTC, or XMP metadata.

    Tries, in order: EXIF ``ImageDescription``, IPTC ``Caption-Abstract``
    (record 2, dataset 120), and XMP ``dc:description``. Returns the first
    non-empty value, or ``None``. All lookups are defensive because these
    metadata blocks are frequently absent or malformed.
    """
    try:
        desc = _clean_description(exif.get(_EXIF_TAG_IMAGE_DESCRIPTION))
        if desc:
            return desc
    except Exception:
        pass

    try:
        from PIL import IptcImagePlugin

        iptc = IptcImagePlugin.getiptcinfo(img)
        if iptc:
            desc = _clean_description(iptc.get((2, 120)))
            if desc:
                return desc
    except Exception:
        pass

    try:
        xmp = img.getxmp()
    except Exception:
        xmp = None
    if isinstance(xmp, dict):
        found = _find_xmp_description(xmp)
        if found:
            return found

    return None


def _find_xmp_description(node: Any) -> str | None:
    """Recursively search a parsed XMP tree for the ``dc:description`` text.

    Pillow's ``getxmp()`` returns nested dicts. The relevant path is
    ``xmpmeta -> RDF -> Description -> description -> Alt -> li``. We match
    the ``dc:description`` *field* by its exact lowercase localname
    ``description`` (case-sensitive) so we don't accidentally match the
    ``rdf:Description`` *container* (capital ``D``) that wraps it.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            # Case-sensitive: dc:description is lowercase; the rdf:Description
            # container is capitalised and must be skipped here.
            if isinstance(key, str) and key.split(":")[-1] == "description":
                text = _extract_xmp_text(value)
                if text:
                    return text
        for value in node.values():
            found = _find_xmp_description(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_xmp_description(value)
            if found:
                return found
    return None


def _extract_xmp_text(value: Any) -> str | None:
    """Pull plain text out of an XMP language-alternative structure.

    Handles the shapes Pillow produces for ``dc:description``:
      - a plain string
      - ``{"Alt"/"Bag"/"Seq": {"li": ...}}`` containers
      - an ``li`` that is a string, a ``{"lang", "text"}`` dict, or a list of
        such dicts (multiple languages)
    Prefers the ``x-default`` language entry when several are present.
    """
    if isinstance(value, str):
        return _clean_description(value)
    if isinstance(value, dict):
        # A language-alternative leaf: ``{"lang": ..., "text": ...}``.
        if "text" in value:
            return _clean_description(value.get("text"))
        # Container wrappers: descend only through the known XMP keys so we
        # never grab a sibling attribute value (e.g. a ``lang`` code).
        for container in ("Alt", "Bag", "Seq"):
            if container in value:
                text = _extract_xmp_text(value[container])
                if text:
                    return text
        if "li" in value:
            return _extract_xmp_text(value["li"])
        return None
    if isinstance(value, list):
        # Multiple ``rdf:li`` entries: prefer x-default, else the first
        # non-empty one.
        default = None
        first = None
        for v in value:
            if isinstance(v, dict) and v.get("lang") == "x-default":
                default = _clean_description(v.get("text"))
                if default:
                    return default
            if first is None:
                first = _extract_xmp_text(v)
        return first
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
            _read_exif_from_image(img, out)
    except Exception as err:
        _LOGGER.debug("EXIF: failed to read %s: %s", path, err)

    return out


def _read_exif_from_image(img: Any, out: dict[str, Any]) -> None:
    """Fill ``out`` with capture date / description / GPS from an open image.

    Shared by ``_read_local_exif`` (opens from a filesystem path) and
    ``_read_exif_from_bytes`` (opens from downloaded bytes, e.g. the
    Nextcloud provider) - both hand this an already-``Image.open``'d image
    plus a dict pre-seeded with a fallback ``captured_at``.
    """
    exif = img.getexif()

    if exif:
        dt_raw = exif.get(_EXIF_TAG_DATETIME_ORIGINAL) or exif.get(
            _EXIF_TAG_DATETIME
        )
        offset_raw = exif.get(_EXIF_TAG_OFFSET_TIME_ORIGINAL)
        parsed = _parse_exif_datetime(dt_raw, offset_raw)
        if parsed is not None:
            out["captured_at"] = parsed

    # Description can come from IPTC / XMP even when the file has no EXIF
    # IFD, so this runs regardless of ``exif`` being present.
    description = _read_photo_description(img, exif)
    if description:
        out["description"] = description

    if not exif:
        return

    gps = None
    try:
        gps = exif.get_ifd(_EXIF_TAG_GPS_IFD) or None
    except Exception:
        gps = None
    if gps:
        lat = _gps_to_decimal(gps.get(_EXIF_GPS_LAT), gps.get(_EXIF_GPS_LAT_REF))
        lon = _gps_to_decimal(gps.get(_EXIF_GPS_LON), gps.get(_EXIF_GPS_LON_REF))
        if lat is not None and lon is not None:
            # Null Island guard: GPS chips and some editors stamp ``(0, 0)``
            # when the fix is invalid. Treat that as no location rather than
            # dropping every such photo onto the equator off the African coast.
            if abs(lat) < 1e-6 and abs(lon) < 1e-6:
                return
            out["latitude"] = lat
            out["longitude"] = lon


def _read_exif_from_bytes(
    data: bytes, mtime_fallback_ms: int | None
) -> dict[str, Any]:
    """Read EXIF metadata from already-downloaded image bytes.

    Same return shape as ``_read_local_exif``, for providers (Nextcloud)
    whose files live on a remote server rather than the local filesystem -
    the caller downloads the file once for enrichment, regardless of which
    quality is used for display. ``mtime_fallback_ms`` takes the place of
    the filesystem mtime fallback (e.g. the WebDAV ``Last-Modified`` date).
    """
    out: dict[str, Any] = {}
    if isinstance(mtime_fallback_ms, int):
        out["captured_at"] = mtime_fallback_ms

    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow ships with HA core
        return out

    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            _read_exif_from_image(img, out)
    except Exception as err:
        _LOGGER.debug("EXIF: failed to read image bytes: %s", err)

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
        if prev.description and not item.description:
            item.description = prev.description
        if prev.exif_scanned:
            item.exif_scanned = True


class AlbumCoordinator(DataUpdateCoordinator):
    # Bump when the persisted item shape changes incompatibly.
    # v3: added ``description``; forces a re-scan so already-cached items
    # (exif_scanned=True) get their description read instead of being
    # skipped forever.
    _ITEM_CACHE_VERSION = 3
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
        self.media_content_id: str | None = entry.data.get(CONF_MEDIA_CONTENT_ID)
        # Extra headers the camera must send when fetching image bytes
        # (Immich API key). Empty for providers that need no auth.
        self.image_request_headers: dict[str, str] = {}

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
            elif self.provider == PROVIDER_MEDIA_SOURCE:
                data = await self._update_media_source()
            elif self.provider == PROVIDER_IMMICH:
                data = await self._update_immich()
            elif self.provider == PROVIDER_PHOTOPRISM:
                data = await self._update_photoprism()
            elif self.provider == PROVIDER_ICLOUD:
                data = await self._update_icloud()
            elif self.provider == PROVIDER_SYNOLOGY:
                data = await self._update_synology()
            elif self.provider == PROVIDER_NEXTCLOUD:
                data = await self._update_nextcloud()
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
        if self.provider in (PROVIDER_LOCAL_FOLDER, PROVIDER_IMMICH, PROVIDER_NEXTCLOUD) and items:
            # Carry forward EXIF/geocode metadata for items we've already
            # scanned this session; new items get filled in by the
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
                    description=raw.get("description"),
                    source_id=raw.get("source_id"),
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
                    "description": it.description,
                    "source_id": it.source_id,
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

    async def _update_media_source(self) -> dict[str, Any]:
        """Build the item list from a Home Assistant Media Source node.

        Browses the configured ``media-source://`` content id recursively,
        collecting image children, then resolves each to a playable URL.
        Media Source exposes no per-photo EXIF, so date/GPS/description
        features do not apply here (same as the Google provider).
        """
        content_id = self.media_content_id
        if not content_id:
            raise UpdateFailed("Missing media source content id")

        try:
            from homeassistant.components import media_source
        except Exception as err:  # pragma: no cover - core ships media_source
            raise UpdateFailed("media_source integration is not available") from err

        collected: list[tuple[str, str | None]] = []
        try:
            await self._browse_media_source(media_source, content_id, collected, 0)
        except UpdateFailed:
            raise
        except Exception as err:
            raise UpdateFailed(f"Error browsing media source: {err}") from err

        if not collected:
            raise UpdateFailed("No images found in the selected media source")

        base_url = self._internal_base_url()
        items: list[MediaItem] = []
        for cid, title in collected:
            resolved = await self._resolve_media(media_source, cid)
            if resolved is None:
                continue
            raw = resolved[0]
            # ``async_resolve_media`` returns an UNSIGNED ``/media/...`` path
            # when called directly (the frontend's websocket handler is what
            # normally signs it). Fetching an unsigned local-media path
            # server-side gets a 401, so sign it ourselves before use.
            if isinstance(raw, str) and raw.startswith("/"):
                raw = self._sign_media_path(raw)
            url = _normalize_resolved_url(raw, base_url)
            if not url:
                continue
            items.append(
                MediaItem(
                    url=url,
                    width=None,
                    height=None,
                    mime_type=resolved[1],
                    filename=title,
                )
            )

        if not items:
            raise UpdateFailed("Could not resolve any media source images")

        return {
            "title": self.entry.title,
            "items": items,
        }

    def _sign_media_path(self, path: str) -> str:
        """Sign a relative ``/media/...`` path so it can be fetched server-side.

        Mirrors what Home Assistant's ``media_source/resolve_media`` websocket
        handler does: quote the path and append an ``authSig`` signature. Uses
        the content user so signing works from a background task with no
        request context (the same mechanism that lets Cast devices fetch local
        media). Signs for a window comfortably longer than the album refresh
        interval; every refresh re-signs, so URLs stay fresh.
        """
        from urllib.parse import quote

        try:
            from homeassistant.components.http.auth import async_sign_path
        except Exception:  # pragma: no cover - http always present
            return path

        quoted = quote(path)
        expiration = timedelta(hours=max(48, int(self.store.refresh_hours) * 2 + 1))
        for kwargs in ({"use_content_user": True}, {}):
            try:
                return async_sign_path(self.hass, quoted, expiration, **kwargs)
            except TypeError:
                # Older/newer signature without ``use_content_user``.
                continue
            except Exception as err:
                _LOGGER.debug("media_source: failed to sign %s: %s", path, err)
                return path
        return path

    async def _browse_media_source(
        self,
        media_source,
        content_id: str,
        collected: list[tuple[str, str | None]],
        depth: int,
    ) -> None:
        """Recursively walk a media source tree collecting image leaves."""
        if len(collected) >= _MEDIA_SOURCE_MAX_ITEMS or depth > _MEDIA_SOURCE_MAX_DEPTH:
            return
        browsed = await media_source.async_browse_media(self.hass, content_id)
        children = getattr(browsed, "children", None) or []
        for child in children:
            if len(collected) >= _MEDIA_SOURCE_MAX_ITEMS:
                break
            child_id = getattr(child, "media_content_id", None)
            if not child_id:
                continue
            title = getattr(child, "title", None)
            if _is_junk_media_title(title):
                continue
            if _media_node_is_image(
                getattr(child, "media_class", None),
                getattr(child, "media_content_type", None),
            ):
                collected.append((child_id, title))
            elif getattr(child, "can_expand", False):
                await self._browse_media_source(
                    media_source, child_id, collected, depth + 1
                )

    async def _resolve_media(
        self, media_source, content_id: str
    ) -> tuple[str, str | None] | None:
        """Resolve a media content id to ``(url, mime_type)`` or ``None``."""
        try:
            try:
                play = await media_source.async_resolve_media(
                    self.hass, content_id, None
                )
            except TypeError:
                # Older cores: async_resolve_media(hass, content_id).
                play = await media_source.async_resolve_media(self.hass, content_id)
        except Exception as err:
            _LOGGER.debug("media_source: failed to resolve %s: %s", content_id, err)
            return None
        url = getattr(play, "url", None)
        if not url:
            return None
        return url, getattr(play, "mime_type", None)

    def _internal_base_url(self) -> str:
        """Best-effort internal base URL for site-relative media URLs."""
        try:
            from homeassistant.helpers.network import get_url

            return get_url(self.hass, prefer_external=False, allow_ip=True)
        except Exception:
            return ""

    async def _update_immich(self) -> dict[str, Any]:
        """Build the item list from an Immich album or person via its API.

        Unlike Media Source, the Immich API exposes per-photo metadata, so
        capture date, GPS/location, and description all work. Dates come from
        the asset list up front; location and description are filled in by the
        background enrichment worker (one asset-detail call each, cached).
        """
        from . import immich as immich_api

        url = self.entry.data.get(CONF_IMMICH_URL)
        api_key = self.entry.data.get(CONF_IMMICH_API_KEY)
        sel_type = self.entry.data.get(CONF_IMMICH_SELECTION_TYPE)
        sel_id = self.entry.data.get(CONF_IMMICH_SELECTION_ID)
        size = self.entry.data.get(CONF_IMMICH_IMAGE_SIZE, DEFAULT_IMMICH_IMAGE_SIZE)
        if not url or not api_key or not sel_type:
            raise UpdateFailed("Immich provider is missing URL, API key, or selection")

        # ``album``/``albums`` and ``person``/``people`` need a target id;
        # composite/favorites/all/random/search do not (an empty composite
        # means "all photos").
        if sel_type in ("album", "albums", "person", "people") and not sel_id:
            raise UpdateFailed("Immich provider is missing the album/person id")

        filter_body = None
        raw_filter = self.entry.data.get(CONF_IMMICH_FILTER)
        if sel_type in ("search", "composite") and raw_filter:
            try:
                parsed = json.loads(raw_filter)
                if isinstance(parsed, dict):
                    filter_body = parsed
            except (ValueError, TypeError):
                raise UpdateFailed("Immich search filter is not valid JSON")

        client = immich_api.ImmichClient(self.hass, url, api_key)
        # Auth header the camera must send when fetching image bytes. Sent
        # server-side only, so the key never reaches the browser or the
        # ``current_url`` attribute.
        self.image_request_headers = dict(client.image_headers)

        try:
            assets = await client.async_collect_assets(sel_type, sel_id, filter_body)
        except Exception as err:
            raise UpdateFailed(f"Error querying Immich: {err}") from err

        if not assets:
            raise UpdateFailed("No images found for the selected Immich source")

        items: list[MediaItem] = []
        for a in assets:
            aid = a.get("id")
            if not aid:
                continue
            captured = immich_api._to_epoch_ms(
                a.get("localDateTime")
            ) or immich_api._to_epoch_ms(a.get("fileCreatedAt"))
            w = a.get("width")
            h = a.get("height")
            items.append(
                MediaItem(
                    url=immich_api.build_image_url(client.base_url, aid, size),
                    width=w if isinstance(w, int) else None,
                    height=h if isinstance(h, int) else None,
                    mime_type=None,
                    filename=a.get("originalFileName"),
                    captured_at=captured,
                    source_id=aid,
                )
            )

        return {
            "title": self.entry.title,
            "items": items,
        }

    async def _update_photoprism(self) -> dict[str, Any]:
        """Fetch photos from PhotoPrism via its REST API.

        Unlike Immich, PhotoPrism returns per-photo metadata inline in the
        search response, so every ``MediaItem`` is built fully here - there is
        no background enrichment pass. Thumbnails carry a preview token in the
        URL, so no auth header is needed to fetch the image bytes.
        """
        from . import photoprism as pp_api

        url = self.entry.data.get(CONF_PHOTOPRISM_URL)
        auth_method = self.entry.data.get(CONF_PHOTOPRISM_AUTH_METHOD)
        sel_type = self.entry.data.get(CONF_PHOTOPRISM_SELECTION_TYPE)
        sel_id = self.entry.data.get(CONF_PHOTOPRISM_SELECTION_ID)
        size = self.entry.data.get(
            CONF_PHOTOPRISM_IMAGE_SIZE, DEFAULT_PHOTOPRISM_IMAGE_SIZE
        )
        filter_query = self.entry.data.get(CONF_PHOTOPRISM_FILTER)
        if not url or not auth_method or not sel_type:
            raise UpdateFailed("PhotoPrism provider is missing URL, auth, or selection")

        client = pp_api.PhotoprismClient(
            self.hass,
            url,
            auth_method=auth_method,
            token=self.entry.data.get(CONF_PHOTOPRISM_TOKEN),
            username=self.entry.data.get(CONF_PHOTOPRISM_USERNAME),
            password=self.entry.data.get(CONF_PHOTOPRISM_PASSWORD),
        )

        try:
            photos = await client.async_collect_assets(sel_type, sel_id, filter_query)
        except Exception as err:
            raise UpdateFailed(f"Error querying PhotoPrism: {err}") from err

        if not photos:
            raise UpdateFailed("No images found for the selected PhotoPrism source")

        token = client.preview_token
        if not token:
            raise UpdateFailed("PhotoPrism did not return a preview token")

        items: list[MediaItem] = []
        for p in photos:
            uid = p.get("UID")
            file_hash = p.get("Hash")
            if not uid or not file_hash:
                continue
            meta = pp_api.parse_photo_meta(p)
            w = p.get("Width")
            h = p.get("Height")
            items.append(
                MediaItem(
                    url=pp_api.build_image_url(client.base_url, file_hash, token, size),
                    width=w if isinstance(w, int) else None,
                    height=h if isinstance(h, int) else None,
                    mime_type=None,
                    filename=p.get("FileName") or p.get("Name"),
                    captured_at=meta.get("captured_at"),
                    latitude=meta.get("latitude"),
                    longitude=meta.get("longitude"),
                    location=meta.get("location"),
                    description=meta.get("description"),
                    source_id=uid,
                    exif_scanned=True,
                )
            )

        return {
            "title": self.entry.title,
            "items": items,
        }

    async def _update_icloud(self) -> dict[str, Any]:
        """Fetch photos from a public iCloud Shared Album.

        The webstream response carries capture date and caption inline, so
        there is no enrichment pass. Signed image URLs are resolved up front
        and expire after roughly a day, so they are refreshed on every album
        refresh (like Google Photos).
        """
        from . import icloud as icloud_api

        token = self.entry.data.get(CONF_ICLOUD_TOKEN)
        size = self.entry.data.get(CONF_ICLOUD_IMAGE_SIZE, DEFAULT_ICLOUD_IMAGE_SIZE)
        if not token:
            raise UpdateFailed("iCloud provider is missing the album token")

        client = icloud_api.IcloudClient(self.hass, token)
        try:
            photos = await client.async_get_photos()
            asset_urls = await client.async_get_asset_urls(
                [p["photoGuid"] for p in photos if p.get("photoGuid")]
            )
        except Exception as err:
            raise UpdateFailed(f"Error querying iCloud album: {err}") from err

        if not photos:
            raise UpdateFailed("No images found in the iCloud album")

        items: list[MediaItem] = []
        for p in photos:
            guid = p.get("photoGuid")
            checksum = icloud_api.pick_checksum(p, size)
            if not guid or not checksum:
                continue
            url = icloud_api.build_image_url(asset_urls.get(checksum))
            if not url:
                continue
            meta = icloud_api.parse_photo_meta(p)
            w = p.get("width")
            h = p.get("height")
            items.append(
                MediaItem(
                    url=url,
                    width=int(w) if str(w).isdigit() else None,
                    height=int(h) if str(h).isdigit() else None,
                    mime_type=None,
                    filename=None,
                    captured_at=meta.get("captured_at"),
                    description=meta.get("description"),
                    source_id=guid,
                    exif_scanned=True,
                )
            )

        if not items:
            raise UpdateFailed("Could not resolve any iCloud image URLs")

        return {
            "title": self.entry.title,
            "items": items,
        }

    async def _update_synology(self) -> dict[str, Any]:
        """Fetch photos from a Synology Photos library via its web API.

        Metadata (capture date, GPS, address, description) is returned inline
        with each item, so every ``MediaItem`` is built fully here - there is
        no background enrichment pass. Thumbnail URLs carry no SID; the session
        cookie is stored on the coordinator and sent server-side by the camera
        (like the Immich x-api-key), so the SID never reaches the browser.
        """
        from . import synology as syn_api

        url = self.entry.data.get(CONF_SYNOLOGY_URL)
        username = self.entry.data.get(CONF_SYNOLOGY_USERNAME)
        password = self.entry.data.get(CONF_SYNOLOGY_PASSWORD)
        device_id = self.entry.data.get(CONF_SYNOLOGY_DEVICE_ID)
        space = self.entry.data.get(CONF_SYNOLOGY_SPACE, SYNOLOGY_SPACE_PERSONAL)
        album_id = self.entry.data.get(CONF_SYNOLOGY_ALBUM_ID)
        size = self.entry.data.get(
            CONF_SYNOLOGY_IMAGE_SIZE, DEFAULT_SYNOLOGY_IMAGE_SIZE
        )
        if not url or not username or not password:
            raise UpdateFailed("Synology provider is missing URL or credentials")

        client = syn_api.SynologyClient(
            self.hass,
            url,
            username=username,
            password=password,
            device_id=device_id,
            space=space,
        )
        try:
            await client.async_login()
            photos = await client.async_collect_assets(album_id or None)
        except Exception as err:
            raise UpdateFailed(f"Error querying Synology Photos: {err}") from err

        if not photos:
            await client.async_logout()
            raise UpdateFailed("No images found for the selected Synology source")

        # Store the session cookie so the camera can fetch thumbnail bytes
        # server-side. Do not log out: the SID must stay valid until the next
        # refresh re-authenticates.
        self.image_request_headers = dict(client.image_headers)

        items: list[MediaItem] = []
        for p in photos:
            ref = syn_api.thumbnail_ref(p)
            if not ref:
                continue
            unit_id, cache_key = ref
            meta = syn_api.parse_photo_meta(p)
            items.append(
                MediaItem(
                    url=syn_api.build_thumbnail_url(
                        client.base_url, unit_id, cache_key, size, space
                    ),
                    width=meta.get("width"),
                    height=meta.get("height"),
                    mime_type=None,
                    filename=p.get("filename"),
                    captured_at=meta.get("captured_at"),
                    byte_size=meta.get("byte_size"),
                    latitude=meta.get("latitude"),
                    longitude=meta.get("longitude"),
                    location=meta.get("location"),
                    description=meta.get("description"),
                    source_id=str(p.get("id")) if p.get("id") is not None else None,
                    exif_scanned=True,
                )
            )

        if not items:
            raise UpdateFailed("Could not resolve any Synology images")

        return {
            "title": self.entry.title,
            "items": items,
        }

    async def _update_nextcloud(self) -> dict[str, Any]:
        """List photos from an authenticated Nextcloud WebDAV folder.

        The PROPFIND listing carries filename/size/content-type/mtime but no
        EXIF, so capture date, GPS and description are filled in afterwards by
        the background enrichment worker (one original-file download per photo -
        Nextcloud has no metadata-only endpoint the way Immich does). The app
        password is sent server-side only via the coordinator's image headers.
        """
        from . import nextcloud as nc_api

        url = self.entry.data.get(CONF_NEXTCLOUD_URL)
        username = self.entry.data.get(CONF_NEXTCLOUD_USERNAME)
        password = self.entry.data.get(CONF_NEXTCLOUD_PASSWORD)
        folder = self.entry.data.get(CONF_NEXTCLOUD_FOLDER) or ""
        recursive = bool(self.entry.data.get(CONF_NEXTCLOUD_RECURSIVE, False))
        size = self.entry.data.get(
            CONF_NEXTCLOUD_IMAGE_SIZE, DEFAULT_NEXTCLOUD_IMAGE_SIZE
        )
        if not url or not username or not password:
            raise UpdateFailed("Nextcloud provider is missing URL or credentials")

        client = nc_api.NextcloudClient(self.hass, url, username, password, folder)
        try:
            photos = await client.async_list_photos(recursive=recursive)
        except Exception as err:
            raise UpdateFailed(f"Error listing Nextcloud folder: {err}") from err

        if not photos:
            raise UpdateFailed("No images found in the Nextcloud folder")

        # The camera fetches image bytes server-side with this Basic-auth
        # header, so the app password never appears in the browser URL.
        self.image_request_headers = dict(client.image_headers)

        items: list[MediaItem] = []
        for p in photos:
            href = p.get("href")
            if not href:
                continue
            if size != NEXTCLOUD_IMAGE_ORIGINAL and p.get("file_id"):
                display_url = nc_api.build_preview_url(
                    client.base_url, p["file_id"], NEXTCLOUD_PREVIEW_PX
                )
            else:
                display_url = href
            items.append(
                MediaItem(
                    url=display_url,
                    width=None,
                    height=None,
                    mime_type=p.get("content_type"),
                    filename=p.get("filename"),
                    uploaded_at=p.get("mtime_ms"),
                    byte_size=p.get("size"),
                    source_id=p.get("file_id") or href,
                )
            )

        return {
            "title": self.entry.title,
            "items": items,
        }

    async def _enrich_nextcloud_item(self, item: MediaItem) -> None:
        """Download one Nextcloud photo's original bytes and read its EXIF.

        Nextcloud's WebDAV folder has no metadata-only endpoint (unlike
        Immich's per-asset detail call), so enrichment costs one full-file
        download per photo regardless of the display quality configured.
        """
        from . import nextcloud as nc_api
        from urllib.parse import quote

        url = self.entry.data.get(CONF_NEXTCLOUD_URL)
        username = self.entry.data.get(CONF_NEXTCLOUD_USERNAME)
        password = self.entry.data.get(CONF_NEXTCLOUD_PASSWORD)
        folder = self.entry.data.get(CONF_NEXTCLOUD_FOLDER) or ""
        if not username or not password or not url:
            item.exif_scanned = True
            return

        # Reconstruct the original-file URL: for preview items the display url
        # is the preview endpoint, so fall back to the folder href by filename.
        original_url = None
        if isinstance(item.url, str) and "/remote.php/dav/files/" in item.url:
            original_url = item.url
        elif item.filename:
            client = nc_api.NextcloudClient(self.hass, url, username, password, folder)
            original_url = client.dav_root + quote(item.filename)
        if not original_url:
            item.exif_scanned = True
            return

        headers = {
            "Authorization": nc_api.basic_auth_header(username, password)
        }
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(30):
                async with session.get(original_url, headers=headers) as resp:
                    resp.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in resp.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > _NEXTCLOUD_ENRICH_MAX_BYTES:
                            _LOGGER.debug(
                                "Nextcloud: %s exceeded %d byte enrichment cap; skipping",
                                item.filename, _NEXTCLOUD_ENRICH_MAX_BYTES,
                            )
                            item.exif_scanned = True
                            return
                        chunks.append(chunk)
                    data = b"".join(chunks)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug(
                "Nextcloud: failed to download %s for enrichment: %s",
                item.filename, err,
            )
            item.exif_scanned = True
            return

        info = await self.hass.async_add_executor_job(
            _read_exif_from_bytes, data, item.uploaded_at
        )
        if "captured_at" in info:
            item.captured_at = info["captured_at"]
        if "description" in info:
            item.description = info["description"]
        if "latitude" in info and "longitude" in info:
            item.latitude = info["latitude"]
            item.longitude = info["longitude"]
        item.exif_scanned = True

    async def _enrich_immich_item(self, item: MediaItem) -> None:
        """Fetch one Immich asset's detail and fill location/description."""
        from . import immich as immich_api

        url = self.entry.data.get(CONF_IMMICH_URL)
        api_key = self.entry.data.get(CONF_IMMICH_API_KEY)
        if not item.source_id or not url or not api_key:
            item.exif_scanned = True
            return
        client = immich_api.ImmichClient(self.hass, url, api_key)
        try:
            asset = await client.async_get_asset(item.source_id)
        except Exception as err:
            _LOGGER.debug("Immich: failed to fetch asset %s: %s", item.source_id, err)
            item.exif_scanned = True
            return
        info = immich_api.parse_asset_exif(asset)
        if "captured_at" in info:
            item.captured_at = info["captured_at"]
        if "latitude" in info and "longitude" in info:
            item.latitude = info["latitude"]
            item.longitude = info["longitude"]
        if "location" in info:
            item.location = info["location"]
        if "description" in info:
            item.description = info["description"]
        item.exif_scanned = True

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

                if self.provider == PROVIDER_IMMICH:
                    try:
                        await self._enrich_immich_item(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Immich enrich error: %s", err)
                        item.exif_scanned = True
                    scanned_since_save += 1
                    self._enrich_progress["exif_done"] = (
                        self._enrich_progress.get("exif_done", 0) + 1
                    )
                    if scanned_since_save >= _EXIF_BATCH_SAVE:
                        scanned_since_save = 0
                        await self._save_cached_items(data)
                        self.async_set_updated_data(data)
                    continue

                if self.provider == PROVIDER_NEXTCLOUD:
                    try:
                        await self._enrich_nextcloud_item(item)
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("Nextcloud enrich error: %s", err)
                        item.exif_scanned = True
                    scanned_since_save += 1
                    self._enrich_progress["exif_done"] = (
                        self._enrich_progress.get("exif_done", 0) + 1
                    )
                    if scanned_since_save >= _EXIF_BATCH_SAVE:
                        scanned_since_save = 0
                        await self._save_cached_items(data)
                        self.async_set_updated_data(data)
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
                if "description" in info:
                    item.description = info["description"]
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

        # Cross-source date enrichment: publicalbum.org often returns a fuller
        # item list but with no (or partial) date metadata, while batchexecute
        # returns dated items. Where the same photo appears in both, backfill
        # the publicalbum item's captured_at / uploaded_at from its dated
        # batchexecute twin (matched by the stable per-photo URL key). This
        # keeps the larger item count while restoring the dates the date
        # filter needs. See issue #18.
        enriched = _enrich_missing_dates(api_items, scraped_items)
        if enriched:
            _LOGGER.info(
                "Album scraper: enriched %d publicalbum item(s) with "
                "batchexecute dates",
                enriched,
            )

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
