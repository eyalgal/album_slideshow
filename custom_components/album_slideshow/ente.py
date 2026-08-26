"""Ente Photos (public album link) client, crypto and pure parsing helpers.

Ente is end-to-end encrypted, so unlike every other provider the server never
sees plaintext and the camera cannot simply be handed an image URL: bytes
arrive encrypted and are decrypted here, inside Home Assistant.

A public album link looks like::

    https://albums.ente.io/?t=<accessToken>#<base58(collectionKey)>

The ``t`` query parameter is the *access token*, sent to the API as the
``X-Auth-Access-Token`` header. The URL *fragment* holds the base58-encoded
**collection key** and is never transmitted to the server (browsers do not send
fragments), which is what keeps the album end-to-end encrypted. Both come from
the link alone, so no Ente account or password is involved.

API shape (museum, default origin ``https://api.ente.com``, all requests carry
the ``X-Auth-Access-Token`` header):

- ``GET /public-collection/info`` -> ``{collection: {...}}``. The album's
    ``encryptedName``/``nameDecryptionNonce`` decrypt to its title under the
    collection key. Used to validate the link in the config flow.
- ``GET /public-collection/diff?sinceTime=N`` -> ``{diff: [file], hasMore}``.
    Paginated by ``updationTime``; each file carries ``encryptedKey`` /
    ``keyDecryptionNonce`` (the per-file key, sealed to the collection key) and
    ``metadata`` (sealed to the per-file key).
- ``GET /public-collection/files/preview/{id}`` (thumbnail) and
    ``.../files/download/{id}`` (full image) -> a 307 redirect to object
    storage. The redirect is followed manually so the access token is never
    forwarded to the storage host.

Crypto (all libsodium, via PyNaCl):

- Keys are sealed with ``crypto_secretbox`` (XSalsa20-Poly1305): the per-file
    key is ``secretbox_open(encryptedKey, keyDecryptionNonce, collectionKey)``.
- Metadata and image bytes use ``crypto_secretstream_xchacha20poly1305``,
    keyed by the per-file key and opened with the matching
    ``decryptionHeader``.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs, urlparse

_TIMEOUT = 60
_MAX_FILES = 20_000
_DIFF_PAGE_GUARD = 500

DEFAULT_API_ORIGIN = "https://api.ente.com"

# Ente's fileType enum. Live photos carry a still frame, but the download is a
# zip of image+video, so only plain images are shown.
FILE_TYPE_IMAGE = 0
FILE_TYPE_VIDEO = 1
FILE_TYPE_LIVE_PHOTO = 2

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58decode(value: str) -> bytes:
    """Decode a base58 (Bitcoin alphabet) string. Raises ``ValueError``."""
    if not value:
        raise ValueError("empty base58 string")
    num = 0
    for char in value:
        digit = _B58_INDEX.get(char)
        if digit is None:
            raise ValueError(f"invalid base58 character: {char!r}")
        num = num * 58 + digit
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    # Leading '1's are encoded leading zero bytes.
    pad = len(value) - len(value.lstrip("1"))
    return b"\x00" * pad + body


def normalize_api_origin(url: str | None) -> str:
    """Return a usable API origin, defaulting to Ente's hosted museum."""
    u = (url or "").strip().rstrip("/")
    return u or DEFAULT_API_ORIGIN


class EnteShare:
    """The three things a public album link yields."""

    __slots__ = ("access_token", "collection_key", "album_origin")

    def __init__(
        self, access_token: str, collection_key: bytes, album_origin: str
    ) -> None:
        self.access_token = access_token
        self.collection_key = collection_key
        self.album_origin = album_origin

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, EnteShare)
            and other.access_token == self.access_token
            and other.collection_key == self.collection_key
            and other.album_origin == self.album_origin
        )


