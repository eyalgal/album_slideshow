"""Immich video slides: asset queries, playlist items, timing, and the proxy."""
from __future__ import annotations

import asyncio
from collections import deque
import io
import random
import types

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import pytest

from custom_components.album_slideshow import camera, immich, video
from custom_components.album_slideshow import config_flow as cf
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow.const import (
    CONF_IMMICH_API_KEY,
    CONF_IMMICH_IMAGE_SIZE,
    CONF_IMMICH_INCLUDE_VIDEOS,
    CONF_IMMICH_SELECTION_ID,
    CONF_IMMICH_SELECTION_TYPE,
    CONF_IMMICH_URL,
    DOMAIN,
    ORIENTATION_MISMATCH_PAIR,
)
from custom_components.album_slideshow.coordinator import MediaItem
from custom_components.album_slideshow.store import SlideshowStore


# ── Immich parsing and query bodies ────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (12345, 12345),           # current servers: integer milliseconds
        (12345.6, 12346),
        (0, None),
        (-5, None),
        (True, None),
        (float("nan"), None),
        ("0:00:12.345000", 12345),  # older servers: H:MM:SS string
        ("1:02:03.5", 3723500),
        ("0:00:00.00000", None),
        ("", None),
        (None, None),
        ("12.5", None),
        ("0:xx:01", None),
        ("0:00:nan", None),
    ],
)
def test_parse_duration_ms(raw, expected):
    assert immich.parse_duration_ms(raw) == expected


def test_build_video_url_uses_playback_endpoint_without_key():
    url = immich.build_video_url("http://immich.test/api/", "abc")
    assert url == "http://immich.test/api/assets/abc/video/playback"


def test_videos_are_filtered_out_unless_enabled():
    payload = {"assets": {"items": [
        {"id": "i", "type": "IMAGE"},
        {"id": "v", "type": "VIDEO"},
        {"id": "a", "type": "AUDIO"},
        {"id": "t", "type": "VIDEO", "isTrashed": True},
    ], "nextPage": None}}
    assert [it["id"] for it in immich.parse_search_page(payload)[0]] == ["i"]
    assert [it["id"] for it in immich.parse_search_page(payload, True)[0]] == ["i", "v"]
    assert [it["id"] for it in immich.parse_random(payload["assets"]["items"], True)] == ["i", "v"]


def test_query_bodies_drop_the_image_type_when_videos_are_enabled():
    assert immich.build_search_body("album", "a1", None) == {"type": "IMAGE", "albumIds": ["a1"]}
    assert immich.build_search_body("album", "a1", None, True) == {"albumIds": ["a1"]}
    bodies = immich.build_composite_bodies(
        '{"albums": ["a1"], "people": [], "favorites": true}', None, True
    )
    assert bodies == [{"albumIds": ["a1"]}, {"isFavorite": True}]
    assert immich.build_composite_bodies(None, None, True) == [{}]


def test_custom_filter_type_is_honoured_only_when_videos_are_enabled():
    only_videos = {"type": "VIDEO", "city": "Paris"}
    assert immich.build_search_body("search", None, only_videos)["type"] == "IMAGE"
    assert immich.build_search_body("search", None, only_videos, True)["type"] == "VIDEO"
    other = immich.build_search_body("search", None, {"type": "OTHER"}, True)
    assert "type" not in other


def test_random_selection_requests_both_types_when_enabled():
    class Client(immich.ImmichClient):
        def __init__(self):
            self.bodies = []

        async def _post(self, path, body):
            self.bodies.append(body)
            return [{"id": "i", "type": "IMAGE"}, {"id": "v", "type": "VIDEO"}]

    client = Client()
    out = asyncio.run(client.async_collect_assets("random", include_videos=True))
    assert [it["id"] for it in out] == ["i", "v"]
    assert "type" not in client.bodies[0]
    asyncio.run(client.async_collect_assets("random"))
    assert client.bodies[1]["type"] == "IMAGE"


