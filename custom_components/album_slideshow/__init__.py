from __future__ import annotations

import asyncio
import json
import logging
import os

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, SERVICE_NEXT_SLIDE, SERVICE_REFRESH_ALBUM, ATTR_ENTRY_ID
from .store import SlideshowStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["camera", "sensor", "button", "number", "select", "text", "switch"]

CARD_STATIC_PATH = "/album_slideshow_static"
CARD_FILE = "album-slideshow-card.js"


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the Lovelace card JS and register it as a frontend module.

    Idempotent: only the first config entry to load triggers registration
    for the HA session. The card lets dashboards cross-fade between slides
    in the browser (GPU compositor) instead of forcing the camera entity
    to render a JPEG burst on the event loop.
    """
    if hass.data.get(DOMAIN, {}).get("card_registered"):
        return

    integration_dir = os.path.dirname(__file__)
    www_dir = os.path.join(integration_dir, "www")
    card_path = os.path.join(www_dir, CARD_FILE)

    if not os.path.isfile(card_path):
        # Some HACS upgrade paths (and broken zip extractors) drop the
        # ``www/`` subdirectory. Try to recover by checking whether the
        # integration root has the file under a literal-backslash name
        # (a symptom of zips written with Windows path separators) or
        # directly at the root, and salvage it into ``www/`` so the
        # rest of the registration can proceed.
        recovered = await hass.async_add_executor_job(
            _recover_card_from_root, integration_dir, www_dir, card_path
        )
        if not recovered:
            _LOGGER.warning(
                "Album Slideshow card missing on disk (%s). Re-install"
                " the integration via HACS (3-dot menu -> Redownload)"
                " or copy %s/%s into the album_slideshow folder."
                " The custom:album-slideshow-card type will not be"
                " available until this is fixed.",
                card_path,
                "www",
                CARD_FILE,
            )
            return

    try:
        from homeassistant.components.http import StaticPathConfig

        await hass.http.async_register_static_paths(
            [StaticPathConfig(CARD_STATIC_PATH, www_dir, False)]
        )
    except Exception:  # noqa: BLE001 - many possible failure modes here
        _LOGGER.exception(
            "Failed to register static path for Album Slideshow card"
        )
        return

    # Cache-bust the card URL with the integration version so dashboards
    # always pick up the script that matches the running integration
    # rather than a stale copy from a previous release.
    version = await hass.async_add_executor_job(
        _read_manifest_version, integration_dir
    )
    card_url = f"{CARD_STATIC_PATH}/{CARD_FILE}"
    if version:
        card_url = f"{card_url}?v={version}"

    try:
        from homeassistant.components.frontend import add_extra_js_url

        add_extra_js_url(hass, card_url)
    except Exception:  # noqa: BLE001
        _LOGGER.exception(
            "Failed to add Album Slideshow card to Lovelace; you can"
            " still register it manually as a resource at %s",
            card_url,
        )
        return

    hass.data.setdefault(DOMAIN, {})["card_registered"] = True
    _LOGGER.info("Album Slideshow card registered at %s", card_url)


def _read_manifest_version(integration_dir: str) -> str | None:
    try:
        with open(
            os.path.join(integration_dir, "manifest.json"),
            "r",
            encoding="utf-8",
        ) as fh:
            return json.load(fh).get("version")
    except Exception:  # noqa: BLE001
        return None


def _recover_card_from_root(
    integration_dir: str, www_dir: str, card_path: str
) -> bool:
    """Salvage the card file from a broken extraction.

    PowerShell's ``Compress-Archive`` writes zip entries with backslash
    separators, which Linux unzip implementations may treat as literal
    filenames. The resulting layout is::

        custom_components/album_slideshow/www\\album-slideshow-card.js

    instead of the expected ``www/album-slideshow-card.js``. Move it to
    the right place so subsequent installs don't need a re-download.
    """
    candidates = [
        os.path.join(integration_dir, f"www\\{CARD_FILE}"),
        os.path.join(integration_dir, CARD_FILE),
    ]
    for src in candidates:
        if os.path.isfile(src):
            try:
                os.makedirs(www_dir, exist_ok=True)
                os.replace(src, card_path)
                _LOGGER.info(
                    "Recovered Album Slideshow card from %s", src
                )
                return True
            except OSError:
                _LOGGER.exception(
                    "Found candidate card at %s but could not move it"
                    " to %s",
                    src,
                    card_path,
                )
                return False
    return False


async def _async_cleanup_legacy_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    # Server-side transitions were tried in earlier 0.7-rc builds and
    # removed before the first public 0.7 pre-release because the
    # resource cost on low-end hardware was too high. Drop any leftover
    # transition entities so users don't see stale disabled rows under
    # the device.
    legacy_unique_ids = {
        f"{entry.entry_id}_max_items",
        f"{entry.entry_id}_transition",
        f"{entry.entry_id}_transition_duration_ms",
        f"{entry.entry_id}_transition_fps",
    }

    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id in legacy_unique_ids:
            registry.async_remove(entity.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import AlbumCoordinator

    hass.data.setdefault(DOMAIN, {})
    # Domain-wide concurrency limit on compose work. Multiple album cameras
    # share HA's small executor pool; without coordination they can all
    # decode + render in parallel and saturate the loop. One ticket means
    # at most one album does PIL work at a time, queueing the rest.
    if "compose_semaphore" not in hass.data[DOMAIN]:
        hass.data[DOMAIN]["compose_semaphore"] = asyncio.Semaphore(1)

    # Register the Lovelace card once per HA session. The card runs the
    # GPU-composited transitions in the browser so the camera entity
    # itself never has to render a transition burst on the event loop.
    await _async_register_card(hass)

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
        domain_data = hass.data.get(DOMAIN, {})
        domain_data.pop(entry.entry_id, None)
        # Drop the shared semaphore once the last album is gone so it's
        # re-created if the integration is re-added later.
        entry_keys = [
            k for k in domain_data.keys() if k != "compose_semaphore"
        ]
        if not entry_keys:
            domain_data.pop("compose_semaphore", None)
    return unload_ok
