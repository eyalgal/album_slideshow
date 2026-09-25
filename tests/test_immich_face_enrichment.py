from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from custom_components.album_slideshow import coordinator as c
from custom_components.album_slideshow import image_processing as ip
from custom_components.album_slideshow import immich
from custom_components.album_slideshow.const import (
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_SELECTION_ID,
    CONF_IMMICH_SELECTION_TYPE,
    CONF_IMMICH_URL,
)


def _coordinator(selection_id: str):
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord.hass = object()
    coord.provider = "immich"
    coord.entry = SimpleNamespace(
        entry_id="test-immich",
        data={
            CONF_IMMICH_URL: "http://immich.test",
            CONF_IMMICH_API_KEY: "secret",
            CONF_IMMICH_SELECTION_TYPE: "composite",
            CONF_IMMICH_SELECTION_ID: selection_id,
        }
    )
    return coord


def _item():
    return c.MediaItem(
        url="http://immich.test/api/assets/a1/thumbnail?size=preview",
        width=1000,
        height=2000,
        mime_type=None,
        filename="portrait.jpg",
        source_id="a1",
    )


def test_face_focus_keeps_legacy_playlist_cache_readable():
    assert c.AlbumCoordinator._ITEM_CACHE_VERSION == 3
    coord = _coordinator('{"people": ["p1"]}')
    coord.provider = "google_shared"
    coord._enrichment_task = None
    coord._update_google_shared = AsyncMock(side_effect=c.UpdateFailed("Offline"))
    coord._items_cache_store = SimpleNamespace(async_load=AsyncMock(return_value={
        "title": "Cached album",
        "items": [{"url": "https://example.test/photo.jpg", "exif_scanned": True}],
    }))

    cached = asyncio.run(coord._async_update_data())

    assert cached["title"] == "Cached album"
    assert len(cached["items"]) == 1
    assert cached["items"][0].faces is None
    assert cached["items"][0].face_scanned is False


def test_immich_enrichment_adds_weighted_face_boxes(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, asset_id):
            assert asset_id == "a1"
            return {"exifInfo": {"description": "Portrait"}}

        async def async_get_faces(self, asset_id):
            assert asset_id == "a1"
            return [
                {
                    "imageWidth": 1000,
                    "imageHeight": 2000,
                    "boundingBoxX1": 100,
                    "boundingBoxY1": 200,
                    "boundingBoxX2": 300,
                    "boundingBoxY2": 600,
                    "person": {"id": "p1", "name": "Selected"},
                }
            ]

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"albums": [], "people": ["p1"], "favorites": false}')
    item = _item()

    asyncio.run(coord._enrich_immich_item(item))

    assert item.description == "Portrait"
    assert item.faces == [pytest.approx([0.1, 0.1, 0.3, 0.3, 0.04, True])]
    assert item.exif_scanned is True
    assert item.face_scanned is True


def test_immich_face_failure_keeps_metadata_and_center_fallback(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"exifInfo": {"description": "Still available"}}

        async def async_get_faces(self, _asset_id):
            raise PermissionError("face.read missing")

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"albums": [], "people": ["p1"], "favorites": false}')
    item = _item()

    asyncio.run(coord._enrich_immich_item(item))

    assert item.description == "Still available"
    assert item.faces is None  # unknown, not "no faces"
    assert item.exif_scanned is True
    assert item.face_scanned is False


def test_immich_album_only_source_stores_all_faces(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"exifInfo": {}}

        async def async_get_faces(self, _asset_id):
            return [
                {
                    "imageWidth": 1000,
                    "imageHeight": 2000,
                    "boundingBoxX1": 100,
                    "boundingBoxY1": 200,
                    "boundingBoxX2": 300,
                    "boundingBoxY2": 600,
                    "person": None,
                },
                {
                    "imageWidth": 1000,
                    "imageHeight": 2000,
                    "boundingBoxX1": 500,
                    "boundingBoxY1": 1000,
                    "boundingBoxX2": 700,
                    "boundingBoxY2": 1400,
                    "person": {"id": "someone", "name": "Someone"},
                },
            ]

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"albums": ["a1"], "people": [], "favorites": false}')
    item = _item()

    asyncio.run(coord._enrich_immich_item(item))

    assert item.faces == [
        pytest.approx([0.1, 0.1, 0.3, 0.3, 0.04, False]),
        pytest.approx([0.5, 0.5, 0.7, 0.7, 0.04, False]),
    ]
    assert item.exif_scanned is True


