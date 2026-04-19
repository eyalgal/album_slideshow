"""
conftest.py — install lightweight stubs for homeassistant so that modules
inside custom_components can be imported without a real HA installation.
"""
from __future__ import annotations

import sys
import types


def _make_stub(*names: str) -> None:
    """Create empty module stubs for each dotted name and all parent packages."""
    for name in names:
        parts = name.split(".")
        for i in range(1, len(parts) + 1):
            mod_name = ".".join(parts[:i])
            if mod_name not in sys.modules:
                sys.modules[mod_name] = types.ModuleType(mod_name)


_make_stub(
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.camera",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.update_coordinator",
    "async_timeout",
)

import homeassistant.components.camera as _cam
_cam.Camera = object  # type: ignore[attr-defined]

import homeassistant.helpers.entity_platform as _ep
_ep.AddEntitiesCallback = object  # type: ignore[attr-defined]

# Provide the handful of names actually referenced at import time.
import homeassistant.config_entries as _ce
import homeassistant.core as _core
import homeassistant.helpers.update_coordinator as _upc
import homeassistant.helpers.entity_registry as _er

_ce.ConfigEntry = object  # type: ignore[attr-defined]
_core.HomeAssistant = object  # type: ignore[attr-defined]


class _DataUpdateCoordinator:
    def __init__(self, *a, **kw):
        pass


class _UpdateFailed(Exception):
    pass


_upc.DataUpdateCoordinator = _DataUpdateCoordinator  # type: ignore[attr-defined]
_upc.UpdateFailed = _UpdateFailed  # type: ignore[attr-defined]
_er.async_get = lambda *a, **kw: None  # type: ignore[attr-defined]
_er.async_entries_for_config_entry = lambda *a, **kw: []  # type: ignore[attr-defined]

import homeassistant.helpers.aiohttp_client as _aiohttp_client
_aiohttp_client.async_get_clientsession = lambda *a, **kw: None  # type: ignore[attr-defined]
