from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN, DEFAULT_PAIR_DIVIDER_COLOR
from .store import SlideshowStore


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]
    async_add_entities([PairDividerColorText(entry, store)])


class PairDividerColorText(TextEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:palette"
    _attr_native_min = 1
    _attr_native_max = 32

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.entry = entry
        self.store = store

        self._attr_unique_id = f"{entry.entry_id}_pair_divider_color"
        self._attr_name = "Pair divider color"

        def _on_store_change() -> None:
            self.async_write_ha_state()

        store.add_listener(_on_store_change)

    @property
    def native_value(self) -> str:
        return self.store.pair_divider_color

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }

    async def async_set_value(self, value: str) -> None:
        val = (value or "").strip()
        self.store.pair_divider_color = val or DEFAULT_PAIR_DIVIDER_COLOR
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.store.add_listener(self.async_write_ha_state)
        old = await self.async_get_last_state()
        if old and old.state not in (None, "unknown", "unavailable"):
            self.store.pair_divider_color = old.state.strip() or DEFAULT_PAIR_DIVIDER_COLOR
            self.store.notify()