# ── Coordinator items ──────────────────────────────────────────────────────


def _coordinator(monkeypatch, assets, *, include_videos=True, size="fullsize"):
    calls = []

    class FakeClient:
        def __init__(self, _hass, url, _key):
            self.base_url = url
            self.image_headers = {"x-api-key": "secret"}

        async def async_collect_assets(self, *args):
            calls.append(args)
            return assets

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = object()
    coord.entry = types.SimpleNamespace(title="Trip", data={
        CONF_IMMICH_URL: "http://immich.test",
        CONF_IMMICH_API_KEY: "secret",
        CONF_IMMICH_SELECTION_TYPE: "composite",
        CONF_IMMICH_SELECTION_ID: "{}",
        CONF_IMMICH_IMAGE_SIZE: size,
        CONF_IMMICH_INCLUDE_VIDEOS: include_videos,
    })
    return coord, calls


def test_video_assets_become_poster_items_with_a_playback_url(monkeypatch):
    coord, calls = _coordinator(monkeypatch, [
        {"id": "img", "type": "IMAGE", "width": 400, "height": 300},
        {"id": "vid", "type": "VIDEO", "width": 1080, "height": 1920,
         "duration": 7500},
    ])
    items = asyncio.run(coord._update_immich())["items"]

    assert calls[0][-1] is True
    image, clip = items
    assert not image.is_video
    assert image.url.endswith("/assets/img/thumbnail?size=fullsize")
    assert clip.is_video
    # ``fullsize`` thumbnails exist only for images; posters use the preview.
    assert clip.url.endswith("/assets/vid/thumbnail?size=preview")
    assert clip.video_url == "http://immich.test/api/assets/vid/video/playback"
    assert clip.duration_ms == 7500


def test_videos_stay_off_by_default(monkeypatch):
    coord, calls = _coordinator(monkeypatch, [{"id": "img", "type": "IMAGE"}], include_videos=False)
    asyncio.run(coord._update_immich())
    assert calls[0][-1] is False


def test_video_fields_round_trip_through_the_items_cache():
    saved = {}

    class Store:
        async def async_save(self, payload):
            saved.update(payload)

        async def async_load(self):
            return saved

    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord._items_cache_store = Store()
    coord.provider = "immich"
    clip = MediaItem(
        url="poster", width=None, height=None, mime_type=None, filename="clip.mov",
        source_id="vid", video_url="http://immich.test/api/assets/vid/video/playback",
        duration_ms=7500,
    )
    asyncio.run(coord._save_cached_items({"title": "Trip", "items": [clip]}))
    (loaded,) = asyncio.run(coord._load_cached_items())["items"]
    assert loaded.video_url == clip.video_url
    assert loaded.duration_ms == 7500


def test_toggling_videos_is_a_source_change():
    base = {CONF_IMMICH_URL: "http://immich.test"}
    assert not cf._immich_source_changed(base, {**base, CONF_IMMICH_INCLUDE_VIDEOS: False})
    assert cf._immich_source_changed(base, {**base, CONF_IMMICH_INCLUDE_VIDEOS: True})


# ── Slide timing ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("duration_ms", "max_seconds", "fallback", "expected"),
    [
        (12_000, 60, 30, 13.0),   # plays once, plus start-up grace
        (1_000, 60, 30, 5.0),     # tiny clips are held (and looped)
        (300_000, 60, 30, 60.0),  # long clips are cut at the cap
        (None, 60, 30, 30.0),     # unknown length -> slide interval
        (None, 20, 30, 20.0),     # ... still within the cap
    ],
)
def test_video_hold_seconds(duration_ms, max_seconds, fallback, expected):
    assert video.video_hold_seconds(duration_ms, max_seconds, fallback) == expected


def _jpeg(size):
    buffer = io.BytesIO()
    Image.new("RGB", size, "gray").save(buffer, "JPEG")
    return buffer.getvalue()


class _Hass:
    def __init__(self):
        self.data = {DOMAIN: {}}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


