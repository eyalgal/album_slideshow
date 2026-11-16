"""Google Photos shared album client that bypasses the ~300 item HTML cap.

Strategy
--------
1. Fetch the share URL HTML (with browser User-Agent so Google serves the
   real page, not a JS-only shim).
2. Extract the album media key and auth key from the embedded
   ``AF_dataServiceRequests`` blob - the same parameters Google's JS uses
   when it scrolls and needs more pages.
3. Page through the album by POSTing to the public ``batchexecute`` endpoint
   with the ``snAcKc`` RPC (the same one googlephotos.com calls). Each page
   carries up to 300 items plus a continuation token; we loop until the
   token is empty.

This mirrors the approach used by community projects like
``xob0t/google-photos-toolkit``. It uses only undocumented public endpoints
and no auth.

Brittleness
-----------
Google occasionally reshuffles the per-item array layout. We keep parsing
positional but verify each field at access time and skip malformed entries
rather than failing the whole batch.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from .coordinator import MediaItem

_LOGGER = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# AF_dataServiceRequests block in the HTML carries the snAcKc request payload:
#   "snAcKc",ext: ... ,request:["<albumKey>",null,null,"<authKey>"]
_REQUEST_RE = re.compile(
    r"snAcKc[^}]*?request:\s*\[\s*\"([A-Za-z0-9_-]+)\"\s*,\s*null\s*,\s*null\s*,\s*\"([A-Za-z0-9_-]+)\"",
    re.DOTALL,
)

# XSSI prefix Google prepends to batchexecute responses. We strip it before parsing.
_XSSI = ")]}'"

# Hard ceiling - matches the upstream Google album item limit.
_MAX_ITEMS = 20_000

# Per-page item count Google returns. Used only for sanity logging.
_PAGE_SIZE = 300

# Strip any existing ``=w...-h...`` suffix from a Google CDN URL so we can
# attach our own size hint based on the photo's known dimensions.
_SIZE_SUFFIX_RE = re.compile(r"=[wh]\d+(?:-[a-z0-9]+)*$", re.IGNORECASE)

_VIDEO_DURATION_KEY = 76647426  # presence indicates a video; we skip those
_LIVEPHOTO_KEY = 146008172
_LOCATION_HEADER_BYTES = 256 * 1024


class _AlbumKeys:
    __slots__ = ("album_key", "auth_key")

    def __init__(self, album_key: str, auth_key: str) -> None:
        self.album_key = album_key
        self.auth_key = auth_key


async def fetch_album(
    session, share_url: str, *, timeout: float = 30.0
) -> tuple[str | None, list[MediaItem], set[str]]:
    """Fetch a shared album in full. Returns (title, items, video_keys).

    The HTML page is fetched once - just to recover the album/auth keys and
    title. All actual photo enumeration goes through Google's ``batchexecute``
    endpoint, which is the only way to reach photos beyond the first ~300.

    ``video_keys`` holds the media keys of items skipped as video. The
    publicalbum.org source returns no media type at all, so it relies on these
    to drop the same videos (see #26).
    """
    video_keys: set[str] = set()
    keys, title = await _fetch_album_keys(session, share_url, timeout=timeout)
    if keys is None:
        return None, [], video_keys

    items: list[MediaItem] = []
    seen_urls: set[str] = set()
    seen_keys: set[str] = set()
    page_id: str | None = None
    page_no = 0
    while True:
        page_no += 1
        try:
            page_items, page_id = await _fetch_album_page(
                session, keys, page_id, timeout=timeout, video_keys=video_keys
            )
        except Exception as err:
            _LOGGER.warning(
                "Album scrape: page %d batchexecute failed (%s); returning %d items so far",
                page_no, err, len(items),
            )
            break

        added = 0
        for it in page_items:
            # The same photo can come back with a different link.
            if it.url in seen_urls or it.source_id in seen_keys:
                continue
            seen_urls.add(it.url)
            if it.source_id:
                seen_keys.add(it.source_id)
            items.append(it)
            added += 1
            if len(items) >= _MAX_ITEMS:
                break
        _LOGGER.debug(
            "Album scrape: page %d returned %d items (%d new), running total %d",
            page_no, len(page_items), added, len(items),
        )
        if not page_id or len(items) >= _MAX_ITEMS or added == 0:
            break

    _LOGGER.info(
        "Album scraper: batchexecute fetched %d photos in %d page(s)",
        len(items), page_no,
    )
    return title, items, video_keys


async def fetch_metadata_keys(session, share_url: str, *, timeout: float = 15.0):
    """Read public share keys for optional enrichment, without changing pagination."""
    async with session.get(
        share_url, headers={"User-Agent": _BROWSER_UA}, timeout=timeout,
        allow_redirects=True,
    ) as response:
        html = await _read_metadata_body(response, 4 * 1024 * 1024)
    keys = _extract_keys(html)
    if keys is None:
        raise ValueError("Public Google album metadata keys unavailable")
    return keys


async def fetch_photo_location_header(
    session, image_url: str, *, timeout: float = 15.0,
) -> bytes:
    """Read a bounded original-image prefix for explicitly enabled GPS extraction."""
    parsed = urlsplit(image_url)
    if (
        parsed.scheme != "https"
        or not (parsed.hostname or "").endswith(".googleusercontent.com")
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("Unexpected Google image URL")
    original = urlunsplit(parsed._replace(
        path=_SIZE_SUFFIX_RE.sub("", parsed.path) + "=d", fragment="",
    ))
    async with session.get(
        original, headers={"Range": f"bytes=0-{_LOCATION_HEADER_BYTES - 1}"},
        timeout=timeout, allow_redirects=False,
    ) as response:
        response.raise_for_status()
        if response.status not in (200, 206):
            raise ValueError("Unexpected Google image response")
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if not content_type.startswith("image/") and content_type != "application/octet-stream":
            raise ValueError("Google image response is not an image")
        if response.status == 206 and not re.fullmatch(
            r"bytes 0-\d+/(?:\d+|\*)", response.headers.get("Content-Range", ""),
        ):
            raise ValueError("Unexpected Google image byte range")
        content = bytearray()
        async for chunk in response.content.iter_chunked(32768):
            content.extend(chunk[:_LOCATION_HEADER_BYTES - len(content)])
            if len(content) >= _LOCATION_HEADER_BYTES:
                break
        return bytes(content)


async def fetch_photo_metadata(
    session, keys: _AlbumKeys, media_key: str, *, timeout: float = 15.0,
) -> dict[str, Any]:
    """Read camera metadata and description from Google's public photo endpoint."""
    payload = [media_key, None, keys.auth_key, None, keys.album_key]
    envelope = json.dumps([[["VrseUb", json.dumps(payload), None, "generic"]]])
    endpoint = "https://photos.google.com/u/0/_/PhotosUi/data/batchexecute"
    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Origin": "https://photos.google.com",
        "Referer": f"https://photos.google.com/share/{keys.album_key}?key={keys.auth_key}",
    }
    async with session.post(
        endpoint, params={"rpcids": "VrseUb", "source-path": f"/share/{keys.album_key}"},
        headers=headers, data=f"f.req={quote(envelope)}", timeout=timeout,
        allow_redirects=False,
    ) as response:
        body = await _read_metadata_body(response, 1024 * 1024)
    for line in body.splitlines():
        try:
            entries = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if (
                isinstance(entry, list) and len(entry) >= 3
                and entry[:2] == ["wrb.fr", "VrseUb"] and isinstance(entry[2], str)
            ):
                detail = json.loads(entry[2])
                if _metadata_value(detail, 0, 0) != media_key:
                    raise ValueError("Google photo metadata identity mismatch")
                return parse_photo_metadata(detail)
    raise ValueError("Google photo metadata response unavailable")