@pytest.mark.parametrize("failure", [PermissionError("face.read missing"), TimeoutError()])
@pytest.mark.parametrize("selection", ['{"people": ["p1"]}', '{"albums": ["a1"]}'])
def test_face_lookup_recovers_after_cached_failure(monkeypatch, failure, selection):
    face_calls = []
    asset_calls = []

    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, asset_id):
            asset_calls.append(asset_id)
            return {"exifInfo": {"description": "Portrait"}}

        async def async_get_faces(self, asset_id):
            face_calls.append(asset_id)
            if len(face_calls) == 1:
                raise failure
            return [{
                "imageWidth": 1000,
                "imageHeight": 2000,
                "boundingBoxX1": 100,
                "boundingBoxY1": 200,
                "boundingBoxX2": 300,
                "boundingBoxY2": 600,
                "person": {"id": "p1"},
            }]

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator(selection)
    coord.provider = "immich"
    coord._enrich_progress = {}
    coord._items_cache_store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    coord._geocode_items_background = AsyncMock()
    coord.async_set_updated_data = lambda _data: None

    async def refresh_twice():
        first = _item()
        await coord._enrich_items_background({"items": [first]})
        assert first.description == "Portrait"
        coord._items_cache_store.async_load.return_value = (
            coord._items_cache_store.async_save.call_args.args[0]
        )
        cached = await coord._load_cached_items()
        refreshed = _item()
        c._merge_prior_enrichment([refreshed], cached["items"])
        coord.hass = SimpleNamespace(
            async_create_background_task=lambda coroutine, **_kwargs: asyncio.create_task(coroutine)
        )
        coord._schedule_enrichment({"items": [refreshed]})
        await coord._enrichment_task
        return refreshed

    refreshed = asyncio.run(refresh_twice())
    assert len(face_calls) == 2, "A failed face lookup was permanently cached as complete"
    assert len(asset_calls) == 2
    assert refreshed.faces[0][:4] == pytest.approx([0.1, 0.1, 0.3, 0.3])
    assert refreshed.face_scanned is True


def test_legacy_exif_scanned_immich_item_still_gets_face_enrichment(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"isEdited": False}

        async def async_get_faces(self, _asset_id):
            return []

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"people": ["p1"]}')
    item = _item()
    item.exif_scanned = True
    assert coord._needs_enrichment(item) is True

    asyncio.run(coord._enrich_immich_item(item))

    assert item.exif_scanned is True
    assert item.face_scanned is True
    assert item.faces == []
    assert coord._needs_enrichment(item) is False


def test_v1120_focus_cache_is_reenriched_without_losing_playlist():
    coord = _coordinator('{"people": ["p1"]}')
    coord._items_cache_store = SimpleNamespace(async_load=AsyncMock(return_value={
        "items": [{
            "url": "http://immich.test/photo.jpg", "source_id": "a1",
            "exif_scanned": True, "face_scanned": True,
            "focus_x": 0.2, "focus_y": 0.3,
        }],
    }))

    cached = asyncio.run(coord._load_cached_items())

    assert len(cached["items"]) == 1
    assert cached["items"][0].exif_scanned is True
    assert cached["items"][0].face_scanned is False
    assert coord._needs_enrichment(cached["items"][0]) is True


@pytest.mark.parametrize("provider", ["google_shared", "local_folder", "media_source", "photoprism"])
def test_face_enrichment_does_not_rescan_other_providers(provider):
    coord = _coordinator('{"people": ["p1"]}')
    coord.provider = provider
    item = _item()
    item.exif_scanned = True
    assert coord._needs_enrichment(item) is False


def test_immich_cancelled_face_lookup_is_not_marked_complete(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"isEdited": False}

        async def async_get_faces(self, _asset_id):
            raise asyncio.CancelledError

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"people": ["p1"]}')
    item = _item()
    item.exif_scanned = True

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(coord._enrich_immich_item(item))

    assert item.face_scanned is False