def _render_cam(monkeypatch, items):
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = _Hass()
    cam.entry = types.SimpleNamespace(title="Trip", entry_id="entry")
    cam.coordinator = object()
    cam.store = SlideshowStore(portrait_mode=ORIENTATION_MISMATCH_PAIR, aspect_ratio="16:9")
    cam._download_cache = camera._DownloadCache(1024 * 1024)
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cam._rng = random.Random(1)
    images = {item.url: _jpeg((item.width, item.height)) for item in items}

    async def fetch(_self, url):
        return images[url]

    # Rendering happens on private renderer copies, so patch the class.
    monkeypatch.setattr(camera.AlbumSlideshowCamera, "_fetch_bytes", fetch)
    return cam


def _media(url, *, portrait, clip=False):
    width, height = (90, 160) if portrait else (160, 90)
    return MediaItem(
        url=url, width=width, height=height, mime_type=None, filename=url,
        source_id=url, video_url=f"http://immich.test/{url}" if clip else None,
        duration_ms=7000 if clip else None,
    )


def test_portrait_video_is_shown_alone_instead_of_paired(monkeypatch):
    items = [_media("clip", portrait=True, clip=True), _media("photo", portrait=True)]
    cam = _render_cam(monkeypatch, items)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["photo_ids"] == [items[0].photo_id]
    assert frame.meta.get("pair_frames") is None
    assert frame.meta["video"] == {"photo_id": items[0].photo_id, "duration_ms": 7000}


def test_a_video_is_never_chosen_as_a_pairing_partner(monkeypatch):
    items = [
        _media("photo", portrait=True),
        _media("clip", portrait=True, clip=True),
        _media("partner", portrait=True),
    ]
    cam = _render_cam(monkeypatch, items)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["photo_ids"] == [items[0].photo_id, items[2].photo_id]
    assert "video" not in frame.meta


def test_applying_a_video_frame_publishes_a_signed_url_and_hold(monkeypatch):
    signed = []
    monkeypatch.setattr(video, "sign_video_path", lambda hass, path: signed.append(path) or f"{path}?authSig=x")
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = object()
    cam.entry = types.SimpleNamespace(entry_id="entry", title="Trip")
    cam.store = SlideshowStore(slide_interval=30, video_max_seconds=60)
    cam._previous_frames = deque()
    cam._next_frames = deque()
    cam._current_frame = None
    cam._slide_deadline = None
    cam._interrupt_event = asyncio.Event()
    cam._frame_id = 0
    cam._frame_serials = {}
    cam._rng = random.Random(1)
    cam.async_write_ha_state = lambda: None
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cursor = cam._capture_cursor()

    clip = camera._RenderedFrame(b"jpeg", cursor, {"video": {"photo_id": "v1:abc", "duration_ms": 12_000}})
    cam._apply_frame(clip)
    assert cam._video_url == "/api/album_slideshow/video/entry/v1:abc?authSig=x"
    assert cam._slide_hold_seconds() == 13.0
    # Re-rendering the same clip keeps its URL so the card doesn't restart it.
    cam._apply_frame(clip)
    assert len(signed) == 1

    cam._apply_frame(camera._RenderedFrame(b"jpeg", cursor, {}))
    assert cam._video_url is None
    assert cam._slide_hold_seconds() == 30.0
    assert signed == ["/api/album_slideshow/video/entry/v1:abc"]


# ── Proxy view ─────────────────────────────────────────────────────────────


_CLIP = bytes(range(256)) * 40


