from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .store import SlideshowStore


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]
    async_add_entities([PauseSwitch(entry, store)])


class PauseSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:pause"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.entry = entry
        self.store = store
        self._attr_unique_id = f"{entry.entry_id}_paused"
        self._attr_name = "Pause slideshow"

    @property
    def is_on(self) -> bool:
        return bool(self.store.paused)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }

    async def async_turn_on(self, **kwargs) -> None:
        self.store.paused = True
        self.store.notify()

    async def async_turn_off(self, **kwargs) -> None:
        self.store.paused = False
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.store.add_listener(self.async_write_ha_state)
        old = await self.async_get_last_state()
        if old is not None and old.state in ("on", "off"):
            self.store.paused = old.state == "on"
            self.store.notify()