def parse_share_link(url: str) -> EnteShare | None:
    """Parse ``https://albums.ente.io/?t=TOKEN#BASE58KEY``.

    Returns ``None`` when the link is not a usable Ente public album link.
    The fragment must decode to a 32-byte key; anything else means the user
    pasted a truncated link (browsers hide the fragment in some UIs).
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None

    token = ""
    for candidate in parse_qs(parsed.query).get("t", []):
        if candidate:
            token = candidate
            break
    if not token:
        return None

    fragment = (parsed.fragment or "").strip()
    if not fragment:
        return None
    try:
        key = b58decode(fragment)
    except ValueError:
        return None
    if len(key) != 32:
        return None

    return EnteShare(token, key, f"{parsed.scheme}://{parsed.netloc}")


def decrypt_secretbox(cipher_b64: str, nonce_b64: str, key: bytes) -> bytes:
    """Open a libsodium ``crypto_secretbox``. Raises on failure."""
    from base64 import b64decode

    from nacl.bindings import crypto_secretbox_open

    return crypto_secretbox_open(b64decode(cipher_b64), b64decode(nonce_b64), key)


def decrypt_secretstream(cipher: bytes, header: bytes, key: bytes) -> bytes:
    """Open a libsodium secretstream, concatenating every chunk.

    Ente encrypts in 4 MiB chunks; the pull state carries across them, so the
    whole ciphertext is walked in order rather than decrypted in one shot.
    """
    from nacl.bindings import (
        crypto_secretstream_xchacha20poly1305_ABYTES,
        crypto_secretstream_xchacha20poly1305_init_pull,
        crypto_secretstream_xchacha20poly1305_pull,
        crypto_secretstream_xchacha20poly1305_state,
    )

    chunk_size = 4 * 1024 * 1024 + crypto_secretstream_xchacha20poly1305_ABYTES
    state = crypto_secretstream_xchacha20poly1305_state()
    crypto_secretstream_xchacha20poly1305_init_pull(state, header, key)

    out: list[bytes] = []
    offset = 0
    total = len(cipher)
    while offset < total:
        block = cipher[offset : offset + chunk_size]
        offset += chunk_size
        plain, _tag = crypto_secretstream_xchacha20poly1305_pull(state, block)
        out.append(plain)
    return b"".join(out)


def decrypt_file_key(item: dict[str, Any], collection_key: bytes) -> bytes:
    """Unseal one file's key from the collection key."""
    return decrypt_secretbox(
        item["encryptedKey"], item["keyDecryptionNonce"], collection_key
    )


