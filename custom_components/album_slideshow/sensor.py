from __future__ import annotations

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, PROVIDER_GOOGLE_SHARED, PROVIDER_LOCAL_FOLDER
from .coordinator import AlbumCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = [
        AlbumCountSensor(entry, coordinator),
        AlbumTitleSensor(entry, coordinator),
        CacheUsageSensor(entry, coordinator),
    ]
    if coordinator.provider == PROVIDER_LOCAL_FOLDER:
        # Diagnostic surface for the local-folder background enrichment
        # (EXIF reads + reverse-geocode). For Google albums there's no
        # enrichment work, so this sensor is omitted to keep the device
        # screen tidy.
        entities.append(EnrichmentProgressSensor(entry, coordinator))
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


class EnrichmentProgressSensor(_BaseAlbumSensor):
    """Percent-complete sensor for the local-folder enrichment worker.

    Reports the slower-changing of the two phases:
    - ``exif``: reading capture date + GPS from EXIF tags.
    - ``geocoding``: reverse-geocoding GPS to a city/country label.

    Holds at ``100`` when both phases finish and stays there until the
    next coordinator refresh discovers new files. The ``phase`` and
    raw counts are surfaced as state attributes so dashboards can show
    progress text alongside the bar.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:map-marker-radius"

    def __init__(self, entry: ConfigEntry, coordinator: AlbumCoordinator) -> None:
        super().__init__(entry, coordinator)
        self._attr_unique_id = f"{entry.entry_id}_enrichment_progress"
        self._attr_name = "Enrichment progress"

    @property
    def native_value(self):
        prog = getattr(self.coordinator, "_enrich_progress", None) or {}
        phase = prog.get("phase")
        if phase == "geocoding":
            total = prog.get("geocode_total") or 0
            done = prog.get("geocode_done") or 0
        else:
            total = prog.get("exif_total") or 0
            done = prog.get("exif_done") or 0
        if total <= 0:
            return None
        return max(0, min(100, round(100 * done / total)))

    @property
    def extra_state_attributes(self):
        prog = getattr(self.coordinator, "_enrich_progress", None) or {}
        return {
            "phase": prog.get("phase"),
            "exif_total": prog.get("exif_total", 0),
            "exif_done": prog.get("exif_done", 0),
            "geocode_total": prog.get("geocode_total", 0),
            "geocode_done": prog.get("geocode_done", 0),
        }
