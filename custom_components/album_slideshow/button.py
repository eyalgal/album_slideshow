from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SERVICE_NEXT_SLIDE, SERVICE_REFRESH_ALBUM, ATTR_ENTRY_ID
from .coordinator import AlbumCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            NextSlideButton(hass, entry, coordinator),
            RefreshAlbumButton(hass, entry, coordinator),
        ]
    )


class _BaseButton(ButtonEntity):
    _attr_has_entity_name = True

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        self.hass = hass
        self.entry = entry
        self.coordinator = coordinator

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }


class NextSlideButton(_BaseButton):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(hass, entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_next_button"
        self._attr_name = "Next slide"
        self._attr_icon = "mdi:skip-next"

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_NEXT_SLIDE,
            {ATTR_ENTRY_ID: self.entry.entry_id},
            blocking=False,
        )


class RefreshAlbumButton(_BaseButton):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(hass, entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_refresh_button"
        self._attr_name = "Refresh album"
        self._attr_icon = "mdi:refresh"

    async def async_press(self) -> None:
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH_ALBUM,
            {ATTR_ENTRY_ID: self.entry.entry_id},
            blocking=False,
        )