def _run_proxy(tmp_path, monkeypatch, scenario, *, upstream_type="video/mp4"):
    """Serve the view against a real upstream that requires the API key."""
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(_CLIP)
    upstream_hits = []

    async def playback(request):
        upstream_hits.append(dict(request.headers))
        if request.headers.get("x-api-key") != "secret":
            return web.Response(status=401)
        if upstream_type != "video/mp4":
            return web.Response(text="<html>login</html>", content_type=upstream_type)
        return web.FileResponse(clip_path)

    async def main():
        upstream_app = web.Application()
        upstream_app.router.add_get("/api/assets/{id}/video/playback", playback)
        async with TestServer(upstream_app) as upstream:
            item = MediaItem(
                url="poster", width=None, height=None, mime_type=None, filename=None,
                source_id="vid", video_url=str(upstream.make_url("/api/assets/vid/video/playback")),
            )
            coord = types.SimpleNamespace(
                data={"items": [item]}, image_request_headers={"x-api-key": "secret"},
            )
            hass = types.SimpleNamespace(data={DOMAIN: {"entry": {"coordinator": coord}}})
            view = video.AlbumSlideshowVideoView(hass)
            async with ClientSession() as session:
                monkeypatch.setattr(video, "async_get_clientsession", lambda *_a, **_kw: session)

                async def handler(request):
                    return await view.get(request, **request.match_info)

                app = web.Application()
                app.router.add_get("/video/{entry_id}/{photo_id}", handler)
                async with TestClient(TestServer(app)) as client:
                    return await scenario(client, item.photo_id), upstream_hits

    return asyncio.run(main())


def test_proxy_streams_the_clip_with_the_key_added_server_side(tmp_path, monkeypatch):
    async def scenario(client, photo_id):
        resp = await client.get(f"/video/entry/{photo_id}")
        return resp.status, resp.headers.get("Content-Type"), await resp.read()

    (status, content_type, body), hits = _run_proxy(tmp_path, monkeypatch, scenario)
    assert status == 200
    assert content_type == "video/mp4"
    assert body == _CLIP
    assert hits[0]["x-api-key"] == "secret"


def test_proxy_forwards_range_requests(tmp_path, monkeypatch):
    async def scenario(client, photo_id):
        resp = await client.get(f"/video/entry/{photo_id}", headers={"Range": "bytes=10-19"})
        return resp.status, resp.headers.get("Content-Range"), await resp.read()

    (status, content_range, body), _ = _run_proxy(tmp_path, monkeypatch, scenario)
    assert status == 206
    assert content_range == f"bytes 10-19/{len(_CLIP)}"
    assert body == _CLIP[10:20]


def test_proxy_passes_through_unsatisfiable_ranges(tmp_path, monkeypatch):
    async def scenario(client, photo_id):
        resp = await client.get(f"/video/entry/{photo_id}", headers={"Range": "bytes=999999-"})
        return resp.status

    status, _ = _run_proxy(tmp_path, monkeypatch, scenario)
    assert status == 416


def test_proxy_only_serves_videos_in_the_entry_playlist(tmp_path, monkeypatch):
    async def scenario(client, photo_id):
        unknown = await client.get("/video/entry/v1:not-in-playlist")
        other_entry = await client.get(f"/video/other/{photo_id}")
        return unknown.status, other_entry.status

    statuses, hits = _run_proxy(tmp_path, monkeypatch, scenario)
    assert statuses == (404, 404)
    assert hits == []


def test_proxy_rejects_non_video_upstream_responses(tmp_path, monkeypatch):
    async def scenario(client, photo_id):
        return (await client.get(f"/video/entry/{photo_id}")).status

    status, _ = _run_proxy(tmp_path, monkeypatch, scenario, upstream_type="text/html")
    assert status == 502


# ── Captions: file name, path, and which video stream is served ────────────


@pytest.mark.parametrize(
    ("status", "headers", "expected"),
    [
        (206, {"Content-Range": "bytes 0-0/52428800"}, 52428800),
        (200, {"Content-Length": "1234"}, 1234),
        (206, {"Content-Range": "bytes 0-0/*"}, None),
        (206, {}, None),
        (404, {"Content-Length": "10"}, None),
    ],
)
def test_stream_total_bytes(status, headers, expected):
    assert immich.stream_total_bytes(status, headers) == expected


