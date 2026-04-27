"""Direct scraper for Google Photos shared album HTML pages.

publicalbum.org's JSON-RPC wrapper caps results at ~250-500 items because the
upstream Google embed-player endpoint refuses to return more. The shared album
HTML page itself, however, contains the *full* photo list embedded in
``AF_initDataCallback`` JavaScript blocks. This module fetches the HTML and
walks the embedded data to extract every photo, with no pagination cap.

Brittleness note: Google rearranges the AF block structure roughly every
6-18 months. This parser is intentionally structural rather than positional -
it walks the JSON tree looking for arrays of items shaped like photos
(googleusercontent URL + width + height) rather than indexing into specific
positions. That trades some risk of false positives for survivability across
minor Google changes. Callers should keep ``publicalbum.org`` as a fallback.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .coordinator import MediaItem

_LOGGER = logging.getLogger(__name__)

# AF_initDataCallback({key: 'ds:N', ..., data: <JSON>, sideChannel: {...}});
# We want the `data:` value. The argument to AF_initDataCallback is a JS object
# literal with unquoted keys, so we cannot JSON.parse the whole thing. Instead
# we locate `data:` and bracket-balance from its first `[`.
_AF_BLOCK_RE = re.compile(r"AF_initDataCallback\s*\(\s*\{", re.DOTALL)
_DATA_KEY_RE = re.compile(r"[\s,{]data\s*:\s*\[", re.DOTALL)

# A "photo URL" is a Google-served image URL. Live photos and shared albums
# usually use lh3-lh6.googleusercontent.com, but we accept any
# googleusercontent host plus the rarer photos.fife.usercontent.google.com.
_PHOTO_HOST_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?(?:googleusercontent\.com|google\.com)/",
    re.IGNORECASE,
)

# Common Google image URL size suffix: =w<W>-h<H>-...
_SIZE_SUFFIX_RE = re.compile(r"=[wh]\d+(?:-[a-z0-9]+)*$", re.IGNORECASE)

# Browser-ish user agent so Google's CDN serves us the full HTML page rather
# than a 'please enable JavaScript' shim.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Hard ceiling on items returned, mirroring the per-album cap Google enforces
# (20,000) so a malicious or pathological page can't blow up the integration.
_MAX_ITEMS = 20_000


async def fetch_album_html(session, url: str, *, timeout: float = 30.0) -> str | None:
    """Fetch the shared album page HTML. Returns None on failure."""
    headers = {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(url, headers=headers, timeout=timeout, allow_redirects=True) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type", "").lower()
            if "html" not in ct and "text" not in ct:
                _LOGGER.debug("Album scrape: unexpected content-type %r for %s", ct, url)
                return None
            return await resp.text()
    except Exception as err:
        _LOGGER.debug("Album scrape: failed to fetch %s: %s", url, err)
        return None


def parse_album_html(html: str) -> list[MediaItem]:
    """Parse the HTML and return the extracted media items.

    Returns an empty list if no recognisable photo data was found.
    """
    candidates: list[list[Any]] = []
    for blob in _iter_data_blobs(html):
        try:
            tree = json.loads(blob)
        except json.JSONDecodeError as err:
            _LOGGER.debug("Album scrape: skipped malformed data blob (%s)", err)
            continue
        candidates.extend(_collect_photo_lists(tree))

    if not candidates:
        return []

    # Pick the largest list - the album item list is normally an order of
    # magnitude longer than any side data (album owner, etc.).
    best = max(candidates, key=len)

    items: list[MediaItem] = []
    seen_urls: set[str] = set()
    for raw in best:
        media = _photo_to_media_item(raw)
        if media is None:
            continue
        if media.url in seen_urls:
            continue
        seen_urls.add(media.url)
        items.append(media)
        if len(items) >= _MAX_ITEMS:
            break

    return items


# -- Internals ---------------------------------------------------------------

def _iter_data_blobs(html: str):
    """Yield the JSON text of every AF_initDataCallback ``data:`` value."""
    for m in _AF_BLOCK_RE.finditer(html):
        # Find the matching closing ')' of the AF_initDataCallback call.
        block_end = _balanced_close(html, m.end() - 1, "{", "}")
        if block_end is None:
            continue
        block = html[m.end() - 1: block_end + 1]

        data_match = _DATA_KEY_RE.search(block)
        if data_match is None:
            continue
        # Position of the '[' that opens the data array.
        open_pos = data_match.end() - 1
        close_pos = _balanced_close(block, open_pos, "[", "]")
        if close_pos is None:
            continue
        yield block[open_pos: close_pos + 1]


def _balanced_close(s: str, open_idx: int, open_char: str, close_char: str) -> int | None:
    """Return the index of the matching close for an opening bracket.

    Tracks string literals so brackets inside JS strings don't unbalance us.
    Handles both single-quoted (JS) and double-quoted (JSON) strings, and
    backslash escapes inside them.
    """
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


def _collect_photo_lists(node: Any, _out: list[list[Any]] | None = None) -> list[list[Any]]:
    """Walk a parsed JSON tree and collect every list whose items look like photos."""
    out = _out if _out is not None else []
    if isinstance(node, list):
        if _list_looks_like_photos(node):
            out.append(node)
        for child in node:
            _collect_photo_lists(child, out)
    elif isinstance(node, dict):
        for child in node.values():
            _collect_photo_lists(child, out)
    return out


def _list_looks_like_photos(lst: list[Any]) -> bool:
    """Heuristic: a non-empty list whose every (sampled) entry is photo-shaped.

    We sample the head of the list to bound work on huge album lists, and we
    require *every* sampled entry to parse as a MediaItem. The 'pick largest'
    logic in parse_album_html then disambiguates between the real photo list
    and shorter incidental ones (actors, members) without us having to
    fingerprint the wrapping shape.
    """
    if not lst:
        return False
    sample = lst[:20]
    for item in sample:
        if _photo_to_media_item(item) is None:
            return False
    return True


def _photo_to_media_item(raw: Any) -> MediaItem | None:
    """Extract a MediaItem from a single photo entry, or return None."""
    if not isinstance(raw, list):
        return None

    url = _find_photo_url(raw)
    if not url:
        return None

    width, height = _find_dimensions(raw)
    return MediaItem(
        url=_normalise_size(url, width, height),
        width=width,
        height=height,
        mime_type=None,
        filename=None,
    )


def _find_photo_url(node: Any) -> str | None:
    """First googleusercontent.com URL found anywhere in ``node``."""
    if isinstance(node, str):
        if _PHOTO_HOST_RE.match(node):
            return node
        return None
    if isinstance(node, list):
        for child in node:
            url = _find_photo_url(child)
            if url:
                return url
    elif isinstance(node, dict):
        for child in node.values():
            url = _find_photo_url(child)
            if url:
                return url
    return None


def _find_dimensions(node: Any) -> tuple[int | None, int | None]:
    """Find a (width, height) pair: two consecutive ints in [16, 20000]."""
    if isinstance(node, list):
        # Look for the pattern [..., url, W, H, ...]: a string immediately
        # followed by two plausible-image-dimension ints.
        for i in range(len(node) - 2):
            a, b, c = node[i], node[i + 1], node[i + 2]
            if (
                isinstance(a, str)
                and _PHOTO_HOST_RE.match(a)
                and _is_dimension(b)
                and _is_dimension(c)
            ):
                return int(b), int(c)
        # Recurse into children if not found at this level.
        for child in node:
            w, h = _find_dimensions(child)
            if w is not None:
                return w, h
    elif isinstance(node, dict):
        # Some Google blobs carry width/height under explicit keys.
        w = node.get("width") or node.get("w")
        h = node.get("height") or node.get("h")
        if _is_dimension(w) and _is_dimension(h):
            return int(w), int(h)
        for child in node.values():
            w, h = _find_dimensions(child)
            if w is not None:
                return w, h
    return None, None


def _is_dimension(v: Any) -> bool:
    return isinstance(v, int) and 16 <= v <= 20_000


def _normalise_size(url: str, width: int | None, height: int | None) -> str:
    """Strip any existing ``=w...-h...`` suffix and request a generous size.

    Google's CDN scales lossily based on the suffix; we hint at a size up to
    4K on the longer edge while preserving aspect ratio so render-time
    downsampling produces high-quality output without paying full original
    bandwidth.
    """
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
