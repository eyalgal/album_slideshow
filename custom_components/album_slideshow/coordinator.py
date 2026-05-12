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
    # GPS coordinates extracted from EXIF, when available.
    latitude: float | None = None
    longitude: float | None = None
    # Human-readable location from reverse geocoding, when available.
    location: str | None = None
    # True once _read_local_exif has been called for this item, so subsequent
    # coordinator refreshes can skip the executor hop for unchanged files.
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


def _gps_to_decimal(dms: Any, ref: Any) -> float | None:
    """Convert EXIF GPS DMS tuple + reference letter to a signed decimal degree."""
    if not dms or not ref:
        return None
    try:
        d, m, s = (float(x) for x in dms)
        val = d + m / 60.0 + s / 3600.0
        return round(-val if str(ref) in ("S", "W") else val, 6)
    except Exception:
        return None


def _read_local_exif(path: Path) -> tuple[int | None, float | None, float | None]:
    """Return (captured_at_ms, latitude, longitude) from JPEG EXIF data.

    EXIF DateTimeOriginal has no timezone; we treat it as UTC, consistent with
    how the rest of the codebase stores timestamps. Returns (None, None, None)
    for non-JPEG files or any parse failure.
    """
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return None, None, None
    try:
        from datetime import datetime, timezone
        from PIL import Image
        with Image.open(path) as img:
            exif = img.getexif()
        date_str = exif.get(36867) or exif.get(306)  # DateTimeOriginal, DateTime
        offset_str = exif.get(36881)  # OffsetTimeOriginal, e.g. "+02:00"
        captured_at: int | None = None
        if date_str:
            try:
                # EXIF DateTimeOriginal is local time. Prefer the paired
                # OffsetTimeOriginal tag when present; otherwise fall back to
                # the host's local timezone so the value is consistent with
                # what photo viewers display.
                if offset_str:
                    dt = datetime.strptime(
                        f"{str(date_str).strip()} {str(offset_str).strip()}",
                        "%Y:%m:%d %H:%M:%S %z",
                    )
                else:
                    dt = datetime.strptime(
                        str(date_str).strip(), "%Y:%m:%d %H:%M:%S"
                    ).astimezone()
                captured_at = int(dt.timestamp() * 1000)
            except ValueError:
                pass
        gps_ifd = exif.get_ifd(34853)
        lat = _gps_to_decimal(gps_ifd.get(2), gps_ifd.get(1))
        lon = _gps_to_decimal(gps_ifd.get(4), gps_ifd.get(3))
        return captured_at, lat, lon
    except Exception:
        return None, None, None


async def _nominatim_lookup(session: Any, lat: float, lon: float) -> str | None:
    """Reverse-geocode a coordinate via Nominatim and return 'city, state, country'."""
    url = (
        f"https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=json&zoom=10&addressdetails=1"
    )
    headers = {
        "User-Agent": "AlbumSlideshow/1.0 (https://github.com/eyalgal/album_slideshow)",
        "Accept-Language": "en",
    }
    try:
        async with async_timeout.timeout(10):
            resp = await session.get(url, headers=headers)
            if resp.status != 200:
                return None
            data = await resp.json(content_type=None)
        address = data.get("address", {})
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        parts = [city, address.get("state"), address.get("country")]
        return ", ".join(p for p in parts if p) or None
    except Exception:
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