def test_describe_video_compares_the_stream_with_the_original():
    transcoded = immich.describe_video("video/mp4; charset=x", 3_000_000, 90_000_000, 8_000)
    assert transcoded == {
        "video_version": "transcoded",
        "video_bitrate_mbps": 3.0,
        "video_size_bytes": 3_000_000,
        "video_content_type": "video/mp4",
    }
    assert immich.describe_video("video/quicktime", 5, 5, None)["video_version"] == "original"
    unknown = immich.describe_video(None, None, 5, 1000)
    assert unknown["video_version"] is None and unknown["video_bitrate_mbps"] is None


def test_asset_detail_supplies_the_original_file_size():
    assert immich.parse_asset_exif({"exifInfo": {"fileSizeInByte": 4096}})["byte_size"] == 4096
    assert "byte_size" not in immich.parse_asset_exif({"exifInfo": {"fileSizeInByte": 0}})


def test_immich_items_carry_their_server_path(monkeypatch):
    coord, _ = _coordinator(monkeypatch, [
        {"id": "img", "type": "IMAGE", "originalPath": "/library/2024/IMG_1.jpg"},
    ])
    (item,) = asyncio.run(coord._update_immich())["items"]
    assert item.source_path == "/library/2024/IMG_1.jpg"


def _video_info_coordinator(monkeypatch, *, probe=("video/mp4", 2_000_000), asset_size=80_000_000):
    calls = []

    class FakeClient:
        def __init__(self, _hass, _url, _key):
            pass

        async def async_probe_video(self, asset_id):
            calls.append(("probe", asset_id))
            return probe

        async def async_get_asset(self, asset_id):
            calls.append(("asset", asset_id))
            return {"exifInfo": {"fileSizeInByte": asset_size}}

    monkeypatch.setattr(immich, "ImmichClient", FakeClient)
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.hass = object()
    coord.provider = "immich"
    coord.entry = types.SimpleNamespace(data={CONF_IMMICH_URL: "http://immich.test", CONF_IMMICH_API_KEY: "k"})
    return coord, calls


def test_video_info_is_probed_once_and_cached(monkeypatch):
    coord, calls = _video_info_coordinator(monkeypatch)
    clip = _media("clip", portrait=False, clip=True)
    clip.duration_ms = 8_000

    info = asyncio.run(coord.async_video_info(clip))
    assert info["video_version"] == "transcoded"
    assert info["video_bitrate_mbps"] == 2.0
    # The original size came from the asset detail and is kept on the item.
    assert clip.byte_size == 80_000_000
    asyncio.run(coord.async_video_info(clip))
    assert calls == [("probe", "clip"), ("asset", "clip")]


def test_video_info_reuses_an_enriched_original_size(monkeypatch):
    coord, calls = _video_info_coordinator(monkeypatch, probe=("video/quicktime", 9_000))
    clip = _media("clip", portrait=False, clip=True)
    clip.byte_size = 9_000
    assert asyncio.run(coord.async_video_info(clip))["video_version"] == "original"
    assert calls == [("probe", "clip")]


def test_video_info_is_empty_for_other_providers(monkeypatch):
    coord, calls = _video_info_coordinator(monkeypatch)
    coord.provider = "local_folder"
    assert asyncio.run(coord.async_video_info(_media("clip", portrait=False, clip=True))) == {}
    assert calls == []


def test_probe_reads_only_the_stream_headers(tmp_path, monkeypatch):
    clip_path = tmp_path / "clip.mp4"
    clip_path.write_bytes(_CLIP)
    seen = []

    async def playback(request):
        seen.append(dict(request.headers))
        return web.FileResponse(clip_path)

    async def main():
        app = web.Application()
        app.router.add_get("/api/assets/{id}/video/playback", playback)
        async with TestServer(app) as server, ClientSession() as session:
            monkeypatch.setattr(immich, "async_get_clientsession", lambda _hass: session)
            client = immich.ImmichClient(object(), str(server.make_url("")), "secret")
            return await client.async_probe_video("vid")

    content_type, total = asyncio.run(main())
    assert content_type == "video/mp4"
    assert total == len(_CLIP)
    assert seen[0]["Range"] == "bytes=0-0"
    assert seen[0]["x-api-key"] == "secret"