def decrypt_metadata(item: dict[str, Any], file_key: bytes) -> dict[str, Any]:
    """Decrypt a file's metadata blob to a dict. Returns ``{}`` if unusable."""
    from base64 import b64decode

    meta = item.get("metadata")
    if not isinstance(meta, dict):
        return {}
    data = meta.get("encryptedData")
    header = meta.get("decryptionHeader")
    if not data or not header:
        return {}
    plain = decrypt_secretstream(b64decode(data), b64decode(header), file_key)
    try:
        parsed = json.loads(plain.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def decrypt_magic_metadata(
    item: dict[str, Any], file_key: bytes, field: str = "pubMagicMetadata"
) -> dict[str, Any]:
    """Decrypt a file's public magic metadata (caption, edited time/name)."""
    from base64 import b64decode

    mmd = item.get(field)
    if not isinstance(mmd, dict):
        return {}
    data = mmd.get("data")
    header = mmd.get("header")
    if not data or not header:
        return {}
    try:
        plain = decrypt_secretstream(b64decode(data), b64decode(header), file_key)
        parsed = json.loads(plain.decode("utf-8"))
    except Exception:  # noqa: BLE001 - magic metadata is strictly optional
        return {}
    return parsed if isinstance(parsed, dict) else {}


def decrypt_album_name(collection: Any, collection_key: bytes) -> str | None:
    """Decrypt a collection's display name, or ``None`` when unavailable."""
    if not isinstance(collection, dict):
        return None
    name = collection.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    cipher = collection.get("encryptedName")
    nonce = collection.get("nameDecryptionNonce")
    if not cipher or not nonce:
        return None
    try:
        return decrypt_secretbox(cipher, nonce, collection_key).decode("utf-8")
    except Exception:  # noqa: BLE001 - a bad name must not fail setup
        return None


def _to_epoch_ms(value: Any) -> int | None:
    """Ente stores timestamps as epoch *microseconds*; convert to ms."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return int(value / 1000)


def _to_coord(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    coerced = float(value)
    # Ente writes 0.0/0.0 when a photo has no GPS fix.
    return None if coerced == 0.0 else coerced


def parse_diff_page(payload: Any) -> tuple[list[dict[str, Any]], bool, int]:
    """Return ``(files, has_more, max_updation_time)`` from a diff response.

    Deleted entries are dropped from the returned files but still counted
    toward ``max_updation_time`` so pagination keeps moving forward.
    """
    if not isinstance(payload, dict):
        return [], False, 0
    diff = payload.get("diff")
    if not isinstance(diff, list):
        return [], False, 0

    files: list[dict[str, Any]] = []
    latest = 0
    for entry in diff:
        if not isinstance(entry, dict):
            continue
        updation = entry.get("updationTime")
        if isinstance(updation, int) and updation > latest:
            latest = updation
        if entry.get("isDeleted"):
            continue
        if not entry.get("id") or not entry.get("encryptedKey"):
            continue
        files.append(entry)
    return files, bool(payload.get("hasMore")), latest


def build_media_fields(
    metadata: dict[str, Any], magic: dict[str, Any]
) -> dict[str, Any]:
    """Flatten decrypted metadata into the fields a MediaItem needs.

    Public magic metadata wins where it overlaps: it holds the user's later
    edits (renamed title, corrected date, added caption).
    """
    title = magic.get("editedName") or metadata.get("title")
    captured = magic.get("editedTime")
    if captured is None:
        captured = metadata.get("creationTime")

    latitude = _to_coord(magic.get("lat"))
    longitude = _to_coord(magic.get("long"))
    if latitude is None or longitude is None:
        latitude = _to_coord(metadata.get("latitude"))
        longitude = _to_coord(metadata.get("longitude"))

    caption = magic.get("caption")
    return {
        "filename": title if isinstance(title, str) and title else None,
        "captured_at": _to_epoch_ms(captured),
        "uploaded_at": _to_epoch_ms(metadata.get("modificationTime")),
        "latitude": latitude,
        "longitude": longitude,
        "description": caption if isinstance(caption, str) and caption else None,
        "width": magic.get("w") if isinstance(magic.get("w"), int) else None,
        "height": magic.get("h") if isinstance(magic.get("h"), int) else None,
    }


def is_image(metadata: dict[str, Any]) -> bool:
    """True when the decrypted metadata describes a still image."""
    return metadata.get("fileType") == FILE_TYPE_IMAGE


def build_item_url(file_id: Any) -> str:
    """Synthetic URL for a decrypted Ente image.

    Ente bytes are fetched and decrypted inside Home Assistant, so there is no
    real URL to hand the camera. This opaque id is what lands in the camera's
    ``current_url`` attribute, keeping the access token and collection key out
    of the browser entirely.
    """
    return f"ente://{file_id}"


def file_id_from_url(url: str) -> str | None:
    """Inverse of :func:`build_item_url`."""
    if isinstance(url, str) and url.startswith("ente://"):
        return url[len("ente://") :] or None
    return None


class EnteClient:
    """Thin async wrapper over the public-collection API, with decryption."""

    def __init__(self, hass: Any, share: EnteShare, api_origin: str | None = None) -> None:
        self.hass = hass
        self.share = share
        self.api_origin = normalize_api_origin(api_origin)

    @property
    def headers(self) -> dict[str, str]:
        return {
            "X-Auth-Access-Token": self.share.access_token,
            "X-Client-Package": "io.ente.albums.web",
            "Accept": "application/json",
        }

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import async_timeout
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.get(
                self.api_origin + path, headers=self.headers, params=params
            ) as resp:
                resp.raise_for_status()
                return await resp.json()

    async def async_validate(self) -> str | None:
        """Confirm the link works and return the album's decrypted name."""
        data = await self._get_json("/public-collection/info")
        collection = data.get("collection") if isinstance(data, dict) else None
        return decrypt_album_name(collection, self.share.collection_key)

    async def async_list_files(self) -> list[dict[str, Any]]:
        """Page through the album's diff and return its non-deleted files."""
        out: list[dict[str, Any]] = []
        seen: set[Any] = set()
        since = 0
        for _ in range(_DIFF_PAGE_GUARD):
            payload = await self._get_json(
                "/public-collection/diff", {"sinceTime": since}
            )
            files, has_more, latest = parse_diff_page(payload)
            for f in files:
                if f["id"] not in seen:
                    seen.add(f["id"])
                    out.append(f)
            if not has_more or latest <= since or len(out) >= _MAX_FILES:
                break
            since = latest
        return out[:_MAX_FILES]

    async def async_fetch_image(
        self, file_id: Any, file_key: bytes, header_b64: str, thumbnail: bool
    ) -> bytes:
        """Download one file's bytes and decrypt them."""
        from base64 import b64decode

        kind = "preview" if thumbnail else "download"
        cipher = await self._fetch_encrypted(
            f"/public-collection/files/{kind}/{file_id}"
        )
        return await self.hass.async_add_executor_job(
            decrypt_secretstream, cipher, b64decode(header_b64), file_key
        )

    async def _fetch_encrypted(self, path: str) -> bytes:
        """GET a file endpoint, following its redirect without leaking auth.

        The API answers with a 307 to object storage. Redirects are handled
        manually so the ``X-Auth-Access-Token`` header is not replayed to a
        third-party host.
        """
        import async_timeout
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(self.hass)
        async with async_timeout.timeout(_TIMEOUT):
            async with session.get(
                self.api_origin + path, headers=self.headers, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise RuntimeError("Ente redirect had no Location header")
                else:
                    resp.raise_for_status()
                    return await resp.read()

        async with async_timeout.timeout(_TIMEOUT):
            async with session.get(location) as storage:
                storage.raise_for_status()
                return await storage.read()
