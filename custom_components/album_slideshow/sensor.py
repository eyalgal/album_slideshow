from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PROVIDER_GOOGLE_SHARED
from .coordinator import AlbumCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([AlbumCountSensor(entry, coordinator), AlbumTitleSensor(entry, coordinator)])


class _BaseAlbumSensor(SensorEntity):
    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        self.entry = entry
        self.coordinator = coordinator
        coordinator.async_add_listener(self.async_write_ha_state)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }

    def _provider_icon(self) -> str:
        if self.coordinator.provider == PROVIDER_GOOGLE_SHARED:
            return "mdi:google-photos"
        return "mdi:folder-multiple-image"


class AlbumCountSensor(_BaseAlbumSensor):
    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_count"
        self._attr_name = "Photo count"

    @property
    def icon(self) -> str:
        return self._provider_icon()

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        return len(data.get("items", []))


class AlbumTitleSensor(_BaseAlbumSensor):
    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_title"
        self._attr_name = "Album title"

    @property
    def icon(self) -> str:
        return self._provider_icon()

    @property
    def native_value(self):
        data = self.coordinator.data or {}
        return data.get("title")