def test_video_slide_publishes_its_stream_version(monkeypatch):
    items = [_media("clip", portrait=False, clip=True)]
    cam = _render_cam(monkeypatch, items)

    async def video_info(item):
        return {"video_version": "original", "video_bitrate_mbps": 21.5, "unrelated": 1}

    cam.coordinator = types.SimpleNamespace(async_video_info=video_info)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["video"]["video_version"] == "original"
    assert frame.meta["video"]["video_bitrate_mbps"] == 21.5
    assert "unrelated" not in frame.meta["video"]


def test_a_failed_version_probe_still_shows_the_video(monkeypatch):
    items = [_media("clip", portrait=False, clip=True)]
    cam = _render_cam(monkeypatch, items)

    async def video_info(item):
        raise RuntimeError("immich down")

    cam.coordinator = types.SimpleNamespace(async_video_info=video_info)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["video"]["photo_id"] == items[0].photo_id
    assert "video_version" not in frame.meta["video"]


def test_paired_captions_name_each_file(monkeypatch):
    items = [_media("left", portrait=True), _media("right", portrait=True)]
    items[0].source_path = "/library/left.jpg"
    cam = _render_cam(monkeypatch, items)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    left, right = frame.meta["pair_frames"]
    assert (left["filename"], left["path"]) == ("left", "/library/left.jpg")
    assert (right["filename"], right["path"]) == ("right", None)


def test_a_slide_swapped_in_by_a_rebuild_gets_its_own_hold(monkeypatch):
    """A re-render or exclusion rebuild can replace a photo with a video
    outside the timer; the video must not inherit the photo's countdown."""
    monkeypatch.setattr(video, "sign_video_path", lambda hass, path: path)
    cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
    cam.hass = object()
    cam.entry = types.SimpleNamespace(entry_id="entry", title="Trip")
    cam.store = SlideshowStore(slide_interval=10, video_max_seconds=60)
    cam._previous_frames = deque()
    cam._next_frames = deque()
    cam._current_frame = None
    cam._interrupt_event = asyncio.Event()
    cam._frame_id = 0
    cam._frame_serials = {}
    cam._rng = random.Random(1)
    cam.async_write_ha_state = lambda: None
    cam._index = 0
    cam._random_order = []
    cam._random_pos = 0
    cam._recent_urls = []
    cursor = cam._capture_cursor()
    photo = camera._RenderedFrame(b"jpeg", cursor, {"photo_ids": ["p"]})
    clip = camera._RenderedFrame(
        b"jpeg", cursor, {"photo_ids": ["v"], "video": {"photo_id": "v", "duration_ms": 40_000}},
    )

    cam._apply_frame(photo)
    cam._slide_deadline = 1234.0  # the photo's running countdown
    cam._interrupt_event.clear()

    # Re-rendering the same photo keeps its countdown.
    cam._apply_frame(camera._RenderedFrame(b"jpeg2", cursor, {"photo_ids": ["p"]}))
    assert cam._slide_deadline == 1234.0
    assert not cam._interrupt_event.is_set()

    # A different slide restarts the clock; the loop then uses the clip's hold.
    cam._apply_frame(clip)
    assert cam._slide_deadline is None
    assert cam._interrupt_event.is_set()
    assert cam._slide_hold_seconds() == 41.0


# ── Live Photos and the video count ────────────────────────────────────────


