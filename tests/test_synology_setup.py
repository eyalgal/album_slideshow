"""Synology setup: the Shared Space is shown whole, so its last step says so."""
from __future__ import annotations

import asyncio
import json

import pytest

from custom_components.album_slideshow import synology
from custom_components.album_slideshow.config_flow import ConfigFlow
from custom_components.album_slideshow.const import (
    CONF_ALBUM_NAME,
    CONF_SYNOLOGY_IMAGE_SIZE,
    CONF_SYNOLOGY_PASSWORD,
    CONF_SYNOLOGY_SELECTION,
    CONF_SYNOLOGY_SPACE,
    CONF_SYNOLOGY_URL,
    CONF_SYNOLOGY_USERNAME,
    DEFAULT_SYNOLOGY_IMAGE_SIZE,
    SYNOLOGY_SPACE_PERSONAL,
    SYNOLOGY_SPACE_SHARED,
)


class _Client:
    def __init__(self, hass, url, *, username, password, space):
        self.base_url = url
        self.captured_device_id = None

    async def async_login(self, otp_code=None):
        return "sid"

    async def async_logout(self):
        return None

    async def async_collect_assets(self, selection):
        return []

    async def async_list_albums(self):
        return [
            {"id": 1, "name": "Trip"},
            {"id": 7, "name": "Grandma", "shared": True, "passphrase": "abc"},
        ]

    async def async_list_people(self):
        return []

    async def async_list_places(self):
        return []

    async def async_list_tags(self):
        return []

    async def async_list_subjects(self):
        return []


def _login(monkeypatch, space, *, url="http://nas.local:5000", client=_Client):
    monkeypatch.setattr(synology, "SynologyClient", client)
    flow = ConfigFlow()
    flow.hass = object()
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    flow._abort_if_unique_id_configured = lambda: None

    async def set_unique_id(unique_id):
        flow.unique_id = unique_id

    flow.async_set_unique_id = set_unique_id
    form = asyncio.run(flow.async_step_synology({
        CONF_SYNOLOGY_URL: url,
        CONF_SYNOLOGY_USERNAME: "photos",
        CONF_SYNOLOGY_PASSWORD: "secret",
        CONF_SYNOLOGY_SPACE: space,
    }))
    return flow, form


def _fields(form):
    return {str(key) for key in form["data_schema"].schema}


def test_shared_space_asks_only_for_a_name_on_its_own_step(monkeypatch):
    flow, form = _login(monkeypatch, SYNOLOGY_SPACE_SHARED)
    assert form["step_id"] == "synology_shared"
    assert _fields(form) == {CONF_ALBUM_NAME, CONF_SYNOLOGY_IMAGE_SIZE}

    entry = asyncio.run(flow.async_step_synology_shared({
        CONF_ALBUM_NAME: "Team photos",
        CONF_SYNOLOGY_IMAGE_SIZE: DEFAULT_SYNOLOGY_IMAGE_SIZE,
    }))
    assert entry["title"] == "Team photos"
    assert entry["data"][CONF_SYNOLOGY_SPACE] == SYNOLOGY_SPACE_SHARED
    selection = json.loads(entry["data"][CONF_SYNOLOGY_SELECTION])
    assert not any(selection.values())


def test_personal_library_lists_albums_shared_with_you(monkeypatch):
    _flow, form = _login(monkeypatch, SYNOLOGY_SPACE_PERSONAL)
    assert form["step_id"] == "synology_select"
    assert {"favorites", "albums"} <= _fields(form)
    album_field = next(key for key in form["data_schema"].schema if str(key) == "albums")
    labels = [option["label"] for option in form["data_schema"].schema[album_field].config["options"]]
    assert labels == ["Trip", "Grandma (shared)"]


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("myds", "synology_quickconnect"),
        ("quickconnect.to/myds", "synology_quickconnect"),
        ("https://quickconnect.to/myds", "synology_quickconnect"),
        ("192.168.1.10:5000", "synology_url_scheme"),
    ],
)
def test_unusable_addresses_get_a_clear_error_before_logging_in(monkeypatch, url, error):
    logins = []

    class Client(_Client):
        async def async_login(self, otp_code=None):
            logins.append(url)
            return "sid"

    _flow, form = _login(monkeypatch, SYNOLOGY_SPACE_PERSONAL, url=url, client=Client)

    assert (form["step_id"], form["errors"]) == ("synology", {"base": error})
    assert logins == []


def test_a_local_name_without_dots_still_logs_in(monkeypatch):
    _flow, form = _login(monkeypatch, SYNOLOGY_SPACE_PERSONAL, url="http://diskstation:5000")
    assert form["step_id"] == "synology_select"


def test_address_errors_have_text_in_both_translation_files():
    import pathlib

    root = pathlib.Path(__file__).parents[1] / "custom_components" / "album_slideshow"
    for name in ("strings.json", "translations/en.json"):
        errors = json.loads((root / name).read_text())["config"]["error"]
        assert {"synology_quickconnect", "synology_url_scheme"} <= set(errors)


def test_shared_space_step_text_exists_in_both_translation_files():
    import pathlib

    root = pathlib.Path(__file__).parents[1] / "custom_components" / "album_slideshow"
    for name in ("strings.json", "translations/en.json"):
        steps = json.loads((root / name).read_text())["config"]["step"]
        assert set(steps["synology_shared"]["data"]) == {CONF_ALBUM_NAME, CONF_SYNOLOGY_IMAGE_SIZE}
