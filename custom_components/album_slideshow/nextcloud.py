"""Nextcloud (authenticated WebDAV folder) client and pure parsing helpers.

Talks to a Nextcloud server's WebDAV endpoint for a user's files, pointed at
any folder. Authentication is HTTP Basic with a username + **app password**
(created under Settings > Security > Devices & sessions); the app password is
sent server-side only, so it never reaches the browser or the camera's
``current_url``. HTTP lives in ``NextcloudClient``; the parsing / URL helpers
are pure functions so they can be unit-tested without a live server or aiohttp.

API shape (WebDAV, Basic auth):
- ``PROPFIND /remote.php/dav/files/{user}/{path}/`` (``Depth: 1`` or
    ``infinity`` for recursive) -> a multistatus XML listing the folder's files
    (name, size, content-type, last-modified, ``oc:fileid``). ``Depth: 0`` on
    the folder is used to validate the URL + credentials in the config flow.
- Image bytes (original): ``GET`` of the file's WebDAV href. Sent with the same
    Basic-auth header.
- Image bytes (preview): ``GET /index.php/core/preview?fileId={id}&x=&y=&a=1``
    -> a resized JPEG (smoother / smaller for a slideshow).

Nextcloud has no metadata-only endpoint for WebDAV files, so capture date, GPS
and description are filled in by the coordinator's background enrichment worker
(one original-file download per photo, then read the EXIF - same idea as the
Local Folder provider, just over the network).
"""
from __future__ import annotations

import base64
import email.utils
from typing import Any
from urllib.parse import quote, unquote, urlparse

import async_timeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = 30
_MAX_ASSETS = 20_000

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".heic", ".heif"}

_DAV_NS = "DAV:"
_OC_NS = "http://owncloud.org/ns"

