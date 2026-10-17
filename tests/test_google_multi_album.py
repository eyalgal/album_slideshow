"""Google Photos entries that combine several shared albums."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.album_slideshow import config_flow as cf
from custom_components.album_slideshow import coordinator as coordinator_module
from custom_components.album_slideshow import google_scraper
from custom_components.album_slideshow.const import (
    CONF_ALBUM_NAME,
    CONF_ALBUM_URL,
    CONF_ALBUM_URLS,
    CONF_PROVIDER,
    PROVIDER_GOOGLE_SHARED,
)
from custom_components.album_slideshow.coordinator import (
    MediaItem,
    _merge_album_items,
)
from custom_components.album_slideshow.google_albums import (
    album_url_data,
    album_urls_from_data,
    parse_album_urls,
)
from homeassistant.helpers.update_coordinator import UpdateFailed

A = "https://photos.app.goo.gl/AAAA"
B = "https://photos.app.goo.gl/BBBB"
C = "https://photos.app.goo.gl/CCCC"


def _photo(source_id, album=None):
    return MediaItem(
        url=f"https://lh3.googleusercontent.com/{source_id}=w1920-h1080",
        width=1920, height=1080, mime_type=None, filename=None,
        source_id=source_id, source_album_url=album,
    )


# -- link parsing / storage -------------------------------------------------

def test_parse_album_urls_splits_lines_commas_and_drops_repeats():
    text = f"{A}\n\n  {B} ,{A}/\n{C};"
    assert parse_album_urls(text) == [A, B, C]


def test_parse_album_urls_handles_empty_input():
    assert parse_album_urls("") == []
    assert parse_album_urls(None) == []
    assert parse_album_urls("  \n ") == []


def test_existing_single_link_entries_keep_working():
    assert album_urls_from_data({CONF_ALBUM_URL: A}) == [A]
    assert album_urls_from_data({}) == []


def test_multi_link_entries_read_the_list():
    data = {CONF_ALBUM_URL: A, CONF_ALBUM_URLS: [A, B, C]}
    assert album_urls_from_data(data) == [A, B, C]


def test_single_link_is_stored_exactly_as_before():
    assert album_url_data([A]) == {CONF_ALBUM_URL: A}
    assert album_url_data([A, B]) == {CONF_ALBUM_URL: A, CONF_ALBUM_URLS: [A, B]}


# -- merging ----------------------------------------------------------------

def test_merge_keeps_album_order_and_drops_photos_in_several_albums():
    first = [_photo("one", A), _photo("shared", A)]
    second = [_photo("shared", B), _photo("two", B)]
    merged = _merge_album_items([first, second])
    assert [p.source_id for p in merged] == ["one", "shared", "two"]
    # The first album containing a duplicate keeps it.
    assert merged[1].source_album_url == A


def test_merge_falls_back_to_the_url_when_there_is_no_media_key():
    a = MediaItem(url="https://lh3.googleusercontent.com/x=w100-h100", width=1, height=1,
                  mime_type=None, filename=None)
    b = MediaItem(url="https://lh3.googleusercontent.com/x=w1920-h1080", width=1, height=1,
                  mime_type=None, filename=None)
    assert len(_merge_album_items([[a], [b]])) == 1


def _shot(source_id, taken, width=4032, height=3024, byte_size=2_500_000):
    return MediaItem(
        url=f"https://lh3.googleusercontent.com/pw/{source_id}=w1920-h1080",
        width=width, height=height, mime_type=None, filename=None,
        captured_at=taken, byte_size=byte_size, source_id=source_id,
    )


def test_merge_matches_a_photo_in_two_albums_by_time_taken_and_size():
    # Each shared album gives the same photo its own media key and URL.
    first = [_shot("a-1", 1_700_000_000_000), _shot("a-2", 1_700_000_100_000)]
    second = [
        _shot("b-1", 1_700_000_000_000),
        _shot("b-2", 1_700_000_000_000, width=3024, height=4032),
        _shot("b-3", 1_700_000_000_000, byte_size=2_400_000),
    ]
    merged = _merge_album_items([first, second])
    assert [p.source_id for p in merged] == ["a-1", "a-2", "b-2", "b-3"]


def test_merge_keeps_burst_shots_within_one_album():
    album = [_shot("burst-1", 1_700_000_000_000), _shot("burst-2", 1_700_000_000_000)]
    assert len(_merge_album_items([album, [_shot("other", 1_700_000_500_000)]])) == 3


# -- coordinator fetch ------------------------------------------------------

def _coordinator(urls, *, data=None, cached=None):
    coord = coordinator_module.AlbumCoordinator.__new__(coordinator_module.AlbumCoordinator)
    coord.provider = PROVIDER_GOOGLE_SHARED
    coord.album_urls = list(urls)
    coord.album_url = urls[0] if urls else None
    coord.entry = SimpleNamespace(entry_id="e1", title="Photo Frame", options={})
    coord.hass = SimpleNamespace()
    coord.data = data
    coord._load_cached_items = AsyncMock(return_value=cached)
    return coord


def _albums(**by_url):
    """Fake ``_fetch_google_album``: url -> list of source ids, or an exception."""
    calls = []

    async def fetch(_session, url):
        calls.append(url)
        result = by_url[url]
        if isinstance(result, Exception):
            raise result
        return {"title": f"title of {url}", "items": [_photo(sid) for sid in result]}

    return fetch, calls


def test_single_album_behaves_as_before():
    coord = _coordinator([A])
    coord._fetch_google_album, calls = _albums(**{A: ["one", "two"]})

    data = asyncio.run(coord._update_google_shared())

    assert calls == [A]
    assert data["title"] == f"title of {A}"
    assert [p.source_id for p in data["items"]] == ["one", "two"]
    assert {p.source_album_url for p in data["items"]} == {A}


def test_several_albums_are_combined_and_tagged():
    coord = _coordinator([A, B, C])
    coord._fetch_google_album, calls = _albums(**{
        A: ["one", "shared"], B: ["shared", "two"], C: ["three"],
    })

    data = asyncio.run(coord._update_google_shared())

    assert calls == [A, B, C]
    assert data["title"] == "Photo Frame"
    by_id = {p.source_id: p.source_album_url for p in data["items"]}
    assert by_id == {"one": A, "shared": A, "two": B, "three": C}


def test_a_failing_album_keeps_its_photos_from_the_last_refresh():
    previous = {"items": [_photo("old-b", B), _photo("old-a", A)]}
    coord = _coordinator([A, B], data=previous)
    coord._fetch_google_album, _ = _albums(**{A: ["new-a"], B: UpdateFailed("timeout")})

    data = asyncio.run(coord._update_google_shared())

    assert [p.source_id for p in data["items"]] == ["new-a", "old-b"]
    coord._load_cached_items.assert_not_awaited()


def test_after_a_restart_a_failing_album_is_served_from_the_disk_cache():
    cached = {"items": [_photo("cached-b", B)]}
    coord = _coordinator([A, B], data=None, cached=cached)
    coord._fetch_google_album, _ = _albums(**{A: ["new-a"], B: UpdateFailed("timeout")})

    data = asyncio.run(coord._update_google_shared())

    assert [p.source_id for p in data["items"]] == ["new-a", "cached-b"]


def test_all_albums_failing_raises_so_the_cache_fallback_runs():
    coord = _coordinator([A, B])
    coord._fetch_google_album, _ = _albums(**{A: UpdateFailed("x"), B: UpdateFailed("y")})

    with pytest.raises(UpdateFailed):
        asyncio.run(coord._update_google_shared())


def test_missing_links_raise():
    coord = _coordinator([])
    with pytest.raises(UpdateFailed):
        asyncio.run(coord._update_google_shared())


# -- metadata enrichment ----------------------------------------------------

def test_metadata_keys_are_fetched_once_per_album(monkeypatch):
    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(coordinator_module.aiohttp, "ClientSession", lambda **kw: Session())
    monkeypatch.setattr(coordinator_module, "_GOOGLE_METADATA_INTERVAL", 0)
    keys = AsyncMock(side_effect=lambda _session, url: f"keys:{url}")
    monkeypatch.setattr(google_scraper, "fetch_metadata_keys", keys)
    used = []

    async def metadata(_session, album_keys, source_id):
        used.append((source_id, album_keys))
        return {}

    monkeypatch.setattr(google_scraper, "fetch_photo_metadata", metadata)
    coord = _coordinator([A, B])
    coord.entry.options = {"google_metadata": True}
    coord._enrich_progress = {"phase": "exif", "exif_done": 0}
    coord._items_cache_store = SimpleNamespace(async_save=AsyncMock())
    coord.async_set_updated_data = lambda data: None
    items = [_photo("a1", A), _photo("b1", B), _photo("a2", A), _photo("legacy")]

    asyncio.run(coord._enrich_google_items_background({"items": items}))

    assert [call.args[1] for call in keys.await_args_list] == [A, B]
    assert used == [
        ("a1", f"keys:{A}"), ("b1", f"keys:{B}"), ("a2", f"keys:{A}"),
        # Items cached before this change have no album: use the primary one.
        ("legacy", f"keys:{A}"),
    ]


def test_album_of_each_photo_survives_the_disk_cache():
    coord = _coordinator([A, B])
    store = SimpleNamespace(async_save=AsyncMock(), async_load=AsyncMock())
    coord._items_cache_store = store
    del coord._load_cached_items  # use the real implementation
    coord._apply_google_location_privacy = lambda items: None

    asyncio.run(coord._save_cached_items({"title": "t", "items": [_photo("b1", B)]}))
    store.async_load.return_value = store.async_save.call_args.args[0]
    loaded = asyncio.run(coord._load_cached_items())

    assert loaded["items"][0].source_album_url == B


# -- setup form -------------------------------------------------------------

def _config_flow(monkeypatch):
    flow = cf.ConfigFlow()
    flow.hass = object()
    monkeypatch.setattr(flow, "async_show_form", lambda **kw: {"type": "form", **kw}, raising=False)
    monkeypatch.setattr(flow, "async_create_entry", lambda **kw: {"type": "create_entry", **kw}, raising=False)
    monkeypatch.setattr(flow, "async_set_unique_id", AsyncMock(), raising=False)
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None, raising=False)
    return flow


def test_setup_with_one_link_stores_the_same_data_as_before(monkeypatch):
    flow = _config_flow(monkeypatch)
    result = asyncio.run(flow.async_step_google_shared({CONF_ALBUM_NAME: "Frame", CONF_ALBUM_URL: f" {A} "}))
    assert result["data"] == {
        CONF_PROVIDER: PROVIDER_GOOGLE_SHARED, CONF_ALBUM_URL: A, CONF_ALBUM_NAME: "Frame",
    }
    flow.async_set_unique_id.assert_awaited_once_with(f"album_slideshow:google_shared:{A}:Frame")


def test_setup_with_several_links(monkeypatch):
    flow = _config_flow(monkeypatch)
    result = asyncio.run(flow.async_step_google_shared({CONF_ALBUM_NAME: "Frame", CONF_ALBUM_URL: f"{A}\n{B}"}))
    assert result["data"][CONF_ALBUM_URL] == A
    assert result["data"][CONF_ALBUM_URLS] == [A, B]


@pytest.mark.parametrize("text", ["", "not a link", f"{A}\nhttps://example.com/album"])
def test_setup_rejects_anything_that_is_not_a_shared_album_link(monkeypatch, text):
    flow = _config_flow(monkeypatch)
    result = asyncio.run(flow.async_step_google_shared({CONF_ALBUM_NAME: "Frame", CONF_ALBUM_URL: text}))
    assert result["type"] == "form"
    assert result["errors"] == {CONF_ALBUM_URL: "invalid_album_url"}


# -- options: edit the albums of an existing entry --------------------------

class _Entries:
    def __init__(self):
        self.updates = []

    def async_update_entry(self, entry, **kwargs):
        self.updates.append(kwargs)


def _options_flow(monkeypatch, data):
    cleared = []

    async def clear(_hass, entry_id):
        cleared.append(entry_id)

    monkeypatch.setattr(cf, "_async_clear_item_cache", clear)
    flow = cf.GoogleOptionsFlow()
    flow.hass = SimpleNamespace(config_entries=_Entries())
    flow.config_entry = SimpleNamespace(entry_id="e1", data=data, options={"google_metadata": True})
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    return flow, cleared


def _base_data(**extra):
    return {CONF_PROVIDER: PROVIDER_GOOGLE_SHARED, CONF_ALBUM_NAME: "Frame", CONF_ALBUM_URL: A, **extra}


def test_album_editor_is_prefilled_one_link_per_line(monkeypatch):
    flow, _ = _options_flow(monkeypatch, _base_data(**{CONF_ALBUM_URLS: [A, B]}))
    form = asyncio.run(flow.async_step_google_albums())
    (marker,) = form["data_schema"].schema
    assert str(marker) == CONF_ALBUM_URLS
    assert marker.default() == f"{A}\n{B}"


def test_adding_albums_updates_the_entry_and_clears_the_cache(monkeypatch):
    flow, cleared = _options_flow(monkeypatch, _base_data())
    result = asyncio.run(flow.async_step_google_albums({CONF_ALBUM_URLS: f"{A}\n{B}\n{C}"}))

    assert result["type"] == "create_entry"
    assert result["data"] == {"google_metadata": True}  # options untouched
    (update,) = flow.hass.config_entries.updates
    assert update["data"] == _base_data(**{CONF_ALBUM_URLS: [A, B, C]})
    assert cleared == ["e1"]


def test_going_back_to_one_album_drops_the_list(monkeypatch):
    flow, _ = _options_flow(monkeypatch, _base_data(**{CONF_ALBUM_URLS: [A, B]}))
    asyncio.run(flow.async_step_google_albums({CONF_ALBUM_URLS: B}))
    (update,) = flow.hass.config_entries.updates
    assert update["data"] == {**_base_data(), CONF_ALBUM_URL: B}


def test_saving_unchanged_links_does_not_touch_the_entry(monkeypatch):
    flow, cleared = _options_flow(monkeypatch, _base_data(**{CONF_ALBUM_URLS: [A, B]}))
    result = asyncio.run(flow.async_step_google_albums({CONF_ALBUM_URLS: f"{A}\n{B}"}))
    assert result["type"] == "create_entry"
    assert flow.hass.config_entries.updates == []
    assert cleared == []


def test_album_editor_rejects_bad_links(monkeypatch):
    flow, cleared = _options_flow(monkeypatch, _base_data())
    result = asyncio.run(flow.async_step_google_albums({CONF_ALBUM_URLS: f"{A}\nnope"}))
    assert result["errors"] == {CONF_ALBUM_URLS: "invalid_album_url"}
    assert flow.hass.config_entries.updates == []
    assert cleared == []


def test_new_strings_match_in_both_translation_files():
    import json
    from pathlib import Path

    root = Path(cf.__file__).parent
    strings = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())
    for path in (("options", "step", "google_menu"), ("options", "step", "google_albums"),
                 ("config", "step", "google_shared")):
        a, b = strings, english
        for key in path:
            a, b = a[key], b[key]
        assert a == b
    assert set(strings["options"]["step"]["google_menu"]["menu_options"]) == {
        "google_albums", "google_metadata",
    }
    assert "invalid_album_url" in strings["options"]["error"]
    assert "google_cache_clear_failed" in strings["options"]["error"]
    assert strings["options"]["error"] == english["options"]["error"]


# -- review follow-ups ------------------------------------------------------

def test_albums_are_not_changed_when_the_old_cache_cannot_be_cleared(monkeypatch):
    flow, _ = _options_flow(monkeypatch, _base_data())

    async def fail(_hass, _entry_id):
        raise OSError("disk full")

    monkeypatch.setattr(cf, "_async_clear_item_cache", fail)
    result = asyncio.run(flow.async_step_google_albums({CONF_ALBUM_URLS: f"{A}\n{B}"}))

    assert result["type"] == "form"
    assert result["errors"] == {"base": "google_cache_clear_failed"}
    assert flow.hass.config_entries.updates == []


def test_failing_album_missing_from_memory_is_looked_up_on_disk():
    # Memory has album A's photos but none from B (e.g. B was empty last time
    # or was just added); B's previous photos are only in the disk cache.
    memory = {"items": [_photo("old-a", A)]}
    cached = {"items": [_photo("cached-b", B)]}
    coord = _coordinator([A, B], data=memory, cached=cached)
    coord._fetch_google_album, _ = _albums(**{A: ["new-a"], B: UpdateFailed("timeout")})

    data = asyncio.run(coord._update_google_shared())

    assert [p.source_id for p in data["items"]] == ["new-a", "cached-b"]


def test_photos_cached_before_albums_were_tracked_count_as_the_first_album():
    # Upgraded single-album entry, second album added: the first album fails
    # on the very first refresh, and its saved photos have no album tag yet.
    cached = {"items": [_photo("legacy-1"), _photo("legacy-2")]}
    coord = _coordinator([A, B], data=None, cached=cached)
    coord._fetch_google_album, _ = _albums(**{A: UpdateFailed("timeout"), B: ["new-b"]})

    data = asyncio.run(coord._update_google_shared())

    assert [p.source_id for p in data["items"]] == ["legacy-1", "legacy-2", "new-b"]


def test_untagged_photos_are_not_attributed_to_other_albums():
    cached = {"items": [_photo("legacy")]}
    coord = _coordinator([A, B], data=None, cached=cached)
    coord._fetch_google_album, _ = _albums(**{A: ["new-a"], B: UpdateFailed("timeout")})

    data = asyncio.run(coord._update_google_shared())

    assert [p.source_id for p in data["items"]] == ["new-a"]
