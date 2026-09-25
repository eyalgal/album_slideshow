from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest

from custom_components.album_slideshow import camera, coordinator, google_scraper
from custom_components.album_slideshow.store import SlideshowStore
from custom_components.album_slideshow import _register_photo_services
from custom_components import album_slideshow as integration
from custom_components.album_slideshow import store as store_module
from tests.test_camera_next_slide import _FakeHass


def _item(source_id, url="https://example.com/photo?token=first", filename="same.jpg"):
    return coordinator.MediaItem(
        url=url, width=40, height=80, mime_type="image/jpeg",
        filename=filename, source_id=source_id,
    )


def test_photo_id_ignores_download_url_quality_and_filename():
    first = _item("asset-a")
    second = _item("asset-a", "https://other.example/photo?token=renewed", "renamed.jpg")
    assert first.photo_id == second.photo_id
    assert first.photo_id != _item("asset-b").photo_id
    assert first.photo_id.startswith("v1:")
    assert "asset-a" not in first.photo_id
    assert "token" not in first.photo_id


def test_unknown_identity_is_not_guessed_from_filename_or_signed_url():
    assert _item(None).photo_id is None


def test_google_scraper_retains_id_shared_with_fallback():
    item = google_scraper._parse_album_item(["google-photo-id", ["https://lh3.googleusercontent.com/photo", 40, 80]])
    assert item.source_id == "google-photo-id"
    assert item.photo_id == _item("google-photo-id").photo_id


def test_photo_id_survives_cached_item_round_trip():
    saved = {}

    class Storage:
        async def async_save(self, value):
            saved.update(value)

        async def async_load(self):
            return saved

    coord = coordinator.AlbumCoordinator.__new__(coordinator.AlbumCoordinator)
    coord._items_cache_store = Storage()
    item = _item("https://nextcloud.example/dav/share-secret/photo.jpg")
    item.photo_id = coordinator._photo_identifier("nextcloud-file-id")

    async def run():
        await coord._save_cached_items({"title": "Test", "items": [item]})
        restored = await coord._load_cached_items()
        assert restored["items"][0].photo_id == item.photo_id

    asyncio.run(run())


def test_local_identity_distinguishes_same_filename_in_different_folders(tmp_path):
    for directory in ("first", "second"):
        folder = tmp_path / directory
        folder.mkdir()
        (folder / "same.jpg").write_bytes(b"placeholder")

    class Hass:
        async def async_add_executor_job(self, func, *args):
            return func(*args)

    coord = coordinator.AlbumCoordinator.__new__(coordinator.AlbumCoordinator)
    coord.hass = Hass()
    coord.local_path = str(tmp_path)
    coord.recursive = True
    result = asyncio.run(coord._update_local_folder())
    assert len({item.photo_id for item in result["items"]}) == 2
    assert all(item.source_id.startswith("file://") for item in result["items"])


async def _camera(items, monkeypatch, *, paired=False):
    monkeypatch.setattr(camera.ip, "resolve_output_size", lambda *args: (80, 40))
    hass = _FakeHass()
    store = SlideshowStore(order_mode="album_order", portrait_mode="pair" if paired else "single")
    await store.async_load_hidden_photos(hass, "test")
    coord = SimpleNamespace(data={"items": items}, async_add_listener=lambda listener: None)
    cam = camera.AlbumSlideshowCamera(
        hass, SimpleNamespace(entry_id="test", title="Test"), coord, store
    )
    cam.async_write_ha_state = lambda: None
    cam._schedule_preload = lambda: None
    for index, item in enumerate(items):
        with Image.new("RGB", (40, 80), (index * 60 % 255, 70, 120)) as image:
            output = BytesIO()
            image.save(output, format="JPEG")
            cam._download_cache.put(item.url, output.getvalue())
    await cam._rebuild_current_frame()
    return cam


