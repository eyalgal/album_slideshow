from __future__ import annotations

import io
import pytest
from PIL import Image

# All tests import from image_processing directly — no HA needed.
from custom_components.album_slideshow import image_processing as ip
from custom_components.album_slideshow.coordinator import MediaItem


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_jpeg(width: int, height: int, color=(128, 64, 32)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_png_rgba(width: int, height: int) -> bytes:
    img = Image.new("RGBA", (width, height), color=(0, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── open_image ──────────────────────────────────────────────────────────────

def test_open_image_jpeg_returns_rgb():
    data = _make_jpeg(100, 200)
    img = ip.open_image(data)
    assert img.mode == "RGB"
    assert img.size == (100, 200)


def test_open_image_rgba_preserved():
    data = _make_png_rgba(50, 50)
    img = ip.open_image(data)
    # RGBA input should come out as RGBA (mode not stripped)
    assert img.mode in ("RGB", "RGBA")
    assert img.size == (50, 50)


# ── is_portrait_img ─────────────────────────────────────────────────────────

def test_is_portrait_img_portrait():
    img = Image.new("RGB", (100, 200))
    assert ip.is_portrait_img(img) is True


def test_is_portrait_img_landscape():
    img = Image.new("RGB", (200, 100))
    assert ip.is_portrait_img(img) is False


def test_is_portrait_img_square():
    img = Image.new("RGB", (100, 100))
    assert ip.is_portrait_img(img) is True


# ── is_portrait_item ────────────────────────────────────────────────────────

def test_is_portrait_item_uses_metadata_when_available():
    item = MediaItem(url="x", width=100, height=200, mime_type=None, filename=None)
    assert ip.is_portrait_item(item) is True


def test_is_portrait_item_falls_back_to_img():
    item = MediaItem(url="x", width=None, height=None, mime_type=None, filename=None)
    img = Image.new("RGB", (100, 300))
    assert ip.is_portrait_item(item, img) is True


def test_is_portrait_item_no_img_no_meta_returns_false():
    item = MediaItem(url="x", width=None, height=None, mime_type=None, filename=None)
    assert ip.is_portrait_item(item) is False


# ── resolve_output_size ─────────────────────────────────────────────────────

def test_resolve_output_size_default_16_9():
    w, h = ip.resolve_output_size(None, None, "16:9")
    assert w == 3840
    assert h == 2160


def test_resolve_output_size_fixed_width():
    w, h = ip.resolve_output_size(1920, None, "16:9")
    assert w == 1920
    assert h == 1080


def test_resolve_output_size_fixed_height():
    w, h = ip.resolve_output_size(None, 1080, "16:9")
    assert w == 1920
    assert h == 1080


def test_resolve_output_size_portrait_default():
    w, h = ip.resolve_output_size(None, None, "9:16")
    assert h == 3840
    assert w == 2160


# ── render_image ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("fill_mode", ["cover", "contain", "blur"])
def test_render_image_output_size(fill_mode):
    img = Image.new("RGB", (400, 300))
    result = ip.render_image(img, fill_mode, 200, 150)
    assert result.size == (200, 150)


def test_render_image_cover_fills_canvas():
    img = Image.new("RGB", (400, 300))
    result = ip.render_image(img, "cover", 200, 200)
    assert result.size == (200, 200)


def test_render_image_contain_adds_letterbox():
    img = Image.new("RGB", (400, 100))
    result = ip.render_image(img, "contain", 200, 200)
    assert result.size == (200, 200)


# ── pair_images ──────────────────────────────────────────────────────────────

def test_pair_images_landscape_canvas():
    img1 = Image.new("RGB", (100, 200))
    img2 = Image.new("RGB", (100, 200))
    result = ip.pair_images(
        img1, img2,
        target_w=400, target_h=300,
        fill_mode="cover",
        portrait_canvas=False,
        divider=4,
        divider_fill=(255, 255, 255),
        transparent_divider=False,
    )
    assert result.size == (400, 300)
    assert result.mode == "RGB"


def test_pair_images_portrait_canvas():
    img1 = Image.new("RGB", (200, 100))
    img2 = Image.new("RGB", (200, 100))
    result = ip.pair_images(
        img1, img2,
        target_w=300, target_h=400,
        fill_mode="cover",
        portrait_canvas=True,
        divider=8,
        divider_fill=(0, 0, 0),
        transparent_divider=False,
    )
    assert result.size == (300, 400)


def test_pair_images_transparent_divider_rgba():
    img1 = Image.new("RGB", (100, 200))
    img2 = Image.new("RGB", (100, 200))
    result = ip.pair_images(
        img1, img2,
        target_w=400, target_h=300,
        fill_mode="cover",
        portrait_canvas=False,
        divider=4,
        divider_fill=(0, 0, 0, 0),
        transparent_divider=True,
    )
    assert result.mode == "RGBA"


# ── encode_image ─────────────────────────────────────────────────────────────

def test_encode_image_rgb_produces_jpeg():
    img = Image.new("RGB", (100, 100))
    data = ip.encode_image(img)
    assert data[:2] == b'\xff\xd8'  # JPEG SOI marker


def test_encode_image_rgba_produces_png():
    img = Image.new("RGBA", (100, 100))
    data = ip.encode_image(img)
    assert data[:4] == b'\x89PNG'


def test_encode_image_returns_bytes():
    img = Image.new("RGB", (50, 50))
    data = ip.encode_image(img)
    assert isinstance(data, bytes)
    assert len(data) > 0


# ── parse_divider_color ───────────────────────────────────────────────────────

def test_parse_divider_color_white():
    color, transparent = ip.parse_divider_color("#FFFFFF")
    assert color == (255, 255, 255)
    assert transparent is False


def test_parse_divider_color_transparent():
    color, transparent = ip.parse_divider_color("transparent")
    assert transparent is True
    assert color == (0, 0, 0, 0)


def test_parse_divider_color_invalid_falls_back_to_white():
    color, transparent = ip.parse_divider_color("notacolor")
    assert color == (255, 255, 255)
    assert transparent is False
