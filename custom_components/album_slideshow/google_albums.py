"""Helpers for Google Photos entries that combine several shared albums.

An entry has always stored a single ``album_url``. Entries that combine
several albums additionally store ``album_urls`` (the full ordered list);
``album_url`` keeps holding the first link so that older code paths and
existing installs keep working unchanged.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from .const import CONF_ALBUM_URL, CONF_ALBUM_URLS

# Links may be separated by new lines, commas, semicolons or spaces.
_SPLIT_RE = re.compile(r"[\s,;]+")


def parse_album_urls(text: str | Iterable[str] | None) -> list[str]:
    """Split user input into a list of links, dropping blanks and repeats.

    Order is preserved (the first link stays the "primary" album).
    """
    if text is None:
        return []
    if isinstance(text, str):
        parts = _SPLIT_RE.split(text)
    else:
        parts = []
        for chunk in text:
            if isinstance(chunk, str):
                parts.extend(_SPLIT_RE.split(chunk))
    urls: list[str] = []
    seen: set[str] = set()
    for part in parts:
        url = part.strip()
        if not url:
            continue
        key = url.rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        urls.append(url)
    return urls


def album_urls_from_data(data: Mapping[str, Any]) -> list[str]:
    """Return the entry's album links, oldest single-link entries included."""
    urls = parse_album_urls(data.get(CONF_ALBUM_URLS) or [])
    if not urls:
        single = data.get(CONF_ALBUM_URL)
        if isinstance(single, str):
            urls = parse_album_urls(single)
    return urls


def album_url_data(urls: list[str]) -> dict[str, Any]:
    """Entry-data fields for a list of links.

    A single link is stored exactly as before (``album_url`` only), so a
    one-album entry stays byte-for-byte compatible with older versions.
    """
    data: dict[str, Any] = {CONF_ALBUM_URL: urls[0] if urls else None}
    if len(urls) > 1:
        data[CONF_ALBUM_URLS] = list(urls)
    return data