@pytest.mark.parametrize("size", ["preview", "fullsize", "original"])
def test_edited_face_focus_matches_download_coordinates(size):
    from urllib.parse import parse_qs, urlsplit

    image_url = immich.build_image_url("http://immich.test", "a1", size)
    assert parse_qs(urlsplit(image_url).query).get("edited", ["false"]) == ["false"]
    mirrored_face_response = [{
        "imageWidth": 300,
        "imageHeight": 100,
        "boundingBoxX1": 20,
        "boundingBoxY1": 40,
        "boundingBoxX2": 40,
        "boundingBoxY2": 60,
        "person": {"id": "p1"},
    }]
    boxes = immich.parse_face_boxes(
        mirrored_face_response, {"p1"},
        edits=[{"action": "mirror", "parameters": {"axis": "vertical"}}],
    )
    with Image.new("RGB", (300, 100), "green") as original:
        original.paste("red", (260, 40, 280, 60))
        hints = ip.CropHints(faces=tuple(tuple(box) for box in boxes))
        with ip.render_image(original, "cover", 100, 100, hints) as actual:
            assert actual.getpixel((70, 50)) == (255, 0, 0), (
                "Edited face coordinates focus the opposite side of the unedited download"
            )


def test_immich_enrichment_maps_edited_faces_to_original(monkeypatch):
    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"isEdited": True, "exifInfo": {"exifImageWidth": 300, "exifImageHeight": 100}}

        async def async_get_faces(self, _asset_id):
            return [{
                "imageWidth": 300, "imageHeight": 100,
                "boundingBoxX1": 20, "boundingBoxY1": 40,
                "boundingBoxX2": 40, "boundingBoxY2": 60,
                "person": {"id": "p1"},
            }]

        async def async_get_asset_edits(self, asset_id):
            assert asset_id == "a1"
            return [{"action": "mirror", "parameters": {"axis": "vertical"}}]

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"people": ["p1"]}')
    item = _item()

    asyncio.run(coord._enrich_immich_item(item))

    assert item.faces[0][:4] == pytest.approx([260 / 300, 0.4, 280 / 300, 0.6])
    assert item.face_scanned is True
    assert "edited=" not in item.url


@pytest.mark.parametrize("failure", [PermissionError("asset.edit.get missing"), TimeoutError()])
def test_edited_face_metadata_failure_retries_safely(monkeypatch, failure):
    edit_calls = []

    class FakeClient:
        def __init__(self, *_args):
            pass

        async def async_get_asset(self, _asset_id):
            return {"isEdited": True}

        async def async_get_faces(self, _asset_id):
            return [{
                "imageWidth": 300, "imageHeight": 100,
                "boundingBoxX1": 20, "boundingBoxY1": 40,
                "boundingBoxX2": 40, "boundingBoxY2": 60,
                "person": {"id": "p1"},
            }]

        async def async_get_asset_edits(self, asset_id):
            edit_calls.append(asset_id)
            if len(edit_calls) == 1:
                raise failure
            return [{"action": "mirror", "parameters": {"axis": "vertical"}}]

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = _coordinator('{"people": ["p1"]}')
    first = _item()
    asyncio.run(coord._enrich_immich_item(first))
    assert first.faces is None
    assert first.face_scanned is False
    refreshed = _item()
    c._merge_prior_enrichment([refreshed], [first])
    assert coord._needs_enrichment(refreshed) is True

    asyncio.run(coord._enrich_immich_item(refreshed))

    assert len(edit_calls) == 2
    assert refreshed.faces[0][:4] == pytest.approx([260 / 300, 0.4, 280 / 300, 0.6])
    assert refreshed.face_scanned is True


@pytest.mark.parametrize("response", [None, {}, {"edits": None}, {"edits": {}}])
def test_immich_edit_client_rejects_unexpected_payloads(response):
    client = immich.ImmichClient.__new__(immich.ImmichClient)
    client._get = AsyncMock(return_value=response)
    with pytest.raises(ValueError):
        asyncio.run(client.async_get_asset_edits("a1"))
    client._get.assert_awaited_once_with("/api/assets/a1/edits")


def test_immich_edit_and_face_endpoints():
    client = immich.ImmichClient.__new__(immich.ImmichClient)
    edits = [{"action": "rotate", "parameters": {"angle": 90}}]
    client._get = AsyncMock(return_value={"edits": edits})
    assert asyncio.run(client.async_get_asset_edits("a1")) == edits
    client._get.assert_awaited_once_with("/api/assets/a1/edits")
    client._get = AsyncMock(return_value=[])
    assert asyncio.run(client.async_get_faces("a1")) == []
    client._get.assert_awaited_once_with("/api/faces?id=a1")
    client._get = AsyncMock(return_value={})
    with pytest.raises(ValueError):
        asyncio.run(client.async_get_faces("a1"))
