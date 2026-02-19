from __future__ import annotations

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
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    PUBLICALBUM_ENDPOINT,
    CONF_PROVIDER,
    CONF_ALBUM_URL,
    CONF_LOCAL_PATH,
    CONF_RECURSIVE,
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
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.hass = hass
        self.entry = entry
        self.store = store

        self.provider: str = entry.data.get(CONF_PROVIDER, PROVIDER_GOOGLE_SHARED)
        self.album_url: str | None = entry.data.get(CONF_ALBUM_URL)
        self.local_path: str | None = entry.data.get(CONF_LOCAL_PATH)
        self.recursive: bool = bool(entry.data.get(CONF_RECURSIVE, True))

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
        if self.provider == PROVIDER_LOCAL_FOLDER:
            return await self._update_local_folder()

        if self.provider == PROVIDER_GOOGLE_SHARED:
            return await self._update_google_shared()

        raise UpdateFailed(f"Unsupported provider: {self.provider}")

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

        data = await self._call_publicalbum(session, 500)

        result = data.get("result") if isinstance(data, dict) else {}
        if not isinstance(result, dict):
            result = {}

        page_items = _find_largest_item_list(result) or _find_largest_item_list(data)
        if not page_items:
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
            raise UpdateFailed("No photos returned from publicalbum.org API")

        items: list[MediaItem] = []
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

            items.append(MediaItem(
                url=url, width=width, height=height,
                mime_type=mime, filename=filename,
            ))

        _LOGGER.debug(
            "Google shared album: %d raw items, %d photos returned",
            len(page_items), len(items),
        )

        return {
            "title": result.get("title") or self.entry.title,
            "items": items,
        }
