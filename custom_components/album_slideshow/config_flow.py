from __future__ import annotations

import json
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

# Sentinel values for the "Select all" options in the multi-selects.
_ALL_PEOPLE = "__all_people__"
_ALL_ALBUMS = "__all_albums__"

from .const import (
    DOMAIN,
    CONF_PROVIDER,
    CONF_ALBUM_NAME,
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
    IMMICH_IMAGE_SIZE_OPTIONS,
    IMMICH_SELECTION_COMPOSITE,
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
    PHOTOPRISM_IMAGE_PREVIEW,
    PHOTOPRISM_IMAGE_FULLSIZE,
    PHOTOPRISM_IMAGE_ORIGINAL,
    PHOTOPRISM_AUTH_APP_PASSWORD,
    PHOTOPRISM_AUTH_USER_PASSWORD,
    PHOTOPRISM_SELECTION_COMPOSITE,
    CONF_ICLOUD_URL,
    CONF_ICLOUD_TOKEN,
    CONF_ICLOUD_IMAGE_SIZE,
    DEFAULT_ICLOUD_IMAGE_SIZE,
    ICLOUD_IMAGE_FULL,
    ICLOUD_IMAGE_PREVIEW,
    CONF_SYNOLOGY_URL,
    CONF_SYNOLOGY_USERNAME,
    CONF_SYNOLOGY_PASSWORD,
    CONF_SYNOLOGY_DEVICE_ID,
    CONF_SYNOLOGY_SPACE,
    CONF_SYNOLOGY_ALBUM_ID,
    CONF_SYNOLOGY_IMAGE_SIZE,
    CONF_SYNOLOGY_PASSPHRASE,
    CONF_SYNOLOGY_FAVORITE,
    CONF_SYNOLOGY_SELECTION,
    DEFAULT_SYNOLOGY_IMAGE_SIZE,
    SYNOLOGY_SPACE_PERSONAL,
    SYNOLOGY_SPACE_SHARED,
    SYNOLOGY_IMAGE_SMALL,
    SYNOLOGY_IMAGE_MEDIUM,
    SYNOLOGY_IMAGE_LARGE,
    CONF_NEXTCLOUD_URL,
    CONF_NEXTCLOUD_USERNAME,
    CONF_NEXTCLOUD_PASSWORD,
    CONF_NEXTCLOUD_FOLDER,
    CONF_NEXTCLOUD_RECURSIVE,
    CONF_NEXTCLOUD_IMAGE_SIZE,
    DEFAULT_NEXTCLOUD_IMAGE_SIZE,
    NEXTCLOUD_IMAGE_PREVIEW,
    NEXTCLOUD_IMAGE_ORIGINAL,
    DEFAULT_REVERSE_GEOCODE,
    PROVIDER_GOOGLE_SHARED,
    PROVIDER_LOCAL_FOLDER,
    PROVIDER_MEDIA_SOURCE,
    PROVIDER_IMMICH,
    PROVIDER_PHOTOPRISM,
    PROVIDER_ICLOUD,
    PROVIDER_SYNOLOGY,
    PROVIDER_NEXTCLOUD,
    DEFAULT_RECURSIVE,
)


def _normalize_local_path(hass, path: str) -> str:
    p = path.strip()
    if p.startswith("/local/"):
        p = "/config/www/" + p[len("/local/"):]
    elif p == "/local":
        p = "/config/www"
    elif p.startswith("local/"):
        p = "/config/www/" + p[len("local/"):]
    elif p.startswith("/media/local/"):
        p = "/media/" + p[len("/media/local/"):]
    elif p.startswith("media/local/"):
        p = "/media/" + p[len("media/local/"):]
    elif p.startswith("media/"):
        p = "/media/" + p[len("media/"):]
    elif p == "media":
        p = "/media"
    if not p.startswith("/"):
        p = hass.config.path(p)
    return p


