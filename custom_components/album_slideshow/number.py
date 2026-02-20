from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import AlbumCoordinator
from .store import SlideshowStore


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities(
        [
            SlideIntervalNumber(entry, store),
            RefreshHoursNumber(entry, store, coordinator),
            PairDividerWidthNumber(entry, store),
        ]
    )


class _BaseNumber(NumberEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_mode = NumberMode.BOX

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.entry = entry
        self.store = store

        def _on_store_change() -> None:
            self.async_write_ha_state()

        store.add_listener(_on_store_change)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }


class SlideIntervalNumber(_BaseNumber):
    _attr_icon = "mdi:timer-outline"
    _attr_native_min_value = 3
    _attr_native_max_value = 3600
    _attr_native_step = 1

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_interval"
        self._attr_name = "Slide interval (seconds)"

    @property
    def native_value(self):
        return int(self.store.slide_interval)

    async def async_set_native_value(self, value: float) -> None:
        self.store.slide_interval = int(value)
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state not in (None, "unknown", "unavailable"):
            try:
                self.store.slide_interval = int(float(old.state))
                self.store.notify()
            except Exception:
                return


class RefreshHoursNumber(_BaseNumber):
    _attr_icon = "mdi:refresh"
    _attr_native_min_value = 1
    _attr_native_max_value = 168
    _attr_native_step = 1

    def __init__(self, entry: ConfigEntry, store: SlideshowStore, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, store)
        self.coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh_minutes"
        self._attr_name = "Album refresh (hours)"

    @property
    def native_value(self):
        return int(self.store.refresh_hours)

    async def async_set_native_value(self, value: float) -> None:
        self.store.refresh_hours = int(value)
        self.store.notify()
        await self.coordinator.async_request_refresh()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state not in (None, "unknown", "unavailable"):
            try:
                val = int(float(old.state))
                # Migration: old values were in minutes; convert if above max hours
                if val > self._attr_native_max_value:
                    val = max(int(val / 60), int(self._attr_native_min_value))
                self.store.refresh_hours = val
                self.store.notify()
            except Exception:
                return


class PairDividerWidthNumber(_BaseNumber):
    _attr_icon = "mdi:border-vertical"
    _attr_native_min_value = 0
    _attr_native_max_value = 64
    _attr_native_step = 1

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_pair_divider_px"
        self._attr_name = "Pair divider size (px)"

    @property
    def native_value(self):
        return int(self.store.pair_divider_px)

    async def async_set_native_value(self, value: float) -> None:
        self.store.pair_divider_px = max(0, int(value))
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state not in (None, "unknown", "unavailable"):
            try:
                self.store.pair_divider_px = max(0, int(float(old.state)))
                self.store.notify()
            except Exception:
                return
