from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SERVICE_NEXT_SLIDE, SERVICE_REFRESH_ALBUM, ATTR_ENTRY_ID
from .store import SlideshowStore

PLATFORMS: list[str] = ["camera", "sensor", "button", "number", "select", "text"]


async def _async_cleanup_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    legacy_unique_ids = {
        f"{entry.entry_id}_max_items",
    }

    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id in legacy_unique_ids:
            registry.async_remove(entity.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import AlbumCoordinator

    hass.data.setdefault(DOMAIN, {})

    await _async_cleanup_legacy_entities(hass, entry)

    store = SlideshowStore()
    coordinator = AlbumCoordinator(hass, entry, store)
    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id] = {
        "coordinator": coordinator,
        "store": store,
        "camera": None,
    }

    async def _next_slide(call) -> None:
        entry_id = call.data.get(ATTR_ENTRY_ID)
        if not entry_id:
            return
        data = hass.data.get(DOMAIN, {}).get(entry_id)
        if not data:
            return
        cam = data.get("camera")
        if cam:
            await cam.async_force_next()

    async def _refresh_album(call) -> None:
        entry_id = call.data.get(ATTR_ENTRY_ID)
        if not entry_id:
            return
        data = hass.data.get(DOMAIN, {}).get(entry_id)
        if not data:
            return
        cam = data.get("camera")
        if cam:
            await cam.async_force_refresh()

    if not hass.services.has_service(DOMAIN, SERVICE_NEXT_SLIDE):
        hass.services.async_register(DOMAIN, SERVICE_NEXT_SLIDE, _next_slide)

    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_ALBUM):
        hass.services.async_register(DOMAIN, SERVICE_REFRESH_ALBUM, _refresh_album)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    store.notify()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