ALBUM_URL_RE = re.compile(r"^https?://photos\.app\.goo\.gl/[^/]+/?$")


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._provider: str | None = None
        # Immich flow state carried between steps.
        self._immich_url: str | None = None
        self._immich_key: str | None = None
        # id -> name maps for the Albums and People multi-selects.
        self._immich_albums: dict[str, str] = {}
        self._immich_people: dict[str, str] = {}
        # PhotoPrism flow state carried between steps.
        self._pp_url: str | None = None
        self._pp_auth_method: str | None = None
        self._pp_token: str | None = None
        self._pp_username: str | None = None
        self._pp_password: str | None = None
        self._pp_albums: dict[str, str] = {}
        self._pp_people: dict[str, str] = {}
        # Synology flow state carried between steps.
        self._syn_url: str | None = None
        self._syn_username: str | None = None
        self._syn_password: str | None = None
        self._syn_device_id: str | None = None
        self._syn_space: str = SYNOLOGY_SPACE_PERSONAL
        self._syn_albums: dict[str, str] = {}
        # option key -> {"album_id": id|None, "passphrase": str|None}
        self._syn_album_meta: dict[str, dict[str, Any]] = {}
        # id(str) -> name maps for the composite category multi-selects.
        self._syn_people: dict[str, str] = {}
        self._syn_places: dict[str, str] = {}
        self._syn_tags: dict[str, str] = {}
        self._syn_subjects: dict[str, str] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the options flow handler.

        Only local-folder entries expose user-tunable options today (the
        reverse-geocode toggle); Google entries get a no-op handler so
        that the "Configure" button doesn't appear empty in the UI.

        Note: do NOT pass ``config_entry`` to the OptionsFlow constructor.
        Since Home Assistant 2024.12 the base class manages
        ``self.config_entry`` as a property and assigning to it in
        ``__init__`` raises (the symptom is a 500 when the user clicks
        Configure).
        """
        if config_entry.data.get(CONF_PROVIDER) in (
            PROVIDER_LOCAL_FOLDER,
            PROVIDER_NEXTCLOUD,
        ):
            return LocalFolderOptionsFlow()
        return _NoOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._provider = user_input[CONF_PROVIDER]
            if self._provider == PROVIDER_LOCAL_FOLDER:
                return await self.async_step_local_folder()
            if self._provider == PROVIDER_MEDIA_SOURCE:
                return await self.async_step_media_source()
            if self._provider == PROVIDER_IMMICH:
                return await self.async_step_immich()
            if self._provider == PROVIDER_PHOTOPRISM:
                return await self.async_step_photoprism()
            if self._provider == PROVIDER_ICLOUD:
                return await self.async_step_icloud()
            if self._provider == PROVIDER_SYNOLOGY:
                return await self.async_step_synology()
            if self._provider == PROVIDER_NEXTCLOUD:
                return await self.async_step_nextcloud()
            return await self.async_step_google_shared()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROVIDER, default=PROVIDER_GOOGLE_SHARED): vol.In({
                    PROVIDER_GOOGLE_SHARED: "Google Photos",
                    PROVIDER_LOCAL_FOLDER: "Local Folder",
                    PROVIDER_IMMICH: "Immich (direct API, full metadata)",
                    PROVIDER_PHOTOPRISM: "PhotoPrism (direct API, full metadata)",
                    PROVIDER_ICLOUD: "iCloud Shared Album",
                    PROVIDER_SYNOLOGY: "Synology Photos (direct API, full metadata)",
                    PROVIDER_NEXTCLOUD: "Nextcloud (WebDAV folder, full metadata)",
                    PROVIDER_MEDIA_SOURCE: "Media Source (any source, no metadata)",
                })
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_google_shared(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_ALBUM_URL].strip()
            name = user_input[CONF_ALBUM_NAME].strip()

            if not ALBUM_URL_RE.match(url):
                errors[CONF_ALBUM_URL] = "invalid_album_url"
            else:
                await self.async_set_unique_id(f"{DOMAIN}:{PROVIDER_GOOGLE_SHARED}:{url}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_PROVIDER: PROVIDER_GOOGLE_SHARED,
                        CONF_ALBUM_URL: url,
                        CONF_ALBUM_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALBUM_NAME): str,
                vol.Required(CONF_ALBUM_URL): str,
            }
        )
        return self.async_show_form(step_id="google_shared", data_schema=schema, errors=errors)

    async def async_step_local_folder(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            path = _normalize_local_path(self.hass, user_input[CONF_LOCAL_PATH])
            name = user_input[CONF_ALBUM_NAME].strip()
            recursive = bool(user_input.get(CONF_RECURSIVE, DEFAULT_RECURSIVE))

            if not path:
                errors[CONF_LOCAL_PATH] = "invalid_path"
            else:
                await self.async_set_unique_id(f"{DOMAIN}:{PROVIDER_LOCAL_FOLDER}:{path}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_PROVIDER: PROVIDER_LOCAL_FOLDER,
                        CONF_LOCAL_PATH: path,
                        CONF_RECURSIVE: recursive,
                        CONF_ALBUM_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALBUM_NAME): str,
                vol.Required(CONF_LOCAL_PATH): str,
                vol.Optional(CONF_RECURSIVE, default=DEFAULT_RECURSIVE): bool,
            }
        )
        return self.async_show_form(step_id="local_folder", data_schema=schema, errors=errors)

    async def async_step_media_source(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            content_id = user_input[CONF_MEDIA_CONTENT_ID].strip()
            name = user_input[CONF_ALBUM_NAME].strip()

            if not content_id.startswith("media-source://"):
                errors[CONF_MEDIA_CONTENT_ID] = "invalid_media_source"
            else:
                await self.async_set_unique_id(
                    f"{DOMAIN}:{PROVIDER_MEDIA_SOURCE}:{content_id}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_PROVIDER: PROVIDER_MEDIA_SOURCE,
                        CONF_MEDIA_CONTENT_ID: content_id,
                        CONF_ALBUM_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALBUM_NAME): str,
                vol.Required(CONF_MEDIA_CONTENT_ID): str,
            }
        )
        return self.async_show_form(
            step_id="media_source", data_schema=schema, errors=errors
        )

    async def async_step_immich(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the Immich URL + API key and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_IMMICH_URL].strip()
            key = user_input[CONF_IMMICH_API_KEY].strip()
            from . import immich as immich_api

            client = immich_api.ImmichClient(self.hass, url, key)
            try:
                await client.async_validate()
                albums = await client.async_list_albums()
                people = await client.async_list_people()
            except Exception:  # noqa: BLE001 - any failure means bad URL/key
                errors["base"] = "immich_cannot_connect"
            else:
                self._immich_url = client.base_url
                self._immich_key = key
                # id -> name maps for the two multi-select pickers.
                self._immich_albums = {
                    a["id"]: (a.get("albumName") or a["id"])
                    for a in albums
                    if a.get("id")
                }
                self._immich_people = {
                    p["id"]: p["name"]
                    for p in people
                    if p.get("id") and (p.get("name") or "").strip()
                }
                return await self.async_step_immich_select()

        schema = vol.Schema(
            {
                vol.Required(CONF_IMMICH_URL): str,
                vol.Required(CONF_IMMICH_API_KEY): str,
            }
        )
        return self.async_show_form(
            step_id="immich", data_schema=schema, errors=errors
        )

    async def async_step_immich_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Build a composite Immich selection and finish the entry.

        The user ticks any mix of albums, people and favorites (and may add a
        custom JSON filter); the coordinator unions them. Leaving everything
        empty means "all photos".
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ALBUM_NAME].strip()
            size = user_input.get(CONF_IMMICH_IMAGE_SIZE, DEFAULT_IMMICH_IMAGE_SIZE)
            raw_filter = (user_input.get(CONF_IMMICH_FILTER) or "").strip()
            favorites = bool(user_input.get("favorites"))

            chosen_albums = [a for a in user_input.get("albums", []) if a]
            if _ALL_ALBUMS in chosen_albums:
                chosen_albums = list(self._immich_albums.keys())
            else:
                chosen_albums = [a for a in chosen_albums if a in self._immich_albums]

            chosen_people = [p for p in user_input.get("people", []) if p]
            if _ALL_PEOPLE in chosen_people:
                chosen_people = list(self._immich_people.keys())
            else:
                chosen_people = [p for p in chosen_people if p in self._immich_people]

            if raw_filter:
                try:
                    parsed = json.loads(raw_filter)
                    if not isinstance(parsed, dict):
                        raise ValueError
                except ValueError:
                    errors[CONF_IMMICH_FILTER] = "immich_filter_invalid"

            if not errors:
                selection = {
                    "albums": chosen_albums,
                    "people": chosen_people,
                    "favorites": favorites,
                }
                sel_id = json.dumps(selection, sort_keys=True)
                unique = (
                    f"{DOMAIN}:{PROVIDER_IMMICH}:{self._immich_url}:"
                    f"composite:{sel_id}:{raw_filter}"
                )
                await self.async_set_unique_id(unique)
                self._abort_if_unique_id_configured()
                data = {
                    CONF_PROVIDER: PROVIDER_IMMICH,
                    CONF_IMMICH_URL: self._immich_url,
                    CONF_IMMICH_API_KEY: self._immich_key,
                    CONF_IMMICH_SELECTION_TYPE: IMMICH_SELECTION_COMPOSITE,
                    CONF_IMMICH_SELECTION_ID: sel_id,
                    CONF_IMMICH_IMAGE_SIZE: size,
                    CONF_ALBUM_NAME: name,
                }
                if raw_filter:
                    data[CONF_IMMICH_FILTER] = raw_filter
                return self.async_create_entry(title=name, data=data)

        fields: dict[Any, Any] = {vol.Required(CONF_ALBUM_NAME): str}
        if self._immich_albums:
            album_options = [
                selector.SelectOptionDict(
                    value=_ALL_ALBUMS, label="Select all albums"
                )
            ] + [
                selector.SelectOptionDict(value=aid, label=name)
                for aid, name in self._immich_albums.items()
            ]
            fields[vol.Optional("albums")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=album_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )
        if self._immich_people:
            people_options = [
                selector.SelectOptionDict(
                    value=_ALL_PEOPLE, label="Select all people"
                )
            ] + [
                selector.SelectOptionDict(value=pid, label=name)
                for pid, name in self._immich_people.items()
            ]
            fields[vol.Optional("people")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=people_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )
        fields[vol.Optional("favorites", default=False)] = selector.BooleanSelector()
        fields[vol.Optional(CONF_IMMICH_FILTER)] = str
        fields[
            vol.Optional(CONF_IMMICH_IMAGE_SIZE, default=DEFAULT_IMMICH_IMAGE_SIZE)
        ] = vol.In(IMMICH_IMAGE_SIZE_OPTIONS)
        schema = vol.Schema(fields)
        return self.async_show_form(
            step_id="immich_select", data_schema=schema, errors=errors
        )

    async def async_step_photoprism(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the PhotoPrism URL + credentials and validate them."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_PHOTOPRISM_URL].strip()
            method = user_input[CONF_PHOTOPRISM_AUTH_METHOD]
            token = (user_input.get(CONF_PHOTOPRISM_TOKEN) or "").strip()
            username = (user_input.get(CONF_PHOTOPRISM_USERNAME) or "").strip()
            password = user_input.get(CONF_PHOTOPRISM_PASSWORD) or ""

            if method == PHOTOPRISM_AUTH_APP_PASSWORD and not token:
                errors[CONF_PHOTOPRISM_TOKEN] = "photoprism_token_required"
            elif method == PHOTOPRISM_AUTH_USER_PASSWORD and not (username and password):
                errors[CONF_PHOTOPRISM_USERNAME] = "photoprism_user_required"

            if not errors:
                from . import photoprism as pp_api

                client = pp_api.PhotoprismClient(
                    self.hass,
                    url,
                    auth_method=method,
                    token=token or None,
                    username=username or None,
                    password=password or None,
                )
                try:
                    await client.async_validate()
                    albums = await client.async_list_albums()
                    people = await client.async_list_people()
                except Exception:  # noqa: BLE001 - any failure means bad URL/creds
                    errors["base"] = "photoprism_cannot_connect"
                else:
                    self._pp_url = client.base_url
                    self._pp_auth_method = method
                    self._pp_token = token or None
                    self._pp_username = username or None
                    self._pp_password = password or None
                    self._pp_albums = {
                        a["UID"]: (a.get("Title") or a["UID"])
                        for a in albums
                        if a.get("UID")
                    }
                    self._pp_people = {
                        p["UID"]: p["Name"]
                        for p in people
                        if p.get("UID") and (p.get("Name") or "").strip()
                    }
                    return await self.async_step_photoprism_select()

        schema = vol.Schema(
            {
                vol.Required(CONF_PHOTOPRISM_URL): str,
                vol.Required(
                    CONF_PHOTOPRISM_AUTH_METHOD,
                    default=PHOTOPRISM_AUTH_APP_PASSWORD,
                ): vol.In(
                    {
                        PHOTOPRISM_AUTH_APP_PASSWORD: "App password (recommended)",
                        PHOTOPRISM_AUTH_USER_PASSWORD: "Username + password",
                    }
                ),
                vol.Optional(CONF_PHOTOPRISM_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_PHOTOPRISM_USERNAME): str,
                vol.Optional(CONF_PHOTOPRISM_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="photoprism", data_schema=schema, errors=errors
        )

    async def async_step_photoprism_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Build a composite PhotoPrism selection and finish the entry.

        Same combined picker as Immich: tick any mix of albums, people and
        favorites (optionally a custom search query); an empty selection means
        the whole library.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ALBUM_NAME].strip()
            size = user_input.get(
                CONF_PHOTOPRISM_IMAGE_SIZE, DEFAULT_PHOTOPRISM_IMAGE_SIZE
            )
            raw_filter = (user_input.get(CONF_PHOTOPRISM_FILTER) or "").strip()
            favorites = bool(user_input.get("favorites"))

            chosen_albums = [a for a in user_input.get("albums", []) if a]
            if _ALL_ALBUMS in chosen_albums:
                chosen_albums = list(self._pp_albums.keys())
            else:
                chosen_albums = [a for a in chosen_albums if a in self._pp_albums]

            chosen_people = [p for p in user_input.get("people", []) if p]
            if _ALL_PEOPLE in chosen_people:
                chosen_people = list(self._pp_people.keys())
            else:
                chosen_people = [p for p in chosen_people if p in self._pp_people]

            selection = {
                "albums": chosen_albums,
                "people": chosen_people,
                "favorites": favorites,
            }
            sel_id = json.dumps(selection, sort_keys=True)
            unique = (
                f"{DOMAIN}:{PROVIDER_PHOTOPRISM}:{self._pp_url}:"
                f"composite:{sel_id}:{raw_filter}"
            )
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()
            data = {
                CONF_PROVIDER: PROVIDER_PHOTOPRISM,
                CONF_PHOTOPRISM_URL: self._pp_url,
                CONF_PHOTOPRISM_AUTH_METHOD: self._pp_auth_method,
                CONF_PHOTOPRISM_SELECTION_TYPE: PHOTOPRISM_SELECTION_COMPOSITE,
                CONF_PHOTOPRISM_SELECTION_ID: sel_id,
                CONF_PHOTOPRISM_IMAGE_SIZE: size,
                CONF_ALBUM_NAME: name,
            }
            if self._pp_token:
                data[CONF_PHOTOPRISM_TOKEN] = self._pp_token
            if self._pp_username:
                data[CONF_PHOTOPRISM_USERNAME] = self._pp_username
            if self._pp_password:
                data[CONF_PHOTOPRISM_PASSWORD] = self._pp_password
            if raw_filter:
                data[CONF_PHOTOPRISM_FILTER] = raw_filter
            return self.async_create_entry(title=name, data=data)

        fields: dict[Any, Any] = {vol.Required(CONF_ALBUM_NAME): str}
        if self._pp_albums:
            album_options = [
                selector.SelectOptionDict(value=_ALL_ALBUMS, label="Select all albums")
            ] + [
                selector.SelectOptionDict(value=uid, label=name)
                for uid, name in self._pp_albums.items()
            ]
            fields[vol.Optional("albums")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=album_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )
        if self._pp_people:
            people_options = [
                selector.SelectOptionDict(value=_ALL_PEOPLE, label="Select all people")
            ] + [
                selector.SelectOptionDict(value=uid, label=name)
                for uid, name in self._pp_people.items()
            ]
            fields[vol.Optional("people")] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=people_options,
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )
        fields[vol.Optional("favorites", default=False)] = selector.BooleanSelector()
        fields[vol.Optional(CONF_PHOTOPRISM_FILTER)] = str
        fields[
            vol.Optional(
                CONF_PHOTOPRISM_IMAGE_SIZE, default=DEFAULT_PHOTOPRISM_IMAGE_SIZE
            )
        ] = vol.In(
            {
                PHOTOPRISM_IMAGE_PREVIEW: "Preview (1280px)",
                PHOTOPRISM_IMAGE_FULLSIZE: "Full size (1920px)",
                PHOTOPRISM_IMAGE_ORIGINAL: "High detail (2560px)",
            }
        )
        schema = vol.Schema(fields)
        return self.async_show_form(
            step_id="photoprism_select", data_schema=schema, errors=errors
        )

    async def async_step_icloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate an iCloud Shared Album link."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ALBUM_NAME].strip()
            raw_url = user_input[CONF_ICLOUD_URL].strip()
            size = user_input.get(CONF_ICLOUD_IMAGE_SIZE, DEFAULT_ICLOUD_IMAGE_SIZE)

            from . import icloud as icloud_api

            token = icloud_api.parse_share_link(raw_url)
            if not token:
                errors[CONF_ICLOUD_URL] = "invalid_icloud_url"
            else:
                client = icloud_api.IcloudClient(self.hass, token)
                try:
                    await client.async_validate()
                except Exception:  # noqa: BLE001 - any failure means bad/expired link
                    errors["base"] = "icloud_cannot_connect"
                else:
                    await self.async_set_unique_id(
                        f"{DOMAIN}:{PROVIDER_ICLOUD}:{token}"
                    )
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_PROVIDER: PROVIDER_ICLOUD,
                            CONF_ICLOUD_TOKEN: token,
                            CONF_ICLOUD_IMAGE_SIZE: size,
                            CONF_ALBUM_NAME: name,
                        },
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALBUM_NAME): str,
                vol.Required(CONF_ICLOUD_URL): str,
                vol.Optional(
                    CONF_ICLOUD_IMAGE_SIZE, default=DEFAULT_ICLOUD_IMAGE_SIZE
                ): vol.In(
                    {
                        ICLOUD_IMAGE_FULL: "Full size (best for slideshow)",
                        ICLOUD_IMAGE_PREVIEW: "Preview (thumbnail, fastest)",
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="icloud", data_schema=schema, errors=errors
        )

    async def async_step_synology(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect the Synology URL + credentials (and optional 2FA code)."""
        errors: dict[str, str] = {}

        if user_input is not None:
            url = user_input[CONF_SYNOLOGY_URL].strip()
            username = user_input[CONF_SYNOLOGY_USERNAME].strip()
            password = user_input.get(CONF_SYNOLOGY_PASSWORD) or ""
            space = user_input.get(CONF_SYNOLOGY_SPACE, SYNOLOGY_SPACE_PERSONAL)
            otp = (user_input.get("otp_code") or "").strip()

            from . import synology as syn_api

            client = syn_api.SynologyClient(
                self.hass,
                url,
                username=username,
                password=password,
                space=space,
            )
            try:
                await client.async_login(otp_code=otp or None)
                # Albums and category browsing live only in the Personal space
                # (there is no Shared Space album/category API). For the Shared
                # Space, validate access up front so a permission problem
                # surfaces here, not later.
                if space == SYNOLOGY_SPACE_SHARED:
                    albums = people = places = tags = subjects = []
                    await client.async_collect_assets(None)
                else:
                    albums = await client.async_list_albums()
                    people = await client.async_list_people()
                    places = await client.async_list_places()
                    tags = await client.async_list_tags()
                    subjects = await client.async_list_subjects()
            except syn_api.SynologyOtpRequired:
                errors["otp_code"] = "synology_otp_required"
            except syn_api.SynologyPermissionError:
                errors["base"] = "synology_shared_unavailable"
            except Exception:  # noqa: BLE001 - any failure means bad URL/creds
                errors["base"] = "synology_cannot_connect"
            else:
                self._syn_url = client.base_url
                self._syn_username = username
                self._syn_password = password
                self._syn_space = space
                # A trusted-device token is captured only on the OTP login;
                # store it so future logins skip the 2FA prompt.
                self._syn_device_id = client.captured_device_id
                # Key each album by a synthetic value so an own album and a
                # shared-with-me album that happen to share a numeric id don't
                # collide. Track album_id + passphrase per option.
                self._syn_albums = {}
                self._syn_album_meta = {}
                for a in albums:
                    if a.get("id") is None:
                        continue
                    shared = bool(a.get("shared"))
                    key = f"{'shared' if shared else 'own'}:{a['id']}"
                    label = a.get("name") or str(a["id"])
                    self._syn_albums[key] = f"{label} (shared)" if shared else label
                    self._syn_album_meta[key] = {
                        "album_id": None if shared else a["id"],
                        "passphrase": a.get("passphrase") if shared else None,
                    }
                self._syn_people = {
                    str(p["id"]): p["name"] for p in people if p.get("id") is not None
                }
                self._syn_places = {
                    str(p["id"]): p["name"] for p in places if p.get("id") is not None
                }
                self._syn_tags = {
                    str(t["id"]): t["name"] for t in tags if t.get("id") is not None
                }
                self._syn_subjects = {
                    str(s["id"]): s["name"] for s in subjects if s.get("id") is not None
                }
                await client.async_logout()
                return await self.async_step_synology_select()

        schema = vol.Schema(
            {
                vol.Required(CONF_SYNOLOGY_URL): str,
                vol.Required(CONF_SYNOLOGY_USERNAME): str,
                vol.Required(CONF_SYNOLOGY_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(
                    CONF_SYNOLOGY_SPACE, default=SYNOLOGY_SPACE_PERSONAL
                ): vol.In(
                    {
                        SYNOLOGY_SPACE_PERSONAL: "Personal (My Photos)",
                        SYNOLOGY_SPACE_SHARED: "Shared Space",
                    }
                ),
                vol.Optional("otp_code"): str,
            }
        )
        return self.async_show_form(
            step_id="synology", data_schema=schema, errors=errors
        )

    async def async_step_synology_select(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Build a composite Synology selection and finish the entry.

        Like the Immich/PhotoPrism providers: tick any mix of favorites,
        albums, people, places, tags and subjects. Synology has no OR across
        categories, so each member is queried on its own and merged. An empty
        selection means the whole space.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ALBUM_NAME].strip()
            size = user_input.get(
                CONF_SYNOLOGY_IMAGE_SIZE, DEFAULT_SYNOLOGY_IMAGE_SIZE
            )
            favorites = bool(user_input.get("favorites"))

            album_ids: list[Any] = []
            passphrases: list[str] = []
            for key in user_input.get("albums", []) or []:
                meta = self._syn_album_meta.get(key)
                if not meta:
                    continue
                if meta.get("passphrase"):
                    passphrases.append(meta["passphrase"])
                elif meta.get("album_id") is not None:
                    album_ids.append(meta["album_id"])

            def _ids(field: str, valid: dict[str, str]) -> list[int]:
                out: list[int] = []
                for v in user_input.get(field, []) or []:
                    if v in valid:
                        try:
                            out.append(int(v))
                        except (TypeError, ValueError):
                            pass
                return out

            selection = {
                "favorites": favorites,
                "album_ids": album_ids,
                "passphrases": passphrases,
                "person_ids": _ids("people", self._syn_people),
                "geocoding_ids": _ids("places", self._syn_places),
                "tag_ids": _ids("tags", self._syn_tags),
                "concept_ids": _ids("subjects", self._syn_subjects),
            }
            sel_id = json.dumps(selection, sort_keys=True)
            unique = (
                f"{DOMAIN}:{PROVIDER_SYNOLOGY}:{self._syn_url}:"
                f"{self._syn_space}:{sel_id}"
            )
            await self.async_set_unique_id(unique)
            self._abort_if_unique_id_configured()
            data = {
                CONF_PROVIDER: PROVIDER_SYNOLOGY,
                CONF_SYNOLOGY_URL: self._syn_url,
                CONF_SYNOLOGY_USERNAME: self._syn_username,
                CONF_SYNOLOGY_PASSWORD: self._syn_password,
                CONF_SYNOLOGY_SPACE: self._syn_space,
                CONF_SYNOLOGY_SELECTION: sel_id,
                CONF_SYNOLOGY_IMAGE_SIZE: size,
                CONF_ALBUM_NAME: name,
            }
            if self._syn_device_id:
                data[CONF_SYNOLOGY_DEVICE_ID] = self._syn_device_id
            return self.async_create_entry(title=name, data=data)

        def _multi(options: dict[str, str]):
            return selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=v, label=l)
                        for v, l in options.items()
                    ],
                    multiple=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    custom_value=False,
                )
            )

        fields: dict[Any, Any] = {vol.Required(CONF_ALBUM_NAME): str}
        # Favorites, albums and subjects are Personal-space concepts.
        if self._syn_space == SYNOLOGY_SPACE_PERSONAL:
            fields[vol.Optional("favorites", default=False)] = (
                selector.BooleanSelector()
            )
        if self._syn_albums:
            fields[vol.Optional("albums")] = _multi(self._syn_albums)
        if self._syn_people:
            fields[vol.Optional("people")] = _multi(self._syn_people)
        if self._syn_places:
            fields[vol.Optional("places")] = _multi(self._syn_places)
        if self._syn_tags:
            fields[vol.Optional("tags")] = _multi(self._syn_tags)
        if self._syn_subjects:
            fields[vol.Optional("subjects")] = _multi(self._syn_subjects)
        fields[
            vol.Optional(
                CONF_SYNOLOGY_IMAGE_SIZE, default=DEFAULT_SYNOLOGY_IMAGE_SIZE
            )
        ] = vol.In(
            {
                SYNOLOGY_IMAGE_LARGE: "Large (best for slideshow)",
                SYNOLOGY_IMAGE_MEDIUM: "Medium",
                SYNOLOGY_IMAGE_SMALL: "Small (thumbnail, fastest)",
            }
        )
        return self.async_show_form(
            step_id="synology_select", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_nextcloud(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Collect and validate a Nextcloud WebDAV folder + app password."""
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input[CONF_ALBUM_NAME].strip()
            url = user_input[CONF_NEXTCLOUD_URL].strip()
            username = user_input[CONF_NEXTCLOUD_USERNAME].strip()
            password = user_input.get(CONF_NEXTCLOUD_PASSWORD) or ""
            folder = (user_input.get(CONF_NEXTCLOUD_FOLDER) or "").strip()
            recursive = bool(user_input.get(CONF_NEXTCLOUD_RECURSIVE, False))
            size = user_input.get(
                CONF_NEXTCLOUD_IMAGE_SIZE, DEFAULT_NEXTCLOUD_IMAGE_SIZE
            )

            from . import nextcloud as nc_api

            client = nc_api.NextcloudClient(
                self.hass, url, username, password, folder
            )
            try:
                await client.async_validate()
            except Exception:  # noqa: BLE001 - any failure means bad URL/creds/folder
                errors["base"] = "nextcloud_cannot_connect"
            else:
                await self.async_set_unique_id(
                    f"{DOMAIN}:{PROVIDER_NEXTCLOUD}:{client.base_url}:"
                    f"{username}:{client.folder}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=name,
                    data={
                        CONF_PROVIDER: PROVIDER_NEXTCLOUD,
                        CONF_NEXTCLOUD_URL: client.base_url,
                        CONF_NEXTCLOUD_USERNAME: username,
                        CONF_NEXTCLOUD_PASSWORD: password,
                        CONF_NEXTCLOUD_FOLDER: client.folder,
                        CONF_NEXTCLOUD_RECURSIVE: recursive,
                        CONF_NEXTCLOUD_IMAGE_SIZE: size,
                        CONF_ALBUM_NAME: name,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(CONF_ALBUM_NAME): str,
                vol.Required(CONF_NEXTCLOUD_URL): str,
                vol.Required(CONF_NEXTCLOUD_USERNAME): str,
                vol.Required(CONF_NEXTCLOUD_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Optional(CONF_NEXTCLOUD_FOLDER, default=""): str,
                vol.Optional(CONF_NEXTCLOUD_RECURSIVE, default=False): (
                    selector.BooleanSelector()
                ),
                vol.Optional(
                    CONF_NEXTCLOUD_IMAGE_SIZE, default=DEFAULT_NEXTCLOUD_IMAGE_SIZE
                ): vol.In(
                    {
                        NEXTCLOUD_IMAGE_PREVIEW: "Preview (smoothest slideshow)",
                        NEXTCLOUD_IMAGE_ORIGINAL: "Original (full quality, slower)",
                    }
                ),
            }
        )
        return self.async_show_form(
            step_id="nextcloud", data_schema=schema, errors=errors
        )


class LocalFolderOptionsFlow(config_entries.OptionsFlow):
    """Options for local-folder entries.

    Currently exposes a single toggle: ``reverse_geocode``. Users with
    privacy concerns about handing EXIF GPS coordinates to an external
    OSM endpoint can turn this off; the GPS coordinates remain available
    as ``latitude``/``longitude`` attributes regardless.

    ``self.config_entry`` is provided by ``OptionsFlow`` as a managed
    property (HA 2024.12+); we deliberately do NOT define ``__init__``
    or assign to it, since doing so raises in newer cores.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_REVERSE_GEOCODE, DEFAULT_REVERSE_GEOCODE
        )
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_REVERSE_GEOCODE, default=bool(current)
                ): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)


class _NoOptionsFlow(config_entries.OptionsFlow):
    """Fallback options flow for providers that expose nothing tunable."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        return self.async_create_entry(title="", data={})