async def _read_metadata_body(response, limit: int) -> str:
    response.raise_for_status()
    if response.status != 200:
        raise ValueError("Unexpected Google metadata response")
    chunks = []
    length = 0
    async for chunk in response.content.iter_chunked(65536):
        length += len(chunk)
        if length > limit:
            raise ValueError("Google metadata response exceeds size limit")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


def _metadata_value(value: Any, *indices: int) -> Any:
    for index in indices:
        if not isinstance(value, list) or index >= len(value):
            return None
        value = value[index]
    return value


def parse_photo_metadata(detail: Any) -> dict[str, Any]:
    """Extract the camera and description fields verified in public responses."""
    photo = _metadata_value(detail, 0)
    if not isinstance(photo, list):
        raise ValueError("Invalid Google photo metadata")
    camera = _metadata_value(detail, 0, 1, 8, 4)
    result: dict[str, Any] = {}
    for field, index in (("camera_make", 0), ("camera_model", 1)):
        value = _metadata_value(camera, index)
        if isinstance(value, str) and value.strip():
            result[field] = value.strip()[:128]
    for field, index in (
        ("focal_length_mm", 3), ("aperture_f_number", 4),
        ("iso", 5), ("exposure_time_seconds", 6),
    ):
        value = _metadata_value(camera, index)
        if type(value) in (int, float) and math.isfinite(value) and value > 0:
            if field != "iso" or float(value).is_integer():
                result[field] = int(value) if field == "iso" else float(value)
    for section in photo:
        if not isinstance(section, dict):
            continue
        description = _metadata_value(section.get("396644657", section.get(396644657)), 0)
        if isinstance(description, str) and description.strip():
            result["description"] = description.strip()
            break
    return result


