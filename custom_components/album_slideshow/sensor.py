from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PROVIDER_LOCAL_FOLDER, PROVIDER_GOOGLE_SHARED
from .coordinator import AlbumCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = [
        AlbumCountSensor(entry, coordinator),
        AlbumTitleSensor(entry, coordinator),
        CacheUsageSensor(entry, coordinator),
    ]
    if coordinator.provider == PROVIDER_LOCAL_FOLDER:
        entities.append(GeocodingProgressSensor(entry, coordinator))
    async_add_entities(entities)


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


class CacheUsageSensor(_BaseAlbumSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "MB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:database"
    _attr_should_poll = True

    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_cache_usage"
        self._attr_name = "Image cache usage"

    @property
    def native_value(self):
        cam = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id, {}).get("camera")
        if cam is None:
            return None
        return cam.cache_usage_mb


class GeocodingProgressSensor(_BaseAlbumSensor):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:map-marker-check"

    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_geocoding_progress"
        self._attr_name = "Geocoding progress"

    @property
    def native_value(self) -> int | None:
        if self.coordinator.geocode_total > 0:
            return round(self.coordinator.geocode_done / self.coordinator.geocode_total * 100)
        if self.coordinator.exif_total > 0:
            return round(self.coordinator.exif_done / self.coordinator.exif_total * 100)
        return None

    @property
    def extra_state_attributes(self):
        from .const import CONF_REVERSE_GEOCODE
        geocoding_enabled = self.coordinator.entry.options.get(CONF_REVERSE_GEOCODE, True)
        if not geocoding_enabled:
            return {
                "phase": "geocoding",
                "status": "disabled",
            }
        if self.coordinator.geocode_total > 0 or self.coordinator.geocode_complete:
            status = "complete" if self.coordinator.geocode_complete else "running"
            return {
                "phase": "geocoding",
                "geocoded": self.coordinator.geocode_done,
                "total": self.coordinator.geocode_total,
                "status": status,
            }
        return {
            "phase": "reading EXIF",
            "scanned": self.coordinator.exif_done,
            "total": self.coordinator.exif_total,
            "status": "running" if self.coordinator.exif_total > 0 else "pending",
        }