def test_debug_overlay_rebuilds_buffers_without_changing_exclusions(monkeypatch):
    async def run():
        cam = await _camera([_item("photo-a")], monkeypatch)
        original = cam._current_frame.data
        hidden = cam.store.hidden_photo_ids
        cam._next_frames.append(cam._current_frame)

        cam.store.face_debug = True
        cam.store.notify()

        assert cam._timeline_dirty
        assert cam._crop_hints(None).debug is True
        await cam._rebuild_current_frame()
        assert cam._current_frame.data != original
        assert cam.store.hidden_photo_ids == hidden

        cam.store.face_debug = False
        cam.store.notify()
        await cam._rebuild_current_frame()
        assert cam._current_frame.data == original

    asyncio.run(run())


def test_unload_stops_old_source_enrichment():
    async def run():
        coord = SimpleNamespace(_cancel_enrichment=AsyncMock())
        hass = SimpleNamespace(
            data={integration.DOMAIN: {"entry": {"coordinator": coord}}},
            config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)),
        )

        assert await integration.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))

        coord._cancel_enrichment.assert_awaited_once()
        assert "entry" not in hass.data[integration.DOMAIN]

    asyncio.run(run())


def test_hide_clears_history_and_preloads_and_filters_the_cached_pool(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(4)]
        cam = await _camera(items, monkeypatch)
        await cam._show_next_frame()
        old_frame = cam._current_frame
        cam._next_frames.append(old_frame)
        assert cam._previous_frames
        await cam.async_hide_photo()
        assert old_frame.cursor.index == 1
        assert items[1].photo_id in cam.store.hidden_photo_ids
        assert not cam._previous_frames
        assert not cam._next_frames
        assert await cam._show_previous_frame() is False
        assert items[1] not in cam._effective_items()
        assert cam._effective_items() is cam._effective_items()
        assert items[1].photo_id not in cam._current_frame.meta["photo_ids"]

    asyncio.run(run())


def test_hide_reuses_safe_preloaded_frame_without_rendering(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(4)]
        cam = await _camera(items, monkeypatch)
        next_frame = await cam._render_available_frame(
            cam._current_frame.cursor, items, advance=True
        )
        following_frame = await cam._render_available_frame(
            next_frame.cursor, items, advance=True
        )
        cam._next_frames.extend([next_frame, following_frame])
        renderer = AsyncMock(side_effect=AssertionError("Hide tried to render a ready frame"))
        cam._render_available_frame = renderer

        await cam.async_hide_photo()

        renderer.assert_not_awaited()
        assert cam._framebuffer == next_frame.data
        assert cam._current_frame.meta == next_frame.meta
        assert cam._index == 0
        assert cam._effective_items()[cam._index].photo_id == items[1].photo_id
        assert len(cam._next_frames) == 1
        assert cam._next_frames[0].data == following_frame.data
        assert not cam._previous_frames
        assert cam.store.hidden_photo_ids == {items[0].photo_id}

    asyncio.run(run())


def test_hide_rejects_pairs_with_a_hidden_half_and_reuses_the_next_safe_pair(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(4)]
        for index, item in enumerate(items):
            item.description = f"Caption {index}"
        cam = await _camera(items, monkeypatch, paired=True)
        unsafe = await cam._render_available_frame(cam._current_frame.cursor, items, advance=True)
        safe = await cam._render_available_frame(unsafe.cursor, items, advance=True)
        assert unsafe.meta["photo_ids"] == [items[1].photo_id, items[2].photo_id]
        assert safe.meta["photo_ids"] == [items[2].photo_id, items[3].photo_id]
        cam._next_frames.extend([unsafe, safe])
        renderer = AsyncMock(side_effect=AssertionError("A safe pair was already buffered"))
        cam._render_available_frame = renderer

        await cam.async_hide_photo(position="second")

        renderer.assert_not_awaited()
        assert cam._framebuffer == safe.data
        assert cam._last_pair_frames == safe.meta["pair_frames"]
        assert cam._index == 1
        assert cam.extra_state_attributes["description"] == "Caption 2"
        assert cam.extra_state_attributes["displayed_photo_ids"] == safe.meta["photo_ids"]
        assert not cam._previous_frames
        assert not cam._next_frames

    asyncio.run(run())