class AlbumCoordinator(DataUpdateCoordinator):
    # Bump when the persisted item shape changes incompatibly.
    _ITEM_CACHE_VERSION = 1
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

        # Persist reverse-geocode results so Nominatim is not re-queried on restart.
        self._geocode_cache_store: Store = Store(
            hass,
            self._GEOCODE_CACHE_VERSION,
            f"{DOMAIN}.{entry.entry_id}.geocode",
        )
        self._geocode_cache: dict[str, str | None] = {}
        self._geocode_task: asyncio.Task | None = None
        self.exif_done: int = 0
        self.exif_total: int = 0
        self.geocode_done: int = 0
        self.geocode_total: int = 0
        self.geocode_complete: bool = False

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
        try:
            if self.provider == PROVIDER_LOCAL_FOLDER:
                # Capture previous data NOW — self.data is overwritten once
                # _async_update_data returns, so the background task would
                # see fresh (un-enriched) items if it read self.data instead.
                prior_data = self.data
                data = await self._update_local_folder()
                # Cancel any in-flight geocode task from a previous refresh, then
                # start a new one for the current item list.
                if self._geocode_task and not self._geocode_task.done():
                    self._geocode_task.cancel()
                self._geocode_task = self.hass.async_create_background_task(
                    self._enrich_items_background(data.get("items") or [], prior_data),
                    name="album_slideshow_enrich",
                )
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

    async def _enrich_items_background(
        self,
        items: list[MediaItem],
        prior_data: dict[str, Any] | None = None,
    ) -> None:
        """Read EXIF for each local file in the background, then geocode GPS coordinates.

        Runs one file at a time via the executor so the thread pool is never
        saturated, which is important for large NAS folders over a network.
        Pushes a state update every 100 files so attributes appear incrementally.

        ``prior_data`` must be the coordinator's data dict from the *previous*
        refresh tick, captured before self.data is overwritten. On restart it
        will be None, so we fall back to the on-disk items cache.
        """
        local_items = [it for it in items if it.url.startswith("file://")]
        self.exif_done = 0
        self.exif_total = len(local_items)

        # Build a URL-keyed index of previously enriched items. On an hourly
        # refresh, prior_data is the last coordinator tick (in memory). On a
        # cold restart, prior_data is None so we load from the disk cache.
        prior_items: list[MediaItem] = list((prior_data or {}).get("items") or [])
        if not prior_items:
            cached = await self._load_cached_items()
            prior_items = list((cached or {}).get("items") or [])
        prior: dict[str, MediaItem] = {it.url: it for it in prior_items}

        _STATE_BATCH = 100   # push HA state update every N files
        _SAVE_BATCH  = 1000  # write items cache to disk every N files
        for i, item in enumerate(local_items, start=1):
            cached = prior.get(item.url)
            if cached is not None and cached.exif_scanned:
                item.captured_at = cached.captured_at
                item.latitude = cached.latitude
                item.longitude = cached.longitude
                item.location = cached.location
                item.exif_scanned = True
                self.exif_done = i
                continue
            path = Path(item.url[len("file://"):])
            captured_at, lat, lon = await self.hass.async_add_executor_job(
                _read_local_exif, path
            )
            item.captured_at = captured_at
            item.latitude = lat
            item.longitude = lon
            item.exif_scanned = True
            self.exif_done = i
            if i % _STATE_BATCH == 0 and self.data is not None:
                self.async_set_updated_data(self.data)
            if i % _SAVE_BATCH == 0 and self.data is not None:
                # Persist progress so exif_scanned=True survives a mid-scan restart.
                try:
                    await self._save_cached_items(self.data)
                except Exception as err:
                    _LOGGER.debug("EXIF cache: failed to save (%s)", err)

        # Final push and persist after all EXIF is read.
        if self.data is not None:
            self.async_set_updated_data(self.data)
            try:
                await self._save_cached_items(self.data)
            except Exception as err:
                _LOGGER.debug("EXIF cache: failed to save (%s)", err)

        if self.entry.options.get(CONF_REVERSE_GEOCODE, True):
            await self._geocode_items_background(items)
        else:
            self.geocode_complete = False  # signal sensor to show "disabled"

    async def _geocode_items_background(self, items: list[MediaItem]) -> None:
        """Reverse-geocode local items that have GPS but no location. Background task."""
        # Load persistent cache into memory on first run.
        if not self._geocode_cache:
            try:
                saved = await self._geocode_cache_store.async_load()
                if isinstance(saved, dict):
                    self._geocode_cache.update(saved)
            except Exception:
                pass

        session = async_get_clientsession(self.hass)
        needs_lookup: list[MediaItem] = []
        cache_hits = 0
        for item in items:
            if item.latitude is None or item.longitude is None:
                continue
            key = f"{round(item.latitude, 3)},{round(item.longitude, 3)}"
            if key in self._geocode_cache:
                item.location = self._geocode_cache[key]
                cache_hits += 1
            else:
                needs_lookup.append(item)

        gps_total = cache_hits + len(needs_lookup)
        self.geocode_done = cache_hits
        self.geocode_total = gps_total
        self.geocode_complete = len(needs_lookup) == 0

        # Push cache-hit locations immediately so the UI reflects them without
        # waiting for any new Nominatim lookups to complete.
        if cache_hits and self.data is not None:
            self.async_set_updated_data(self.data)

        _BATCH_SIZE = 10  # push a state update every N new geocode results
        batch_count = 0
        for item in needs_lookup:
            key = f"{round(item.latitude, 3)},{round(item.longitude, 3)}"
            location = await _nominatim_lookup(session, item.latitude, item.longitude)
            item.location = location
            # Only cache successful lookups. Caching None would poison the key
            # forever and prevent retry for what is almost always a transient
            # failure (timeout / 5xx / 429).
            if location is not None:
                self._geocode_cache[key] = location
            _LOGGER.debug("Geocoded %s -> %s", item.filename, location)
            batch_count += 1
            self.geocode_done += 1
            if batch_count % _BATCH_SIZE == 0 and self.data is not None:
                self.async_set_updated_data(self.data)
                try:
                    await self._geocode_cache_store.async_save(dict(self._geocode_cache))
                except Exception as err:
                    _LOGGER.debug("Geocode cache: failed to save (%s)", err)
            # Nominatim rate limit: max 1 request per second.
            await asyncio.sleep(1.1)

        self.geocode_complete = True
        if self.data is not None:
            self.async_set_updated_data(self.data)

        # Persist both caches so the next restart has location pre-populated
        # on every item — no geocoding or Nominatim calls needed at all.
        try:
            await self._geocode_cache_store.async_save(dict(self._geocode_cache))
        except Exception as err:
            _LOGGER.debug("Geocode cache: failed to save (%s)", err)
        if self.data is not None:
            try:
                await self._save_cached_items(self.data)
            except Exception as err:
                _LOGGER.debug("Items cache: failed to save after geocoding (%s)", err)

    async def _update_local_folder(self) -> dict[str, Any]:
        if not self.local_path:
            raise UpdateFailed("Missing local path")

        root = Path(self.local_path)
        if not root.exists() or not root.is_dir():
            raise UpdateFailed("Local path does not exist or is not a directory")

        def _scan() -> list[Path]:
            it = root.rglob("*") if self.recursive else root.glob("*")
            files: list[tuple[Path, float]] = []
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
                files.append((p, p.stat().st_mtime))
            files.sort(key=lambda t: t[1], reverse=True)
            return [p for p, _ in files]

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
