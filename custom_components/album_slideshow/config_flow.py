from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_ALBUM_NAME,
    CONF_ALBUM_URL,
    CONF_BRIGHTWHEEL_2FA_CODE,
    CONF_BRIGHTWHEEL_EMAIL,
    CONF_BRIGHTWHEEL_LOOKBACK_DAYS,
    CONF_BRIGHTWHEEL_PASSWORD,
    CONF_BRIGHTWHEEL_SESSION,
    CONF_BRIGHTWHEEL_STUDENT_IDS,
    CONF_LOCAL_PATH,
    CONF_PROVIDER,
    CONF_RECURSIVE,
    DEFAULT_BRIGHTWHEEL_LOOKBACK_DAYS,
    DEFAULT_RECURSIVE,
    DOMAIN,
    PROVIDER_BRIGHTWHEEL,
    PROVIDER_GOOGLE_SHARED,
    PROVIDER_LOCAL_FOLDER,
)

_LOGGER = logging.getLogger(__name__)


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
        self._bw_email: str | None = None
        self._bw_password: str | None = None
        self._bw_album_name: str | None = None
        self._bw_lookback_days: int = DEFAULT_BRIGHTWHEEL_LOOKBACK_DAYS
        self._bw_students: list[dict[str, Any]] = []
        self._bw_session_dict: dict[str, Any] | None = None
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            self._provider = user_input[CONF_PROVIDER]
            if self._provider == PROVIDER_LOCAL_FOLDER:
                return await self.async_step_local_folder()
            if self._provider == PROVIDER_BRIGHTWHEEL:
                return await self.async_step_brightwheel_credentials()
            return await self.async_step_google_shared()

        schema = vol.Schema(
            {
                vol.Required(CONF_PROVIDER, default=PROVIDER_GOOGLE_SHARED): vol.In(
                    {
                        PROVIDER_GOOGLE_SHARED: "Google Photos",
                        PROVIDER_LOCAL_FOLDER: "Local Folder",
                        PROVIDER_BRIGHTWHEEL: "Brightwheel",
                    }
                )
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

    # --------------------------- Brightwheel ---------------------------

    async def async_step_brightwheel_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        from . import brightwheel_scraper as bw

        errors: dict[str, str] = {}

        if user_input is not None:
            email = (user_input.get(CONF_BRIGHTWHEEL_EMAIL) or "").strip()
            password = user_input.get(CONF_BRIGHTWHEEL_PASSWORD) or ""
            album_name = (user_input.get(CONF_ALBUM_NAME) or "").strip()
            lookback_days = int(
                user_input.get(
                    CONF_BRIGHTWHEEL_LOOKBACK_DAYS, DEFAULT_BRIGHTWHEEL_LOOKBACK_DAYS
                )
            )

            if not email or "@" not in email:
                errors[CONF_BRIGHTWHEEL_EMAIL] = "invalid_email"
            elif not password:
                errors[CONF_BRIGHTWHEEL_PASSWORD] = "invalid_password"
            elif not album_name and self._reauth_entry is None:
                errors[CONF_ALBUM_NAME] = "invalid_album_name"
            else:
                self._bw_email = email
                self._bw_password = password
                self._bw_album_name = album_name or (
                    self._reauth_entry.title if self._reauth_entry else "Brightwheel"
                )
                self._bw_lookback_days = max(0, lookback_days)

                session = async_get_clientsession(self.hass)
                try:
                    await bw.login(session, email, password)
                except bw.BrightwheelTwoFactorRequired:
                    return await self.async_step_brightwheel_2fa()
                except bw.BrightwheelAuthRequired:
                    errors["base"] = "invalid_auth"
                except bw.BrightwheelError as err:
                    _LOGGER.warning("Brightwheel login error: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception as err:  # pragma: no cover - defensive
                    _LOGGER.exception("Brightwheel login unexpected error: %s", err)
                    errors["base"] = "unknown"

        default_email = self._bw_email or ""
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_BRIGHTWHEEL_EMAIL, default=default_email): str,
            vol.Required(CONF_BRIGHTWHEEL_PASSWORD): str,
        }
        if self._reauth_entry is None:
            schema_dict[
                vol.Required(CONF_ALBUM_NAME, default=self._bw_album_name or "")
            ] = str
            schema_dict[
                vol.Optional(
                    CONF_BRIGHTWHEEL_LOOKBACK_DAYS,
                    default=DEFAULT_BRIGHTWHEEL_LOOKBACK_DAYS,
                )
            ] = vol.All(vol.Coerce(int), vol.Range(min=0, max=3650))

        return self.async_show_form(
            step_id="brightwheel_credentials",
            data_schema=vol.Schema(schema_dict),
            errors=errors,
        )

    async def async_step_brightwheel_2fa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        from . import brightwheel_scraper as bw

        errors: dict[str, str] = {}

        if user_input is not None:
            code = (user_input.get(CONF_BRIGHTWHEEL_2FA_CODE) or "").strip()
            if not re.fullmatch(r"\d{4,8}", code):
                errors[CONF_BRIGHTWHEEL_2FA_CODE] = "invalid_code"
            else:
                session = async_get_clientsession(self.hass)
                try:
                    bw_session = await bw.login(
                        session,
                        self._bw_email or "",
                        self._bw_password or "",
                        code=code,
                    )
                except bw.BrightwheelTwoFactorRequired:
                    errors["base"] = "two_factor_required"
                except bw.BrightwheelAuthRequired:
                    errors["base"] = "invalid_code"
                except bw.BrightwheelError as err:
                    _LOGGER.warning("Brightwheel 2FA verification failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    self._bw_session_dict = bw_session.to_dict()
                    if self._reauth_entry is not None:
                        return await self._async_finish_reauth()
                    try:
                        guardian_id = await bw.fetch_guardian_id(
                            session, bw_session.csrf_token
                        )
                        students = await bw.fetch_students(
                            session, bw_session.csrf_token, guardian_id
                        )
                    except bw.BrightwheelError as err:
                        _LOGGER.warning("Brightwheel student lookup failed: %s", err)
                        errors["base"] = "cannot_connect"
                    else:
                        if not students:
                            errors["base"] = "no_students"
                        else:
                            self._bw_students = students
                            return await self.async_step_brightwheel_students()

        return self.async_show_form(
            step_id="brightwheel_2fa",
            data_schema=vol.Schema(
                {vol.Required(CONF_BRIGHTWHEEL_2FA_CODE): str}
            ),
            errors=errors,
        )

    async def async_step_brightwheel_students(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        student_options = {s["id"]: s["name"] for s in self._bw_students}
        all_ids = list(student_options.keys())

        if user_input is not None:
            picked_raw = user_input.get(CONF_BRIGHTWHEEL_STUDENT_IDS) or all_ids
            if isinstance(picked_raw, str):
                picked: list[str] = [picked_raw]
            else:
                picked = list(picked_raw)

            if not picked:
                errors[CONF_BRIGHTWHEEL_STUDENT_IDS] = "no_students_selected"
            else:
                await self.async_set_unique_id(
                    f"{DOMAIN}:{PROVIDER_BRIGHTWHEEL}:{(self._bw_email or '').lower()}"
                )
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=self._bw_album_name or "Brightwheel",
                    data={
                        CONF_PROVIDER: PROVIDER_BRIGHTWHEEL,
                        CONF_ALBUM_NAME: self._bw_album_name or "Brightwheel",
                        CONF_BRIGHTWHEEL_EMAIL: self._bw_email,
                        CONF_BRIGHTWHEEL_PASSWORD: self._bw_password,
                        CONF_BRIGHTWHEEL_SESSION: self._bw_session_dict,
                        CONF_BRIGHTWHEEL_STUDENT_IDS: picked,
                        CONF_BRIGHTWHEEL_LOOKBACK_DAYS: self._bw_lookback_days,
                    },
                )

        # HA's frontend renders ``vol.In(dict)`` as a single-select; for
        # multi-select we fall back to the loose schema below and let the
        # frontend show checkboxes via the ``multi_select`` translation.
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_BRIGHTWHEEL_STUDENT_IDS, default=all_ids
                ): [vol.In(student_options)],
            }
        )
        return self.async_show_form(
            step_id="brightwheel_students",
            data_schema=schema,
            errors=errors,
            description_placeholders={"count": str(len(student_options))},
        )

    # ----------------------------- Reauth -----------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_unknown")

        self._provider = self._reauth_entry.data.get(CONF_PROVIDER)
        if self._provider != PROVIDER_BRIGHTWHEEL:
            return self.async_abort(reason="reauth_not_supported")

        self._bw_email = self._reauth_entry.data.get(CONF_BRIGHTWHEEL_EMAIL)
        self._bw_album_name = self._reauth_entry.title
        return await self.async_step_brightwheel_credentials()

    async def _async_finish_reauth(self) -> FlowResult:
        if self._reauth_entry is None:
            return self.async_abort(reason="reauth_unknown")

        new_data = dict(self._reauth_entry.data)
        new_data[CONF_BRIGHTWHEEL_EMAIL] = self._bw_email
        new_data[CONF_BRIGHTWHEEL_PASSWORD] = self._bw_password
        new_data[CONF_BRIGHTWHEEL_SESSION] = self._bw_session_dict

        self.hass.config_entries.async_update_entry(self._reauth_entry, data=new_data)
        await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
        return self.async_abort(reason="reauth_successful")