def test_live_photo_motion_clips_are_never_listed_on_their_own():
    payload = [
        {"id": "still", "type": "IMAGE", "livePhotoVideoId": "motion"},
        {"id": "motion", "type": "VIDEO", "visibility": "hidden"},
        {"id": "legacy", "type": "VIDEO", "isVisible": False},
        {"id": "clip", "type": "VIDEO", "visibility": "timeline"},
    ]
    assert [it["id"] for it in immich.parse_random(payload, True)] == ["still", "clip"]
    assert immich.live_photo_video_id(payload[0]) == "motion"
    assert immich.live_photo_video_id({"id": "x", "type": "VIDEO", "livePhotoVideoId": "y"}) is None
    assert immich.live_photo_video_id({"id": "x", "type": "IMAGE"}) is None


def test_live_photos_keep_their_still_and_gain_a_motion_clip(monkeypatch, caplog):
    assets = [
        {"id": "still", "type": "IMAGE", "livePhotoVideoId": "motion"},
        {"id": "vid", "type": "VIDEO", "duration": 4000},
    ]
    coord, _ = _coordinator(monkeypatch, assets)
    coord.entry.data[coordinator_module.CONF_IMMICH_LIVE_PHOTOS] = True
    with caplog.at_level("DEBUG", logger=coordinator_module.__name__):
        live, clip = asyncio.run(coord._update_immich())["items"]

    assert live.live_photo and not live.is_video
    assert live.url.endswith("/assets/still/thumbnail?size=fullsize")
    assert live.video_url == "http://immich.test/api/assets/motion/video/playback"
    assert clip.is_video and not clip.live_photo
    assert "Immich Trip: 2 assets (1 videos, 1 Live Photos)" in caplog.text


def test_live_photo_motion_is_off_by_default(monkeypatch):
    coord, _ = _coordinator(monkeypatch, [{"id": "still", "type": "IMAGE", "livePhotoVideoId": "motion"}])
    (still,) = asyncio.run(coord._update_immich())["items"]
    assert still.video_url is None and not still.live_photo


def test_toggling_live_photos_is_a_source_change():
    base = {CONF_IMMICH_URL: "http://immich.test"}
    assert cf._immich_source_changed(base, {**base, coordinator_module.CONF_IMMICH_LIVE_PHOTOS: True})


def test_live_photo_flag_round_trips_through_the_items_cache():
    saved = {}

    class Store:
        async def async_save(self, payload):
            saved.update(payload)

        async def async_load(self):
            return saved

    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord._items_cache_store = Store()
    coord.provider = "immich"
    still = _live("still")
    asyncio.run(coord._save_cached_items({"title": "Trip", "items": [still]}))
    (loaded,) = asyncio.run(coord._load_cached_items())["items"]
    assert loaded.live_photo and loaded.video_url == still.video_url


def _live(url, *, portrait=False):
    item = _media(url, portrait=portrait)
    item.video_url = f"http://immich.test/{url}-motion"
    item.live_photo = True
    return item


def test_a_live_photo_alone_plays_its_motion_at_the_photo_interval(monkeypatch):
    items = [_live("still")]
    cam = _render_cam(monkeypatch, items)
    called = []

    async def video_info(item):
        called.append(item)
        return {"video_version": "original"}

    cam.coordinator = types.SimpleNamespace(async_video_info=video_info)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["video"] == {"photo_id": items[0].photo_id, "live": True}
    assert called == []  # no stream probe for a motion clip
    cam._last_video = frame.meta["video"]
    cam.store.slide_interval = 10
    assert cam._slide_hold_seconds() == 10.0
    assert cam._video_info_attributes()["video_duration"] is None


def test_live_photos_can_still_be_paired(monkeypatch):
    items = [_media("photo", portrait=True), _live("partner", portrait=True)]
    cam = _render_cam(monkeypatch, items)
    frame = asyncio.run(cam._render_available_frame(cam._capture_cursor(), items, advance=False))
    assert frame.meta["photo_ids"] == [items[0].photo_id, items[1].photo_id]
    # A paired Live Photo shows only its still.
    assert "video" not in frame.meta


def test_video_info_skips_live_photos(monkeypatch):
    coord, calls = _video_info_coordinator(monkeypatch)
    assert asyncio.run(coord.async_video_info(_live("still"))) == {}
    assert calls == []
