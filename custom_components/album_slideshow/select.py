from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    DOMAIN,
    FILL_COVER,
    FILL_CONTAIN,
    FILL_BLUR,
    ORIENTATION_MISMATCH_PAIR,
    ORIENTATION_MISMATCH_SINGLE,
    ORIENTATION_MISMATCH_AVOID,
    ORDER_RANDOM,
    ORDER_ALBUM,
)
from .store import SlideshowStore

ASPECT_RATIO_OPTIONS = ["16:9", "16:10", "4:3", "1:1", "3:4", "10:16", "9:16"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]
    async_add_entities(
        [
            FillModeSelect(entry, store),
            PortraitModeSelect(entry, store),
            OrderModeSelect(entry, store),
            AspectRatioSelect(entry, store),
        ]
    )


class _BaseSelect(SelectEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = True

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        self.entry = entry
        self.store = store

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.store.add_listener(self.async_write_ha_state)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.entry.entry_id)},
            "name": f"Album Slideshow {self.entry.title}",
            "manufacturer": "Album Slideshow",
        }


class FillModeSelect(_BaseSelect):
    _attr_icon = "mdi:aspect-ratio"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_fill_mode"
        self._attr_name = "Fill mode"
        self._attr_options = [FILL_BLUR, FILL_COVER, FILL_CONTAIN]

    @property
    def current_option(self):
        value = self.store.fill_mode
        return value if value in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            return
        self.store.fill_mode = option
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state in self.options:
            self.store.fill_mode = old.state
            self.store.notify()


class PortraitModeSelect(_BaseSelect):
    _attr_icon = "mdi:account-box-outline"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_portrait_mode"
        self._attr_name = "Orientation mismatch mode"
        self._attr_options = [
            ORIENTATION_MISMATCH_PAIR,
            ORIENTATION_MISMATCH_SINGLE,
            ORIENTATION_MISMATCH_AVOID,
        ]

    @property
    def current_option(self):
        value = self.store.portrait_mode
        return value if value in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            return
        self.store.portrait_mode = option
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old:
            restored = old.state
            if restored in ("blur", "crop"):
                restored = ORIENTATION_MISMATCH_SINGLE
            if restored in self.options:
                self.store.portrait_mode = restored
                self.store.notify()


class OrderModeSelect(_BaseSelect):
    _attr_icon = "mdi:shuffle-variant"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_order_mode"
        self._attr_name = "Order mode"
        self._attr_options = [ORDER_RANDOM, ORDER_ALBUM]

    @property
    def current_option(self):
        value = self.store.order_mode
        return value if value in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            return
        self.store.order_mode = option
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state in self.options:
            self.store.order_mode = old.state
            self.store.notify()


class AspectRatioSelect(_BaseSelect):
    _attr_icon = "mdi:crop"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_aspect_ratio"
        self._attr_name = "Aspect ratio"
        self._attr_options = ASPECT_RATIO_OPTIONS

    @property
    def current_option(self):
        value = self.store.aspect_ratio
        return value if value in self.options else self.options[0]

    async def async_select_option(self, option: str) -> None:
        if option not in self.options:
            return
        self.store.aspect_ratio = option
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state in self.options:
            self.store.aspect_ratio = old.state
            self.store.notify()
