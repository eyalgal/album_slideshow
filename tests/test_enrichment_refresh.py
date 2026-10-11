"""Metadata enrichment: fetched on demand per slide, shared, and saved sparingly."""
from __future__ import annotations

import asyncio
import random
import types

from custom_components.album_slideshow import camera
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.store import SlideshowStore


class _Hass:
    def __init__(self):
        self.data = {}

    def async_create_background_task(self, coro, name):
        return asyncio.ensure_future(coro)

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _item(name: str) -> MediaItem:
    return MediaItem(
        url=f"http://immich.test/{name}", width=None, height=None, mime_type=None,
        filename=name, source_id=name,
    )


def _coordinator(provider="immich"):
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = _Hass()
    coord.provider = provider
    coord.entry = types.SimpleNamespace(entry_id="entry", title="Trip", data={})
    coord._enrich_progress = {"exif_done": 0}
    fetched = []

    async def enrich_immich(item):
        fetched.append(item.filename)
        await asyncio.sleep(0)
        item.exif_scanned = item.face_scanned = True
        item.location = f"{item.filename} place"
        item.faces = [[0.4, 0.2, 0.6, 0.5, 0.06, False]]

    coord._enrich_immich_item = enrich_immich
    return coord, fetched


# ── Coordinator ─────────────────────────────────────────────────────────────


def test_concurrent_requests_for_one_item_share_a_single_fetch():
    coord, fetched = _coordinator()
    item = _item("a")

    async def main():
        return await asyncio.gather(coord.async_enrich_item(item), coord.async_enrich_item(item))

    assert asyncio.run(main()) == [True, True]
    assert fetched == ["a"]
    assert coord._enrich_progress["exif_done"] == 1
    # Already enriched: nothing more to do.
    assert asyncio.run(coord.async_enrich_item(item)) is False
    assert fetched == ["a"]


def test_google_items_are_left_to_the_google_pass():
    coord, fetched = _coordinator(provider="google_shared")
    assert asyncio.run(coord.async_enrich_item(_item("a"))) is False
    assert fetched == []


def test_background_pass_skips_shown_items_and_saves_sparingly():
    coord, fetched = _coordinator()
    items = [_item(f"p{index}") for index in range(60)]
    # The camera already enriched one on demand; the pass must skip it.
    items[3].exif_scanned = items[3].face_scanned = True
    saves, updates = [], []

    async def save(data):
        saves.append(len(data["items"]))

    async def no_geocode(data):
        return None

    coord._save_cached_items = save
    coord._geocode_items_background = no_geocode
    coord.async_set_updated_data = updates.append
    asyncio.run(coord._enrich_items_background({"items": items}))

    assert "p3" not in fetched and len(fetched) == 59
    # Listeners still hear about every batch of 25 (plus the tail)...
    assert len(updates) == 3
    # ...but the whole-library cache is written once during the pass plus
    # the final flush, not once per batch.
    assert len(saves) == 2


# ── Camera ──────────────────────────────────────────────────────────────────


def test_slides_are_rendered_with_their_metadata(monkeypatch):
    from PIL import Image
    import io

    buffer = io.BytesIO()
    Image.new("RGB", (160, 90), "gray").save(buffer, "JPEG")
    jpeg = buffer.getvalue()

    async def fetch(_self, url):
        return jpeg

    monkeypatch.setattr(camera.AlbumSlideshowCamera, "_fetch_bytes", fetch)
    item = _item("a")
    item.width, item.height = 160, 90
    seen_at_render = []
    original_hints = camera.AlbumSlideshowCamera._crop_hints

    def hints(self, it):
        seen_at_render.append(it.location)
        return original_hints(self, it)

    monkeypatch.setattr(camera.AlbumSlideshowCamera, "_crop_hints", hints)
    coord, fetched = _coordinator()
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = _Hass()
    cam.entry = types.SimpleNamespace(entry_id="entry", title="Trip")
    cam.coordinator = coord
    cam.store = SlideshowStore()
    cam._download_cache = camera._DownloadCache(1024 * 1024)
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cam._rng = random.Random(1)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), [item], advance=False))
    assert fetched == ["a"]
    assert seen_at_render == ["a place"]
    # The slide records the faces it was cropped with, so the background
    # pass reaching this photo later doesn't count as new face data.
    assert frame.meta["faces"] == (camera._item_faces(item),)
    assert frame.meta["faces"] != (None,)


def test_a_slow_metadata_fetch_does_not_hold_up_the_slide(monkeypatch):
    monkeypatch.setattr(camera, "_ENRICH_WAIT_SECONDS", 0.01)
    finished = []

    async def slow(item):
        await asyncio.sleep(0.05)
        finished.append(item)
        return True

    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.coordinator = types.SimpleNamespace(async_enrich_item=slow)

    async def main():
        await cam._ensure_enriched("item")
        assert finished == []
        # The fetch carries on in the background for the next render.
        await asyncio.sleep(0.1)
        return finished

    assert asyncio.run(main()) == ["item"]