# -- Internals ---------------------------------------------------------------

async def _fetch_album_keys(
    session, share_url: str, *, timeout: float
) -> tuple[_AlbumKeys | None, str | None]:
    """Fetch the share URL HTML and extract the album/auth keys + title."""
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(
            share_url, headers=headers, timeout=timeout, allow_redirects=True
        ) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "").lower()
            if "html" not in ct and "text" not in ct:
                _LOGGER.debug("Album scrape: unexpected content-type %r", ct)
                return None, None
            html = await resp.text()
    except Exception as err:
        _LOGGER.debug("Album scrape: failed to fetch %s: %s", share_url, err)
        return None, None

    keys = _extract_keys(html)
    if keys is None:
        _LOGGER.debug("Album scrape: could not locate album keys in HTML")
        return None, None

    return keys, _extract_title(html)


def _extract_keys(html: str) -> _AlbumKeys | None:
    m = _REQUEST_RE.search(html)
    if not m:
        return None
    return _AlbumKeys(album_key=m.group(1), auth_key=m.group(2))


def _extract_title(html: str) -> str | None:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = m.group(1).strip()
    suffix = " - Google Photos"
    if title.endswith(suffix):
        title = title[: -len(suffix)].strip()
    return title or None


def _extract_first_page_items(
    html: str, video_keys: set[str] | None = None
) -> list[MediaItem]:
    """Pull the first 300 items out of the AF_initDataCallback blocks.

    Returns an empty list if the embedded data can't be parsed - the caller
    will still get the rest via batchexecute pagination. Media keys of items
    skipped as video are added to ``video_keys`` when one is supplied.
    """
    candidates: list[list[Any]] = []
    for blob in _iter_af_data_blobs(html):
        try:
            tree = json.loads(blob)
        except json.JSONDecodeError:
            continue
        candidates.extend(_collect_album_item_lists(tree))

    if not candidates:
        return []

    best = max(candidates, key=len)
    items: list[MediaItem] = []
    seen: set[str] = set()
    for raw in best:
        if _is_video_item(raw):
            _record_video_key(raw, video_keys)
            continue
        item = _parse_album_item(raw)
        if item is None or item.url in seen:
            continue
        seen.add(item.url)
        items.append(item)
    return items


def _next_page_token_for_first_page(
    first_items: list[MediaItem], first_page_size: int
) -> str | None:
    """The first page's nextPageId isn't easy to find in the HTML AF blob.

    Strategy: if the first page is exactly the standard page size we use a
    sentinel empty token so the caller fetches page 2 with ``pageId=None``.
    The batchexecute endpoint, given ``pageId=None``, returns page 1 again
    along with the real continuation token, which we then use for the rest.
    Slightly wasteful (we re-fetch page 1) but robust to layout changes.
    """
    if first_page_size >= _PAGE_SIZE:
        return ""  # sentinel: drives the first batchexecute call
    return None


