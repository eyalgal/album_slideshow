"""Synology Photos (direct API) client and pure parsing helpers.

Talks to a Synology DSM ``Photos`` package over its ``entry.cgi`` web API - the
same endpoints the Photos web app uses. HTTP lives in ``SynologyClient``; the
parsing / URL helpers are pure functions so they can be unit-tested without a
live NAS or aiohttp.

API shape (all via ``{base}/webapi/entry.cgi``, JSON responses):
- ``SYNO.API.Auth`` v7 ``method=login`` ``account&passwd&format=sid`` ->
    ``{sid}``. For accounts with 2FA enabled, a first login must include
    ``otp_code`` + ``enable_device_token=yes``; the response then carries a
    ``device_id`` that is stored and replayed on later logins (no OTP needed).
- ``SYNO.Foto.Browse.Album`` (personal) / ``SYNO.FotoTeam.Browse.Album``
    (shared space) ``method=list`` -> albums.
- ``SYNO.Foto.Browse.Item`` / ``SYNO.FotoTeam.Browse.Item`` ``method=list``
    ``offset&limit&additional`` (+ optional ``album_id``) -> photo items with
    inline metadata (``time``, ``filesize``, ``resolution``, ``gps``,
    ``address``, ``description``, ``thumbnail{cache_key, unit_id}``).
- Image bytes: ``SYNO.Foto.Thumbnail`` / ``SYNO.FotoTeam.Thumbnail``
    ``method=get`` ``id=<unit_id>&cache_key&type&size``. The string params are
    JSON-encoded (wrapped in literal quotes), e.g. ``type="unit"&size="xl"``.
    Auth is by the session cookie (``id=<sid>``), sent server-side only so the
    SID never reaches the browser or the camera's ``current_url``.

Metadata is inline, so there is no per-asset enrichment pass. Synology surfaces
capture date, GPS and a reverse-geocoded address, so date, location and caption
are all available.
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlencode

import async_timeout
from homeassistant.helpers.aiohttp_client import async_get_clientsession

_TIMEOUT = 30
_PAGE_SIZE = 1000
_MAX_ASSETS = 20_000

_AUTH_API = "SYNO.API.Auth"
_AUTH_VERSION = "7"

# The personal ("My Photos") and shared ("Shared Space") libraries live under
# different API namespaces but are otherwise identical.
_NS_PERSONAL = "SYNO.Foto"
_NS_SHARED = "SYNO.FotoTeam"

SPACE_PERSONAL = "personal"
SPACE_SHARED = "shared"

# Native Synology thumbnail sizes. ``xl`` is the largest (best for a slideshow);
# ``sm`` is a small thumbnail (fastest / least bandwidth).
THUMB_SMALL = "sm"
THUMB_MEDIUM = "m"
THUMB_LARGE = "xl"
THUMB_SIZE_OPTIONS = [THUMB_SMALL, THUMB_MEDIUM, THUMB_LARGE]
DEFAULT_THUMB_SIZE = THUMB_LARGE


class SynologyOtpRequired(Exception):
    """Raised when a login needs a 2FA one-time password."""


class SynologyAuthError(Exception):
    """Raised when login fails for a non-OTP reason (bad creds, host, etc.)."""


class SynologyPermissionError(Exception):
    """Raised when the account cannot access the requested space.

    Most commonly this is the Shared Space (``SYNO.FotoTeam``) returning error
    801 because the Shared Space feature is not enabled on the NAS or the
    account has not been granted access to it.
    """


def normalize_base_url(url: str) -> str:
    """Strip trailing slashes and a trailing ``/webapi/entry.cgi`` from a URL."""
    u = (url or "").strip().rstrip("/")
    for suffix in ("/webapi/entry.cgi", "/webapi", "/photo", "/photos"):
        if u.endswith(suffix):
            u = u[: -len(suffix)]
    return u.rstrip("/")


def api_url(base_url: str) -> str:
    """Return the ``entry.cgi`` endpoint URL for a base URL."""
    return f"{normalize_base_url(base_url)}/webapi/entry.cgi"


def namespace(space: str) -> str:
    """Map a space (``personal``/``shared``) to its API namespace prefix."""
    return _NS_SHARED if space == SPACE_SHARED else _NS_PERSONAL


def build_thumbnail_url(
    base_url: str,
    unit_id: Any,
    cache_key: Any,
    size: str,
    space: str = SPACE_PERSONAL,
    passphrase: str | None = None,
) -> str:
    """Build the thumbnail ``entry.cgi`` URL for a photo unit.

    Synology's binary thumbnail handler expects the string params to be
    JSON-encoded (wrapped in literal double quotes): ``type="unit"``,
    ``size="xl"``, ``cache_key="<id>_<ts>"``. The SID is not in the URL; the
    caller supplies it via the session cookie header (see ``image_headers``).

    For an album that was shared with the account, the owning user's files are
    only reachable with the album's ``passphrase``; without it the thumbnail
    handler returns 404.
    """
    params = {
        "api": f"{namespace(space)}.Thumbnail",
        "version": "2",
        "method": "get",
        "id": unit_id,
        "cache_key": json.dumps(str(cache_key)),
        "type": json.dumps("unit"),
        "size": json.dumps(size if size in THUMB_SIZE_OPTIONS else DEFAULT_THUMB_SIZE),
    }
    if passphrase:
        params["passphrase"] = passphrase
    return f"{api_url(base_url)}?{urlencode(params)}"


def location_label(address: Any) -> str | None:
    """Build a short ``"City, Country"`` label from a Synology address dict."""
    if not isinstance(address, dict):
        return None
    locality = None
    for key in ("city", "town", "village", "district", "county"):
        val = address.get(key)
        if isinstance(val, str) and val.strip():
            locality = val.strip()
            break
    region = address.get("state")
    country = address.get("country")
    parts: list[str] = []
    if locality:
        parts.append(locality)
    elif isinstance(region, str) and region.strip():
        parts.append(region.strip())
    if isinstance(country, str) and country.strip():
        parts.append(country.strip())
    return ", ".join(parts) if parts else None


def is_image(item: Any) -> bool:
    """True for photo items that render as a still image (skip videos)."""
    if not isinstance(item, dict):
        return False
    if str(item.get("type", "photo")).lower() != "photo":
        return False
    thumb = (item.get("additional") or {}).get("thumbnail")
    return isinstance(thumb, dict) and bool(thumb.get("cache_key"))


def thumbnail_ref(item: dict[str, Any]) -> tuple[Any, Any] | None:
    """Return ``(unit_id, cache_key)`` for building a thumbnail URL, or None."""
    thumb = (item.get("additional") or {}).get("thumbnail")
    if not isinstance(thumb, dict):
        return None
    cache_key = thumb.get("cache_key")
    if not cache_key:
        return None
    unit_id = thumb.get("unit_id")
    if unit_id is None:
        unit_id = item.get("id")
    return unit_id, cache_key


def parse_photo_meta(item: dict[str, Any]) -> dict[str, Any]:
    """Extract the metadata we surface from a Synology photo item."""
    out: dict[str, Any] = {}
    taken = item.get("time")
    if isinstance(taken, (int, float)) and taken > 0:
        out["captured_at"] = int(taken * 1000)
    size = item.get("filesize")
    if isinstance(size, int) and size > 0:
        out["byte_size"] = size

    add = item.get("additional") or {}
    res = add.get("resolution") or {}
    if isinstance(res.get("width"), int):
        out["width"] = res["width"]
    if isinstance(res.get("height"), int):
        out["height"] = res["height"]

    gps = add.get("gps") or {}
    lat = gps.get("latitude")
    lng = gps.get("longitude")
    if (
        isinstance(lat, (int, float))
        and isinstance(lng, (int, float))
        and not (abs(lat) < 1e-6 and abs(lng) < 1e-6)
    ):
        out["latitude"] = float(lat)
        out["longitude"] = float(lng)

    loc = location_label(add.get("address"))
    if loc:
        out["location"] = loc

    desc = add.get("description")
    if isinstance(desc, str) and desc.strip():
        out["description"] = desc.strip()
    return out


def _is_otp_error(err: Any) -> bool:
    """True when a login error means "a 2FA one-time password is required"."""
    if not isinstance(err, dict):
        return False
    types = err.get("types")
    if isinstance(types, list) and any(
        isinstance(t, dict) and t.get("type") in ("otp", "2fa") for t in types
    ):
        return True
    # DSM sometimes signals 2FA via an error code plus a JWT token payload.
    if err.get("code") in (403, 404, 406) and err.get("token"):
        return True
    return False


class SynologyClient:
    """Thin async wrapper over the Synology Photos ``entry.cgi`` web API."""

    def __init__(
        self,
        hass,
        url: str,
        username: str,
        password: str,
        device_id: str | None = None,
        space: str = SPACE_PERSONAL,
    ) -> None:
        self.hass = hass
        self.base_url = normalize_base_url(url)
        self.username = username
        self.password = password
        self.device_id = device_id
        self.space = space
        self._sid: str | None = None
        # CSRF token from an ``enable_syno_token`` login. Some endpoints (e.g.
        # the ``favorite_only`` listing) reject requests without it, and in a
        # token session even the thumbnail handler requires it, so it is sent
        # on every API call and included in ``image_headers``.
        self._synotoken: str | None = None
        # Set after a successful OTP login so the config flow can persist it.
        self._captured_device_id: str | None = None

    @property
    def sid(self) -> str | None:
        return self._sid

    @property
    def captured_device_id(self) -> str | None:
        return self._captured_device_id

    @property
    def image_headers(self) -> dict[str, str]:
        """Auth headers for fetching thumbnail bytes (cookie + CSRF token)."""
        if not self._sid:
            return {}
        headers = {"Cookie": f"id={self._sid}"}
        if self._synotoken:
            headers["X-SYNO-TOKEN"] = self._synotoken
        return headers

    async def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        headers = {"X-SYNO-TOKEN": self._synotoken} if self._synotoken else None
        async with async_timeout.timeout(_TIMEOUT):
            async with session.get(
                api_url(self.base_url), params=params, headers=headers
            ) as resp:
                # Synology sometimes serves JSON as text/plain.
                return await resp.json(content_type=None)

    async def async_login(self, otp_code: str | None = None) -> str:
        """Log in and return the session id.

        Raises :class:`SynologyOtpRequired` when the account has 2FA and no
        usable ``device_id`` or ``otp_code`` was supplied.
        """
        params: dict[str, Any] = {
            "api": _AUTH_API,
            "version": _AUTH_VERSION,
            "method": "login",
            "account": self.username,
            "passwd": self.password,
            "format": "sid",
            # Request a CSRF token; needed for the favorites listing and, in a
            # token session, for thumbnail fetches.
            "enable_syno_token": "yes",
        }
        if otp_code:
            params["otp_code"] = otp_code
            params["enable_device_token"] = "yes"
        elif self.device_id:
            params["device_id"] = self.device_id

        data = await self._get(params)
        if not data.get("success"):
            err = data.get("error") or {}
            if _is_otp_error(err):
                raise SynologyOtpRequired()
            raise SynologyAuthError(f"Synology login failed: {err}")

        payload = data.get("data") or {}
        self._sid = payload.get("sid")
        self._synotoken = payload.get("synotoken") or payload.get("syno_token")
        did = payload.get("device_id") or payload.get("did")
        if did:
            self._captured_device_id = did
        if not self._sid:
            raise SynologyAuthError("Synology login returned no session id")
        return self._sid

    async def async_logout(self) -> None:
        if not self._sid:
            return
        try:
            await self._get(
                {
                    "api": _AUTH_API,
                    "version": _AUTH_VERSION,
                    "method": "logout",
                    "_sid": self._sid,
                }
            )
        except Exception:  # noqa: BLE001 - logout is best-effort
            pass
        self._sid = None

    async def async_list_albums(self) -> list[dict[str, Any]]:
        """List the account's own albums plus albums shared with it.

        Albums are a Personal-space concept in Synology Photos - there is no
        ``SYNO.FotoTeam.Browse.Album`` API, so albums are always listed via
        ``SYNO.Foto.Browse.Album`` regardless of the configured space. The
        Shared Space is a flat library with no albums of its own.

        Albums that another user shared with this account do NOT appear in
        ``SYNO.Foto.Browse.Album``; they come from a separate sharing API
        (``SYNO.Foto.Sharing.Misc`` / ``list_shared_with_me_album``) and their
        photos are reachable only via the album's ``passphrase`` (not its id).
        Both kinds are merged here; shared-with-me albums keep their non-empty
        ``passphrase`` and ``shared`` flag so callers can fetch them correctly.
        Returns [] if none / no access.
        """
        out: list[dict[str, Any]] = []

        # 1) The account's own albums.
        offset = 0
        while True:
            data = await self._get(
                {
                    "api": "SYNO.Foto.Browse.Album",
                    "version": "2",
                    "method": "list",
                    "offset": offset,
                    "limit": 100,
                    "_sid": self._sid,
                }
            )
            if not data.get("success"):
                break
            lst = (data.get("data") or {}).get("list") or []
            out.extend(a for a in lst if a.get("id") is not None)
            if len(lst) < 100:
                break
            offset += len(lst)
            if offset >= _MAX_ASSETS:
                break

        # 2) Albums shared with this account (separate API, passphrase-based).
        offset = 0
        while True:
            data = await self._get(
                {
                    "api": "SYNO.Foto.Sharing.Misc",
                    "version": "2",
                    "method": "list_shared_with_me_album",
                    "offset": offset,
                    "limit": 100,
                    "_sid": self._sid,
                }
            )
            if not data.get("success"):
                break
            lst = (data.get("data") or {}).get("list") or []
            for a in lst:
                if a.get("id") is None or not a.get("passphrase"):
                    continue
                a["shared"] = True
                out.append(a)
            if len(lst) < 100:
                break
            offset += len(lst)
            if offset >= _MAX_ASSETS:
                break

        return out

    async def async_collect_assets(
        self,
        album_id: Any = None,
        passphrase: str | None = None,
        favorite_only: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """List photo items, optionally scoped to an album, favorites, or a filter.

        - ``favorite_only`` lists only the account's favorited photos via
          ``SYNO.Foto.Browse.Item`` v7 + ``favorite_only=true`` (Personal
          space; requires the CSRF token from an ``enable_syno_token`` login).
        - ``filters`` (e.g. ``{"person_id": 10}``, ``{"geocoding_id": 3}``,
          ``{"general_tag_id": 1}``, ``{"concept_id": 5}``) scopes the listing
          to that category via ``SYNO.Foto.Browse.Item`` v7.
        - A ``passphrase`` (album shared with this account) lists that album's
          photos via ``SYNO.Foto.Browse.Item`` + ``passphrase`` - the owning
          user's files are not reachable by ``album_id`` (error 609).
        - An ``album_id`` (own album) resolves through ``SYNO.Foto.Browse.Item``.
        - Otherwise the whole space is listed: ``SYNO.Foto.Browse.Item`` for
          the Personal space, ``SYNO.FotoTeam.Browse.Item`` for the Shared
          Space.

        Raises :class:`SynologyPermissionError` when the Shared Space is not
        accessible (error 801), so callers can show a clear message instead of
        a misleading "no images found".
        """
        if favorite_only or filters:
            # Favorites and category filters are Personal-space concepts and are
            # only honoured by version 7 of the item API.
            api = "SYNO.Foto.Browse.Item"
            version = "7"
        elif passphrase or album_id:
            api = "SYNO.Foto.Browse.Item"
            version = "1"
        else:
            api = f"{namespace(self.space)}.Browse.Item"
            version = "1"
        additional = json.dumps(
            ["thumbnail", "resolution", "gps", "address", "description"]
        )
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            params: dict[str, Any] = {
                "api": api,
                "version": version,
                "method": "list",
                "offset": offset,
                "limit": _PAGE_SIZE,
                "additional": additional,
                "_sid": self._sid,
            }
            if favorite_only:
                params["favorite_only"] = "true"
            if filters:
                params.update(filters)
            if passphrase:
                params["passphrase"] = passphrase
            elif album_id:
                params["album_id"] = album_id
            data = await self._get(params)
            if not data.get("success"):
                err = data.get("error") or {}
                if err.get("code") in (801, 105, 119):
                    raise SynologyPermissionError(
                        "Synology Shared Space is not enabled or this account "
                        f"cannot access it (error {err.get('code')})."
                    )
                raise SynologyAuthError(
                    f"Synology item list failed: {err}"
                )
            lst = (data.get("data") or {}).get("list") or []
            items.extend(x for x in lst if is_image(x))
            if len(lst) < _PAGE_SIZE:
                break
            offset += len(lst)
            if offset >= _MAX_ASSETS:
                break
        return items

    async def _list_category(self, api: str, version: str = "1") -> list[dict[str, Any]]:
        """List entries of a browse category (people, places, tags, subjects)."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            data = await self._get(
                {
                    "api": api,
                    "version": version,
                    "method": "list",
                    "offset": offset,
                    "limit": 500,
                    "_sid": self._sid,
                }
            )
            if not data.get("success"):
                break
            lst = (data.get("data") or {}).get("list") or []
            out.extend(a for a in lst if a.get("id") is not None)
            if len(lst) < 500:
                break
            offset += len(lst)
            if offset >= _MAX_ASSETS:
                break
        return out

    async def async_list_people(self) -> list[dict[str, Any]]:
        """List named people (face groups) with a name."""
        people = await self._list_category("SYNO.Foto.Browse.Person", "1")
        return [p for p in people if (p.get("name") or "").strip()]

    async def async_list_places(self) -> list[dict[str, Any]]:
        """List reverse-geocoded places."""
        return await self._list_category("SYNO.Foto.Browse.Geocoding", "1")

    async def async_list_tags(self) -> list[dict[str, Any]]:
        """List user tags."""
        tags = await self._list_category("SYNO.Foto.Browse.GeneralTag", "1")
        return [t for t in tags if (t.get("name") or "").strip()]

    async def async_list_subjects(self) -> list[dict[str, Any]]:
        """List AI-recognised subjects/concepts (e.g. Animals, Food)."""
        subjects = await self._list_category("SYNO.Foto.Browse.Concept", "1")
        return [s for s in subjects if (s.get("name") or "").strip()]

    async def async_collect_composite(
        self, selection: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Collect the union of every selected source, de-duplicated.

        ``selection`` keys (all optional): ``favorites`` (bool), ``album_ids``,
        ``passphrases`` (shared-with-me albums), ``person_ids``,
        ``geocoding_ids``, ``tag_ids``, ``concept_ids``. An empty selection
        means the whole space. Items pulled from a shared album carry a
        ``_passphrase`` key so the caller can build their thumbnail URLs.
        """
        merged: dict[tuple[str, Any], dict[str, Any]] = {}

        def add(items: list[dict[str, Any]], passphrase: str | None = None) -> None:
            for it in items:
                if passphrase:
                    it["_passphrase"] = passphrase
                key = (passphrase or "", it.get("id"))
                merged.setdefault(key, it)

        any_selected = False

        if selection.get("favorites"):
            any_selected = True
            add(await self.async_collect_assets(favorite_only=True))
        for aid in selection.get("album_ids") or []:
            any_selected = True
            add(await self.async_collect_assets(album_id=aid))
        for pp in selection.get("passphrases") or []:
            any_selected = True
            add(await self.async_collect_assets(passphrase=pp), passphrase=pp)
        for pid in selection.get("person_ids") or []:
            any_selected = True
            add(await self.async_collect_assets(filters={"person_id": pid}))
        for gid in selection.get("geocoding_ids") or []:
            any_selected = True
            add(await self.async_collect_assets(filters={"geocoding_id": gid}))
        for tid in selection.get("tag_ids") or []:
            any_selected = True
            add(await self.async_collect_assets(filters={"general_tag_id": tid}))
        for cid in selection.get("concept_ids") or []:
            any_selected = True
            add(await self.async_collect_assets(filters={"concept_id": cid}))

        if not any_selected:
            add(await self.async_collect_assets())

        return list(merged.values())