@pytest.mark.parametrize("position, remaining_index", [("first", 1), ("second", 0)])
def test_hide_from_two_photo_pair_uses_cached_surviving_half(monkeypatch, position, remaining_index):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(2)]
        cam = await _camera(items, monkeypatch, paired=True)
        paired_frame = cam._current_frame
        await cam._preload_loop(cam._timeline_generation)
        assert all(len(frame.meta["photo_ids"]) == 2 for frame in cam._next_frames)
        cam._download_cache = camera._DownloadCache(1)
        renderer = AsyncMock(side_effect=AssertionError("The surviving half was already rendered"))
        cam._render_available_frame = renderer

        await cam.async_hide_photo(position=position)

        renderer.assert_not_awaited()
        assert cam._current_frame.meta["photo_ids"] == [items[remaining_index].photo_id]
        assert cam._last_pair_frames is None
        assert cam._last_pair_orientation is None
        assert cam._framebuffer and cam._framebuffer != paired_frame.data
        assert not cam._previous_frames
        assert not cam._next_frames
        assert cam.extra_state_attributes["empty_reason"] is None

    asyncio.run(run())


def test_pair_fallback_rejects_a_frame_invalidated_while_processing(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(2)]
        cam = await _camera(items, monkeypatch, paired=True)
        original = camera.ip.render_pair_photo

        def invalidate_while_processing(*args):
            cam._invalidate_timeline()
            return original(*args)

        monkeypatch.setattr(camera.ip, "render_pair_photo", invalidate_while_processing)
        renderer = AsyncMock(wraps=cam._render_available_frame)
        cam._render_available_frame = renderer

        await cam.async_hide_photo(position="first")

        renderer.assert_awaited_once()
        assert cam._current_frame.meta["photo_ids"] == [items[1].photo_id]
        assert not cam._timeline_dirty

    asyncio.run(run())


def test_corrupt_cached_pair_falls_back_to_normal_rendering(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(2)]
        cam = await _camera(items, monkeypatch, paired=True)
        frame = cam._current_frame
        cam._current_frame = camera._RenderedFrame(b"invalid-jpeg", frame.cursor, frame.meta)
        renderer = AsyncMock(wraps=cam._render_available_frame)
        cam._render_available_frame = renderer

        await cam.async_hide_photo(position="first")

        renderer.assert_awaited_once()
        assert cam._current_frame.meta["photo_ids"] == [items[1].photo_id]
        assert cam._framebuffer.startswith(b"\xff\xd8")

    asyncio.run(run())


def test_safe_buffer_reuse_keeps_next_and_previous_metadata_consistent(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(5)]
        cam = await _camera(items, monkeypatch)
        first = await cam._render_available_frame(cam._current_frame.cursor, items, advance=True)
        second = await cam._render_available_frame(first.cursor, items, advance=True)
        cam._next_frames.extend([first, second])

        await cam.async_hide_photo()
        assert cam._framebuffer == first.data
        assert await cam._show_next_frame()
        assert cam._framebuffer == second.data
        assert cam._index == 1
        assert cam._effective_items()[cam._index].photo_id == second.meta["photo_ids"][0]
        assert await cam._show_previous_frame()
        assert cam._framebuffer == first.data
        assert cam._index == 0
        await cam._preload_loop(cam._timeline_generation)
        assert len(cam._next_frames) == cam._buffer_depth
        for frame in [cam._current_frame, *cam._previous_frames, *cam._next_frames]:
            assert items[0].photo_id not in frame.meta["photo_ids"]
            assert cam._effective_items()[frame.cursor.index].photo_id == frame.meta["photo_ids"][0]

    asyncio.run(run())