async def _fetch_album_page(
    session,
    keys: _AlbumKeys,
    page_id: str | None,
    *,
    timeout: float,
    video_keys: set[str] | None = None,
) -> tuple[list[MediaItem], str | None]:
    """Call snAcKc once. ``page_id=""`` is treated as ``None`` (initial fetch)."""
    pid = page_id or None
    inner = json.dumps([keys.album_key, pid, None, keys.auth_key])
    envelope = json.dumps([[["snAcKc", inner, None, "generic"]]])
    form = f"f.req={quote(envelope)}"

    url = (
        "https://photos.google.com/u/0/_/PhotosUi/data/batchexecute"
        f"?rpcids=snAcKc&source-path=/share/{quote(keys.album_key)}"
    )
    headers = {
        "User-Agent": _BROWSER_UA,
        "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://photos.google.com",
        "Referer": f"https://photos.google.com/share/{keys.album_key}?key={keys.auth_key}",
    }
    async with session.post(url, data=form, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        body = await resp.text()
    return _parse_batchexecute_album_page(body, video_keys)


def _parse_batchexecute_album_page(
    body: str, video_keys: set[str] | None = None
) -> tuple[list[MediaItem], str | None]:
    """Parse a batchexecute response for one snAcKc call.

    Format (per line, after the XSSI prefix):
      [["wrb.fr", "snAcKc", "<json-encoded inner>", null, null, "generic"], ...]
    The inner data shape (per gptk-toolkit):
      data[1] = list of album items
      data[2] = nextPageId (str) or null
    """
    text = body.lstrip()
    if text.startswith(_XSSI):
        text = text[len(_XSSI):]
    text = text.lstrip()

    line = text.split("\n", 1)[0].strip()
    if not line:
        return [], None
    try:
        outer = json.loads(line)
    except json.JSONDecodeError:
        return [], None

    inner_json: str | None = None
    for entry in outer:
        if isinstance(entry, list) and len(entry) >= 3 and entry[0] == "wrb.fr":
            inner_json = entry[2]
            break
    if not isinstance(inner_json, str):
        return [], None

    try:
        inner = json.loads(inner_json)
    except json.JSONDecodeError:
        return [], None

    raw_items = inner[1] if len(inner) > 1 and isinstance(inner[1], list) else []
    next_page = inner[2] if len(inner) > 2 and isinstance(inner[2], str) else None
    if next_page == "":
        next_page = None

    items: list[MediaItem] = []
    for raw in raw_items:
        if _is_video_item(raw):
            _record_video_key(raw, video_keys)
            continue
        item = _parse_album_item(raw)
        if item is not None:
            items.append(item)
    return items, next_page


def _record_video_key(raw: Any, sink: set[str] | None) -> None:
    """Remember a rejected video so other sources can drop it by id too."""
    if sink is None:
        return
    key = _media_key(raw)
    if key:
        sink.add(key)


def _media_key(raw: Any) -> str | None:
    """Google's stable per-photo id, which publicalbum.org echoes as item ``id``."""
    if isinstance(raw, list) and raw and isinstance(raw[0], str):
        return raw[0]
    return None


def _is_video_item(raw: Any) -> bool:
    """Return ``True`` when an album item carries a video duration.

    Google tags videos with key ``76647426`` (duration in ms) inside a metadata
    dict. That dict is usually the item's last element, but its position moves
    between responses, so scan every nested container instead of just ``[-1]``.
    Live photos carry a duration too and are skipped along with videos, which
    is what the photo-only slideshow wants (see #26).
    """
    return _contains_key(raw, 0)


def _contains_key(node: Any, depth: int) -> bool:
    if depth > 6:
        return False
    if isinstance(node, dict):
        for key in node:
            if key == _VIDEO_DURATION_KEY or key == str(_VIDEO_DURATION_KEY):
                return True
        return any(_contains_key(v, depth + 1) for v in node.values())
    if isinstance(node, list):
        return any(_contains_key(v, depth + 1) for v in node)
    return False


def _parse_album_item(raw: Any) -> MediaItem | None:
    """Parse a single album item array.

    Layout used in both AF blocks and snAcKc responses:
      [mediaKey, [url, w, h, ..., [byte_size]], captured_ms, dedupKey,
       timezoneOffsetMin, uploaded_ms, ..., {<numeric_keys>: ...}]
    """
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    visual = raw[1]
    if not isinstance(visual, list) or len(visual) < 3:
        return None

    url = visual[0]
    if not isinstance(url, str) or not url.startswith("http"):
        return None
    width = visual[1] if isinstance(visual[1], int) else None
    height = visual[2] if isinstance(visual[2], int) else None

    captured_at = raw[2] if len(raw) > 2 and _looks_like_timestamp_ms(raw[2]) else None
    uploaded_at = raw[5] if len(raw) > 5 and _looks_like_timestamp_ms(raw[5]) else None

    # File size, when present, lives at visual[-1][0] as a single int.
    byte_size: int | None = None
    if visual and isinstance(visual[-1], list) and visual[-1]:
        candidate = visual[-1][0]
        if isinstance(candidate, int) and candidate > 0:
            byte_size = candidate

    return MediaItem(
        url=_normalise_size(url, width, height),
        width=width,
        height=height,
        mime_type=None,
        filename=None,
        captured_at=captured_at,
        uploaded_at=uploaded_at,
        byte_size=byte_size,
        source_id=_media_key(raw),
    )


# Plausible epoch-ms range: 1800-01-01 to 2100-01-01.
_MIN_TS_MS = -5_364_662_400_000
_MAX_TS_MS = 4_102_444_800_000


def _looks_like_timestamp_ms(value: Any) -> bool:
    return type(value) is int and _MIN_TS_MS <= value <= _MAX_TS_MS


# -- AF block parsing (for the initial 300 items embedded in the HTML) ------

_AF_BLOCK_RE = re.compile(r"AF_initDataCallback\s*\(\s*\{", re.DOTALL)
_DATA_KEY_RE = re.compile(r"[\s,{]data\s*:\s*\[", re.DOTALL)


def _iter_af_data_blobs(html: str):
    """Yield the JSON text of every AF_initDataCallback ``data:`` value."""
    for m in _AF_BLOCK_RE.finditer(html):
        block_end = _balanced_close(html, m.end() - 1, "{", "}")
        if block_end is None:
            continue
        block = html[m.end() - 1: block_end + 1]
        data_match = _DATA_KEY_RE.search(block)
        if data_match is None:
            continue
        open_pos = data_match.end() - 1
        close_pos = _balanced_close(block, open_pos, "[", "]")
        if close_pos is None:
            continue
        yield block[open_pos: close_pos + 1]


def _balanced_close(s: str, open_idx: int, open_char: str, close_char: str) -> int | None:
    if open_idx >= len(s) or s[open_idx] != open_char:
        return None
    depth = 0
    in_string: str | None = None
    i = open_idx
    n = len(s)
    while i < n:
        c = s[i]
        if in_string is not None:
            if c == "\\":
                i += 2
                continue
            if c == in_string:
                in_string = None
            i += 1
            continue
        if c in ("'", '"'):
            in_string = c
            i += 1
            continue
        if c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _collect_album_item_lists(node: Any, _out: list[list[Any]] | None = None) -> list[list[Any]]:
    """Walk the AF data tree, collecting lists of album-item-shaped entries."""
    out = _out if _out is not None else []
    if isinstance(node, list):
        if _list_looks_like_album_items(node):
            out.append(node)
        for child in node:
            _collect_album_item_lists(child, out)
    elif isinstance(node, dict):
        for child in node.values():
            _collect_album_item_lists(child, out)
    return out


def _list_looks_like_album_items(lst: list[Any]) -> bool:
    if not lst:
        return False
    sample = lst[:20]
    for item in sample:
        if _parse_album_item(item) is None:
            return False
    return True


# -- URL normalisation -------------------------------------------------------

def _normalise_size(url: str, width: int | None, height: int | None) -> str:
    """Strip any existing size suffix and request a 4K-capped version."""
    base = _SIZE_SUFFIX_RE.sub("", url)
    if width and height:
        try:
            w = int(width)
            h = int(height)
        except (TypeError, ValueError):
            return f"{base}=w1920-h1080"
        longest = max(w, h)
        if longest > 3840:
            scale = 3840 / longest
            w = max(1, int(round(w * scale)))
            h = max(1, int(round(h * scale)))
        return f"{base}=w{w}-h{h}"
    return f"{base}=w1920-h1080"


# Backwards-compatible names so the existing tests still find the helpers.
def parse_album_html(html: str) -> list[MediaItem]:
    """Parse only the first-page items embedded in the HTML.

    Retained for backwards compatibility with the 0.5.0-rc1 surface; new
    callers should use ``fetch_album`` for the full paginated result.
    """
    return _extract_first_page_items(html)


_PHOTO_HOST_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?(?:googleusercontent\.com|google\.com)/",
    re.IGNORECASE,
)


def _is_dimension(v: Any) -> bool:
    return isinstance(v, int) and 16 <= v <= 20_000