_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8" ?>'
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    "<d:prop>"
    "<d:getcontenttype/>"
    "<d:getcontentlength/>"
    "<d:getlastmodified/>"
    "<d:resourcetype/>"
    "<oc:fileid/>"
    "</d:prop>"
    "</d:propfind>"
).encode("utf-8")


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and a trailing ``/remote.php/...`` from a URL."""
    u = (url or "").strip().rstrip("/")
    for suffix in ("/remote.php/dav", "/remote.php/webdav", "/remote.php"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def normalize_folder(path: str) -> str:
    """Normalize a folder path to a clean, leading/trailing-slash-free form."""
    p = (path or "").strip().strip("/")
    # Collapse any accidental double slashes.
    while "//" in p:
        p = p.replace("//", "/")
    return p


def dav_root(base_url: str, username: str, folder: str) -> str:
    """Return the WebDAV collection URL for a user's folder.

    Each path segment is percent-encoded, but the separating slashes are kept
    so the folder hierarchy is preserved.
    """
    base = normalize_base_url(base_url)
    user = quote(username)
    folder = normalize_folder(folder)
    encoded = "/".join(quote(seg) for seg in folder.split("/") if seg)
    root = f"{base}/remote.php/dav/files/{user}"
    if encoded:
        root = f"{root}/{encoded}"
    return root + "/"


def build_preview_url(base_url: str, file_id: Any, px: int = 1024) -> str:
    """Build a preview (resized JPEG) URL for a file id via core/preview."""
    base = normalize_base_url(base_url)
    return (
        f"{base}/index.php/core/preview?fileId={file_id}"
        f"&x={px}&y={px}&a=1"
    )


def basic_auth_header(username: str, password: str) -> str:
    """Return the value for an HTTP Basic ``Authorization`` header."""
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _looks_like_image(content_type: str | None, filename: str) -> bool:
    if content_type:
        return content_type.split(";", 1)[0].strip().lower().startswith("image/")
    ext = filename[filename.rfind(".") :].lower() if "." in filename else ""
    return ext in _IMAGE_EXTS


def _mtime_to_epoch_ms(raw: str | None) -> int | None:
    """Parse an RFC-1123 ``Last-Modified`` string to epoch milliseconds."""
    if not raw:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    try:
        return int(dt.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None


def parse_propfind_response(xml_text: str, root_url: str) -> list[dict[str, Any]]:
    """Parse a WebDAV multistatus response into a list of image file dicts.

    Skips the self-referencing root entry, any collection (folder) entries,
    and any entry that doesn't look like an image. Returned dicts carry
    ``href``, ``filename``, ``content_type``, ``size``, ``mtime_ms`` and
    ``file_id`` (any of the latter three may be ``None`` when the server
    omitted them).
    """
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    root_path = urlparse(root_url).path.rstrip("/")
    origin = ""
    parsed_root = urlparse(root_url)
    if parsed_root.scheme and parsed_root.netloc:
        origin = f"{parsed_root.scheme}://{parsed_root.netloc}"

    out: list[dict[str, Any]] = []
    for response in root.findall(f"{{{_DAV_NS}}}response"):
        href_el = response.find(f"{{{_DAV_NS}}}href")
        if href_el is None or not href_el.text:
            continue
        href_raw = href_el.text
        href_path = unquote(href_raw).rstrip("/")
        if href_path == root_path:
            continue  # the folder itself

        prop = None
        for propstat in response.findall(f"{{{_DAV_NS}}}propstat"):
            status = propstat.findtext(f"{{{_DAV_NS}}}status") or ""
            if " 200 " in status:
                prop = propstat.find(f"{{{_DAV_NS}}}prop")
                break
        if prop is None:
            continue

        resourcetype = prop.find(f"{{{_DAV_NS}}}resourcetype")
        if resourcetype is not None and resourcetype.find(
            f"{{{_DAV_NS}}}collection"
        ) is not None:
            continue  # folder, not a photo

        filename = unquote(href_path.rsplit("/", 1)[-1])
        if not filename:
            continue

        content_type = prop.findtext(f"{{{_DAV_NS}}}getcontenttype")
        if not _looks_like_image(content_type, filename):
            continue

        size_raw = prop.findtext(f"{{{_DAV_NS}}}getcontentlength")
        try:
            size = int(size_raw) if size_raw is not None else None
        except ValueError:
            size = None

        # Build an absolute URL for the file from the href (which is
        # site-relative). Keep it already percent-encoded as the server gave it.
        abs_url = origin + href_raw if href_raw.startswith("/") else href_raw

        out.append(
            {
                "href": abs_url,
                "filename": filename,
                "content_type": content_type,
                "size": size,
                "mtime_ms": _mtime_to_epoch_ms(
                    prop.findtext(f"{{{_DAV_NS}}}getlastmodified")
                ),
                "file_id": prop.findtext(f"{{{_OC_NS}}}fileid"),
            }
        )

    return out


class NextcloudClient:
    """Thin async wrapper over an authenticated WebDAV folder."""

    def __init__(
        self, hass: Any, base_url: str, username: str, password: str, folder: str
    ) -> None:
        self.hass = hass
        self.base_url = normalize_base_url(base_url)
        self.username = username
        self.password = password
        self.folder = normalize_folder(folder)

    @property
    def dav_root(self) -> str:
        return dav_root(self.base_url, self.username, self.folder)

    @property
    def image_headers(self) -> dict[str, str]:
        """Auth header for fetching image bytes (Basic auth, server-side)."""
        return {"Authorization": basic_auth_header(self.username, self.password)}

    async def _propfind(self, depth: str) -> str:
        session = async_get_clientsession(self.hass)
        headers = {
            "Depth": depth,
            "Content-Type": "application/xml",
            "Authorization": basic_auth_header(self.username, self.password),
        }
        async with async_timeout.timeout(_TIMEOUT):
            async with session.request(
                "PROPFIND", self.dav_root, data=_PROPFIND_BODY, headers=headers
            ) as resp:
                resp.raise_for_status()
                return await resp.text()

    async def async_validate(self) -> None:
        """Confirm the URL, credentials and folder work. Raises on failure."""
        await self._propfind(depth="0")

    async def async_list_photos(self, recursive: bool = False) -> list[dict[str, Any]]:
        """Return the folder's image files (optionally recursing into it)."""
        xml_text = await self._propfind(depth="infinity" if recursive else "1")
        return parse_propfind_response(xml_text, self.dav_root)[:_MAX_ASSETS]