def test_restore_preserves_current_pixels_and_remaps_the_cursor(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        await cam.async_hide_photo()
        current = cam._current_frame
        assert current.meta["photo_ids"] == [items[1].photo_id]
        renderer = AsyncMock(side_effect=AssertionError("Restoring a photo rerendered a valid frame"))
        cam._render_available_frame = renderer

        await cam.async_restore_photos(undo=True)

        renderer.assert_not_awaited()
        assert cam._framebuffer == current.data
        assert cam._current_frame.meta == current.meta
        assert cam._index == 1
        assert not cam.store.hidden_photo_ids

    asyncio.run(run())


@pytest.mark.parametrize("invalidated_during_save", [False, True])
def test_exclusion_reuse_rejects_frames_invalidated_by_other_changes(monkeypatch, invalidated_during_save):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        safe = await cam._render_available_frame(cam._current_frame.cursor, items, advance=True)
        cam._next_frames.append(safe)
        if invalidated_during_save:
            async def save(data):
                cam._invalidate_timeline()
            monkeypatch.setattr(cam.store._hidden_storage, "async_save", save)
        else:
            cam._timeline_dirty = True
        renderer = AsyncMock(wraps=cam._render_available_frame)
        cam._render_available_frame = renderer

        await cam.async_hide_photo()

        renderer.assert_awaited_once()
        assert items[0].photo_id not in cam._current_frame.meta["photo_ids"]
        assert not cam._next_frames

    asyncio.run(run())


def test_failed_hide_save_keeps_the_original_buffer(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        current = cam._current_frame
        next_frame = await cam._render_available_frame(current.cursor, items, advance=True)
        cam._next_frames.append(next_frame)
        monkeypatch.setattr(cam.store._hidden_storage, "async_save", AsyncMock(side_effect=OSError("Disk unavailable")))

        with pytest.raises(OSError, match="Disk unavailable"):
            await cam.async_hide_photo()

        assert cam._current_frame is current
        assert list(cam._next_frames) == [next_frame]
        assert not cam.store.hidden_photo_ids

    asyncio.run(run())


def test_explicit_photo_id_hides_the_clicked_photo_after_slide_changes(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        clicked_id = cam._current_frame.meta["photo_ids"][0]
        previous_frame_id = cam._frame_id
        await cam._show_next_frame()
        with pytest.raises(ValueError, match="slide changed"):
            await cam.async_hide_photo(frame_id=previous_frame_id)
        await cam.async_hide_photo(photo_ids=[clicked_id])
        assert cam.store.hidden_photo_ids == {items[0].photo_id}

    asyncio.run(run())


@pytest.mark.parametrize("position, hidden_indices", [("first", [0]), ("second", [1]), ("both", [0, 1])])
def test_pair_hiding_requires_an_explicit_side(monkeypatch, position, hidden_indices):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(4)]
        cam = await _camera(items, monkeypatch, paired=True)
        assert cam._current_frame.meta["photo_ids"] == [items[0].photo_id, items[1].photo_id]
        with pytest.raises(ValueError, match="paired"):
            await cam.async_hide_photo()
        await cam.async_hide_photo(position=position)
        expected = {items[index].photo_id for index in hidden_indices}
        assert cam.store.hidden_photo_ids == expected
        assert not expected.intersection(cam._current_frame.meta["photo_ids"])
        await cam.async_restore_photos(undo=True)
        assert not cam.store.hidden_photo_ids

    asyncio.run(run())


def test_hide_last_photo_blanks_camera_and_undo_restores_it(monkeypatch):
    async def run():
        item = _item("last-photo")
        cam = await _camera([item], monkeypatch)
        previous_bytes = cam._framebuffer
        await cam.async_hide_photo()
        assert cam._current_frame is None
        assert cam._framebuffer and cam._framebuffer != previous_bytes
        assert cam.store.last_frame is None
        assert cam.extra_state_attributes["empty_reason"] == "all_hidden"
        assert cam.extra_state_attributes["displayed_photo_ids"] == []
        assert await cam._show_next_frame() is False
        await cam.async_restore_photos(undo=True)
        assert cam._current_frame.meta["photo_ids"] == [item.photo_id]
        assert cam.extra_state_attributes["empty_reason"] is None

    asyncio.run(run())


def test_failed_replacement_does_not_keep_hidden_frame_visible(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(2)]
        cam = await _camera(items, monkeypatch)
        previous_bytes = cam._framebuffer

        async def fail_render(*args, **kwargs):
            raise RuntimeError("Download failed")

        cam._render_available_frame = fail_render
        await cam.async_hide_photo()
        assert cam._current_frame is None
        assert cam._framebuffer and cam._framebuffer != previous_bytes
        assert items[0].photo_id in cam.store.hidden_photo_ids

    asyncio.run(run())


def test_photo_services_target_entry_and_paginate_without_exposing_urls():
    from homeassistant.exceptions import ServiceValidationError
    import voluptuous as vol

    handlers = {}

    class Services:
        def has_service(self, domain, name):
            return name in handlers

        def async_register(self, domain, name, handler, **options):
            handlers[name] = (handler, options)

    photos = [_item(f"photo-{index}", filename=f"Photo {index}.jpg") for index in range(3)]
    cam = SimpleNamespace(
        store=SimpleNamespace(hidden_photo_ids={item.photo_id for item in photos}),
        coordinator=SimpleNamespace(data={"items": photos}),
        async_hide_photo=AsyncMock(), async_restore_photos=AsyncMock(),
    )
    hass = SimpleNamespace(data={"album_slideshow": {"test": {"camera": cam}}}, services=Services())
    _register_photo_services(hass)
    _register_photo_services(hass)
    assert len(handlers) == 5

    async def call(name, **data):
        handler, options = handlers[name]
        return await handler(SimpleNamespace(data=options["schema"](data)))

    async def run():
        result = await call("list_hidden_photos", entry_id="test", limit=2, offset=1)
        assert result["total"] == 3
        assert len(result["photos"]) == 2
        assert "url" not in str(result)
        assert "token" not in str(result)
        assert handlers["list_hidden_photos"][1]["supports_response"] == "only"
        await call("hide_photo", entry_id="test", photo_ids=[photos[0].photo_id])
        cam.async_hide_photo.assert_awaited_once_with(photo_ids=[photos[0].photo_id], position=None, frame_id=None)
        await call("undo_hide", entry_id="test")
        cam.async_restore_photos.assert_awaited_with(undo=True)
        await call("restore_photos", entry_id="test", photo_ids=[photos[0].photo_id])
        cam.async_restore_photos.assert_awaited_with([photos[0].photo_id])
        await call("restore_all_photos", entry_id="test")
        cam.async_restore_photos.assert_awaited_with()
        with pytest.raises(ServiceValidationError, match="not loaded"):
            await call("hide_photo", entry_id="other")
        with pytest.raises(vol.Invalid):
            await call("list_hidden_photos", entry_id="test", limit=1000)

    asyncio.run(run())


def test_hide_cancels_an_inflight_preload(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        started = asyncio.Event()
        original_render = cam._render_available_frame

        async def render(cursor, media, *, advance):
            if advance:
                started.set()
                await asyncio.Event().wait()
            return await original_render(cursor, media, advance=advance)

        cam._render_available_frame = render
        preload = asyncio.create_task(cam._preload_loop(cam._timeline_generation))
        cam._preload_task = preload
        await started.wait()
        await cam.async_hide_photo()
        with pytest.raises(asyncio.CancelledError):
            await preload
        assert not cam._next_frames
        assert cam._preload_task is None
        assert items[0].photo_id not in cam._current_frame.meta["photo_ids"]

    asyncio.run(run())


def test_hide_cached_photo_does_not_wait_for_another_albums_download(monkeypatch):
    async def run():
        items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(3)]
        cam = await _camera(items, monkeypatch)
        other = await _camera(items, monkeypatch)
        other.hass = cam.hass
        download_started = asyncio.Event()
        release_download = asyncio.Event()
        original_fetch = camera.AlbumSlideshowCamera._fetch_bytes

        async def fetch(renderer, url):
            if renderer.coordinator is other.coordinator:
                download_started.set()
                await release_download.wait()
            return await original_fetch(renderer, url)

        monkeypatch.setattr(camera.AlbumSlideshowCamera, "_fetch_bytes", fetch)
        unrelated = asyncio.create_task(
            other._render_available_frame(other._capture_cursor(), items, advance=False)
        )
        try:
            await asyncio.wait_for(download_started.wait(), timeout=1)
            await asyncio.wait_for(cam.async_hide_photo(), timeout=1)
            assert cam._current_frame is not None
            assert items[0].photo_id not in cam._current_frame.meta["photo_ids"]
            assert not unrelated.done()
        finally:
            release_download.set()
            await unrelated

    asyncio.run(run())


@pytest.mark.parametrize("hide_all", [False, True])
def test_startup_filters_restored_exclusions_before_first_frame(monkeypatch, hide_all):
    items = [_item(f"photo-{index}", f"https://example.com/{index}") for index in range(2)]
    excluded = [item.photo_id for item in items] if hide_all else [items[0].photo_id]

    class Storage:
        def __init__(self, *args):
            pass

        async def async_load(self):
            return {"hidden": excluded, "last_hidden": excluded}

    monkeypatch.setattr(store_module, "Store", Storage)

    async def run():
        cam = await _camera(items, monkeypatch)
        if hide_all:
            assert cam._current_frame is None
            assert cam.extra_state_attributes["empty_reason"] == "all_hidden"
        else:
            assert cam._current_frame.meta["photo_ids"] == [items[1].photo_id]

    asyncio.run(run())


def test_storage_load_failure_blocks_integration_startup(monkeypatch):
    from homeassistant.exceptions import ConfigEntryNotReady

    monkeypatch.setattr(integration, "_async_register_card", AsyncMock())
    monkeypatch.setattr(integration, "_async_cleanup_legacy_entities", AsyncMock())
    monkeypatch.setattr(SlideshowStore, "async_load_hidden_photos", AsyncMock(side_effect=OSError("Storage unavailable")))
    constructor = AsyncMock()
    monkeypatch.setattr(coordinator, "AlbumCoordinator", constructor)
    hass = SimpleNamespace(data={})
    with pytest.raises(ConfigEntryNotReady, match="slideshow has not started"):
        asyncio.run(integration.async_setup_entry(hass, SimpleNamespace(entry_id="test")))
    constructor.assert_not_called()


def test_media_source_identity_uses_content_id_not_renewable_url(monkeypatch):
    import homeassistant.components as components
    from tests.test_media_source import _FakeMediaSource, _node

    source = _FakeMediaSource({"root": [_node("media-source://test/photo", media_class="image")]})
    monkeypatch.setattr(components, "media_source", source, raising=False)
    coord = coordinator.AlbumCoordinator.__new__(coordinator.AlbumCoordinator)
    coord.hass = object()
    coord.entry = SimpleNamespace(title="Test")
    coord.media_content_id = "root"
    coord._internal_base_url = lambda: "http://ha.test"

    async def run():
        coord._sign_media_path = lambda path: path + "?authSig=old"
        first = (await coord._update_media_source())["items"][0]
        coord._sign_media_path = lambda path: path + "?authSig=new"
        second = (await coord._update_media_source())["items"][0]
        assert first.url != second.url
        assert first.source_id == "media-source://test/photo"
        assert first.photo_id == second.photo_id

    asyncio.run(run())


def test_missing_identity_cannot_bypass_an_existing_exclusion_list(monkeypatch):
    async def run():
        identified = _item("known-photo")
        unidentified = _item(None, "https://example.com/unknown")
        cam = await _camera([identified, unidentified], monkeypatch)
        await cam.async_hide_photo()
        assert not cam._effective_items()
        assert cam._current_frame is None
        await cam.async_restore_photos(undo=True)
        assert len(cam._effective_items()) == 2

    asyncio.run(run())


def test_card_controls_with_node():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for card control tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).with_name("test_card.cjs"))],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr