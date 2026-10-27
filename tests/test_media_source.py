from __future__ import annotations

import asyncio
from types import SimpleNamespace

from custom_components.album_slideshow import coordinator as c


# ── _media_node_is_image ───────────────────────────────────────────────────

def test_is_image_by_media_class():
    assert c._media_node_is_image("image", None) is True


def test_is_image_by_mime():
    assert c._media_node_is_image(None, "image/jpeg") is True


def test_video_class_is_rejected():
    assert c._media_node_is_image("video", None) is False


def test_video_mime_is_rejected_even_if_class_missing():
    assert c._media_node_is_image(None, "video/mp4") is False


def test_directory_is_not_an_image():
    assert c._media_node_is_image("directory", None) is False


def test_unknown_is_not_an_image():
    assert c._media_node_is_image(None, None) is False


# ── _is_junk_media_title ───────────────────────────────────────────────────

def test_synology_eadir_is_junk():
    assert c._is_junk_media_title("@eaDir") is True


def test_dotfiles_are_junk():
    assert c._is_junk_media_title(".DS_Store") is True
    assert c._is_junk_media_title(".hidden") is True


def test_non_web_formats_are_junk():
    for name in ("photo.psd", "scan.TIF", "IMG.heic", "raw.CR2"):
        assert c._is_junk_media_title(name) is True, name


def test_normal_images_are_not_junk():
    for name in ("BACK.bmp", "wedding.jpg", "a.PNG", "clip.webp", "Grandma"):
        assert c._is_junk_media_title(name) is False, name


def test_junk_check_ignores_non_strings():
    assert c._is_junk_media_title(None) is False
    assert c._is_junk_media_title("") is False


# ── _normalize_resolved_url ────────────────────────────────────────────────

def test_absolute_url_passes_through():
    u = "https://immich.example.com/api/asset/1/thumbnail"
    assert c._normalize_resolved_url(u, "http://homeassistant.local:8123") == u


def test_relative_url_is_prefixed_with_base():
    out = c._normalize_resolved_url(
        "/media/local/a.jpg?authSig=abc", "http://homeassistant.local:8123"
    )
    assert out == "http://homeassistant.local:8123/media/local/a.jpg?authSig=abc"


def test_relative_url_without_base_is_unchanged():
    assert c._normalize_resolved_url("/media/local/a.jpg", "") == "/media/local/a.jpg"


def test_empty_url_is_returned_as_is():
    assert c._normalize_resolved_url("", "http://x") == ""


# ── _browse_media_source (recursive tree walk) ─────────────────────────────

def _node(cid, *, media_class=None, mime=None, can_expand=False, title=None):
    return SimpleNamespace(
        media_content_id=cid,
        media_class=media_class,
        media_content_type=mime,
        can_expand=can_expand,
        title=title,
    )


class _FakeMediaSource:
    """Minimal media_source stand-in: a map of content_id -> children list."""

    def __init__(self, tree: dict[str, list]):
        self.tree = tree

    async def async_browse_media(self, hass, content_id):
        return SimpleNamespace(children=self.tree.get(content_id, []))

    async def async_resolve_media(self, hass, content_id, target=None):
        return SimpleNamespace(url=f"/media/{content_id}.jpg", mime_type="image/jpeg")


def _stub_coord(media_source=None):
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord.hass = object()
    return coord


def test_browse_collects_image_leaves_and_recurses():
    tree = {
        "root": [
            _node("img1", media_class="image", title="One"),
            _node("folderA", media_class="directory", can_expand=True),
            _node("vid1", media_class="video"),
        ],
        "folderA": [
            _node("img2", mime="image/png", title="Two"),
            _node("img3", media_class="image", title="Three"),
        ],
    }
    fake = _FakeMediaSource(tree)
    coord = _stub_coord()
    collected: list = []
    asyncio.run(coord._browse_media_source(fake, "root", collected, 0))
    ids = [cid for cid, _ in collected]
    titles = [t for _, t in collected]
    assert ids == ["img1", "img2", "img3"]
    assert titles == ["One", "Two", "Three"]


def test_browse_skips_junk_dirs_and_non_web_files():
    tree = {
        "root": [
            _node("good", media_class="image", title="good.jpg"),
            _node("psd", media_class="image", title="layers.psd"),
            _node("eadir", media_class="directory", can_expand=True, title="@eaDir"),
        ],
        # If @eaDir were entered, this image would wrongly be collected.
        "eadir": [_node("thumb", media_class="image", title="thumb.jpg")],
    }
    fake = _FakeMediaSource(tree)
    coord = _stub_coord()
    collected: list = []
    asyncio.run(coord._browse_media_source(fake, "root", collected, 0))
    assert [cid for cid, _ in collected] == ["good"]


def test_browse_respects_item_cap():
    children = [_node(f"img{i}", media_class="image") for i in range(20)]
    fake = _FakeMediaSource({"root": children})
    coord = _stub_coord()
    collected: list = []
    orig = c._MEDIA_SOURCE_MAX_ITEMS
    try:
        c._MEDIA_SOURCE_MAX_ITEMS = 5
        asyncio.run(coord._browse_media_source(fake, "root", collected, 0))
    finally:
        c._MEDIA_SOURCE_MAX_ITEMS = orig
    assert len(collected) == 5


def test_browse_respects_depth_cap():
    # A chain of nested expandable folders deeper than the depth cap.
    tree = {}
    for i in range(20):
        tree[f"f{i}"] = [_node(f"f{i + 1}", media_class="directory", can_expand=True)]
    tree["f20"] = [_node("deep_img", media_class="image")]
    fake = _FakeMediaSource(tree)
    coord = _stub_coord()
    collected: list = []
    asyncio.run(coord._browse_media_source(fake, "f0", collected, 0))
    # Depth cap stops recursion before reaching the deep image.
    assert collected == []


