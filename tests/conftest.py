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
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.aiohttp_client",
    "homeassistant.helpers.entity_platform",
    "homeassistant.helpers.entity_registry",
    "homeassistant.helpers.selector",
    "homeassistant.helpers.storage",
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
_core.SupportsResponse = types.SimpleNamespace(NONE="none", ONLY="only")

import homeassistant.exceptions as _exceptions


class _ServiceValidationError(Exception):
    pass


class _ConfigEntryNotReady(Exception):
    pass


_exceptions.ServiceValidationError = _ServiceValidationError
_exceptions.ConfigEntryNotReady = _ConfigEntryNotReady

import homeassistant.const as _const
_const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"  # type: ignore[attr-defined]


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

import homeassistant.helpers.storage as _storage


class _Store:
    def __init__(self, *a, **kw):
        pass

    async def async_load(self):
        return None

    async def async_save(self, *a, **kw):
        return None

    async def async_remove(self):
        return None


_storage.Store = _Store  # type: ignore[attr-defined]


# ``async_timeout.timeout`` is used by coordinator.py around aiohttp calls;
# tests that exercise those code paths need a no-op async context manager
# rather than the empty module stub.
class _NullAsyncTimeout:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


import async_timeout as _async_timeout

_async_timeout.timeout = lambda *a, **kw: _NullAsyncTimeout()  # type: ignore[attr-defined]


# config_flow.py imports ``callback`` from homeassistant.core; provide a
# pass-through decorator that mirrors the real one's behaviour.
def _callback(func):
    return func


_core.callback = _callback  # type: ignore[attr-defined]


# Minimal stubs for the config_entries types used by ConfigFlow + OptionsFlow.
class _ConfigFlow:
    VERSION = 1
    def __init_subclass__(cls, **kwargs):
        # Accept the ``domain=...`` keyword used by ConfigFlow subclasses.
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)


class _OptionsFlow:
    pass


_ce.ConfigFlow = _ConfigFlow  # type: ignore[attr-defined]
_ce.OptionsFlow = _OptionsFlow  # type: ignore[attr-defined]


# data_entry_flow.FlowResult is just an alias for dict in older HA cores.
_make_stub("homeassistant.data_entry_flow")
import homeassistant.data_entry_flow as _def

_def.FlowResult = dict  # type: ignore[attr-defined]


# helpers.entity.EntityCategory is referenced by sensor.py.
_make_stub("homeassistant.helpers.entity")
import homeassistant.helpers.entity as _he


class _EntityCategory:
    DIAGNOSTIC = "diagnostic"
    CONFIG = "config"


_he.EntityCategory = _EntityCategory  # type: ignore[attr-defined]


# components.sensor: SensorEntity + SensorStateClass.
_make_stub("homeassistant.components.sensor")
import homeassistant.components.sensor as _sensor


class _SensorEntity:
    pass


_sensor.SensorEntity = _SensorEntity  # type: ignore[attr-defined]


class _SensorStateClass:
    MEASUREMENT = "measurement"


_sensor.SensorStateClass = _SensorStateClass  # type: ignore[attr-defined]


# helpers.selector: just enough for config_flow to build its schemas.
import homeassistant.helpers.selector as _selector


class _SelectorBase:
    def __init__(self, config=None):
        self.config = config

    def __call__(self, value):
        return value


_selector.SelectOptionDict = dict  # type: ignore[attr-defined]
_selector.SelectSelectorConfig = lambda **kw: kw  # type: ignore[attr-defined]
_selector.SelectSelectorMode = types.SimpleNamespace(DROPDOWN="dropdown", LIST="list")  # type: ignore[attr-defined]
_selector.SelectSelector = _SelectorBase  # type: ignore[attr-defined]
_selector.BooleanSelector = _SelectorBase  # type: ignore[attr-defined]