# ── _resolve_media ─────────────────────────────────────────────────────────

def test_resolve_media_returns_url_and_mime():
    fake = _FakeMediaSource({})
    coord = _stub_coord()
    out = asyncio.run(coord._resolve_media(fake, "abc"))
    assert out == ("/media/abc.jpg", "image/jpeg")


def test_resolve_media_falls_back_to_two_arg_signature():
    class _OldMediaSource:
        async def async_resolve_media(self, hass, content_id):  # no target arg
            return SimpleNamespace(url="/media/old.jpg", mime_type="image/jpeg")

    coord = _stub_coord()
    out = asyncio.run(coord._resolve_media(_OldMediaSource(), "x"))
    assert out == ("/media/old.jpg", "image/jpeg")


def test_resolve_media_returns_none_on_error():
    class _BrokenMediaSource:
        async def async_resolve_media(self, hass, content_id, target=None):
            raise RuntimeError("nope")

    coord = _stub_coord()
    assert asyncio.run(coord._resolve_media(_BrokenMediaSource(), "x")) is None


# ── _sign_media_path ───────────────────────────────────────────────────────

def _stub_coord_with_store(refresh_hours=24):
    coord = c.AlbumCoordinator.__new__(c.AlbumCoordinator)
    coord.hass = object()
    coord.store = SimpleNamespace(refresh_hours=refresh_hours)
    return coord


def test_sign_media_path_signs_with_the_content_user(monkeypatch):
    import sys
    import types

    calls = {}

    def async_sign_path(hass, path, expiration, **kw):
        calls["path"] = path
        calls["expiration"] = expiration
        calls["kw"] = kw
        return f"{path}?authSig=SIG"

    mod = types.ModuleType("homeassistant.components.http.auth")
    mod.async_sign_path = async_sign_path
    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth", mod)

    coord = _stub_coord_with_store()
    out = coord._sign_media_path("/media/local/a%20b.jpg")
    assert out == "/media/local/a%20b.jpg?authSig=SIG"
    assert calls["path"] == "/media/local/a%20b.jpg"
    assert calls["expiration"] == c._MEDIA_LINK_LIFETIME
    assert calls["kw"] == {"use_content_user": True}


def test_sign_media_path_falls_back_without_content_user(monkeypatch):
    import sys
    import types

    def async_sign_path(hass, path, expiration, **kw):
        if "use_content_user" in kw:
            raise TypeError("unexpected keyword argument")
        return f"{path}?authSig=OLD"

    mod = types.ModuleType("homeassistant.components.http.auth")
    mod.async_sign_path = async_sign_path
    monkeypatch.setitem(sys.modules, "homeassistant.components.http.auth", mod)

    coord = _stub_coord_with_store()
    out = coord._sign_media_path("/media/local/a.jpg")
    assert out == "/media/local/a.jpg?authSig=OLD"


def test_sign_media_path_returns_unchanged_when_signing_unavailable():
    # In the stub test env homeassistant.components.http.auth does not exist,
    # so signing is skipped and the path is returned as-is.
    coord = _stub_coord_with_store()
    assert coord._sign_media_path("/media/local/a.jpg") == "/media/local/a.jpg"


# ── Signing at download time (#62) ─────────────────────────────────────────

def _media_coord(provider=c.PROVIDER_MEDIA_SOURCE):
    coord = _stub_coord_with_store()
    coord.provider = provider
    coord._internal_base_url = lambda: "http://ha.test:8123/"
    return coord


def test_media_links_are_stored_unsigned(monkeypatch):
    from homeassistant import components

    source = _FakeMediaSource({"root": [_node("local/My Photos/a", media_class="image")]})
    monkeypatch.setattr(components, "media_source", source, raising=False)
    coord = _media_coord()
    coord.entry = SimpleNamespace(title="Test")
    coord.media_content_id = "root"

    def fail(path):
        raise AssertionError("links must not be signed when the album loads")

    coord._sign_media_path = fail
    items = asyncio.run(coord._update_media_source())["items"]
    assert [item.url for item in items] == ["http://ha.test:8123/media/local/My%20Photos/a.jpg"]


def test_media_link_signature_is_made_for_home_assistant_links_only():
    coord = _media_coord()
    signed = []

    def sign(path):
        signed.append(path)
        return f"{path}?authSig=FRESH"

    coord._sign_media_path = sign
    url = "http://ha.test:8123/immich/entry/asset/fullsize/image/jpeg"
    assert coord.media_link_signature(url) == "FRESH"
    assert signed == ["/immich/entry/asset/fullsize/image/jpeg"]
    assert coord.media_link_signature("http://jellyfin.local/photo.jpg") is None
    assert _media_coord(c.PROVIDER_IMMICH).media_link_signature(url) is None


def test_saved_media_links_drop_signatures_from_before_a_restart():
    saved = {"items": [{"url": "http://ha.test:8123/media/local/My Photos/a.jpg?authSig=OLD"}]}

    class Storage:
        async def async_load(self):
            return saved

    for provider, expected in (
        (c.PROVIDER_MEDIA_SOURCE, "http://ha.test:8123/media/local/My%20Photos/a.jpg"),
        (c.PROVIDER_GOOGLE_SHARED, saved["items"][0]["url"]),
    ):
        coord = _media_coord(provider)
        coord._items_cache_store = Storage()
        items = asyncio.run(coord._load_cached_items())["items"]
        assert items[0].url == expected
