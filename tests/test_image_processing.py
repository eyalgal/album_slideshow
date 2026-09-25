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


def _hints(*faces, debug=False):
    return ip.CropHints(faces=tuple(ip.FaceBox(*face) for face in faces), debug=debug)


def test_render_image_cover_without_faces_is_centred():
    img = Image.new("RGB", (100, 300), color="green")
    img.paste("red", (0, 0, 100, 100))
    assert ip.render_image(img, "cover", 100, 100).getpixel((50, 50)) == (0, 128, 0)
    assert ip.render_image(img, "cover", 100, 100, _hints()).getpixel((50, 50)) == (0, 128, 0)


def test_render_image_cover_keeps_a_face_near_the_top():
    img = Image.new("RGB", (100, 300), color="green")
    img.paste("red", (0, 0, 100, 100))
    face = (0.4, 0.1, 0.6, 0.2, 0.02)
    focused = ip.render_image(img, "cover", 100, 100, _hints(face))
    assert focused.getpixel((50, 50)) == (255, 0, 0)


def test_render_image_cover_keeps_a_face_near_the_right_edge():
    img = Image.new("RGB", (300, 100), color="green")
    img.paste("blue", (200, 0, 300, 100))
    face = (0.8, 0.3, 0.9, 0.6, 0.03)
    focused = ip.render_image(img, "cover", 100, 100, _hints(face))
    assert focused.getpixel((50, 50)) == (0, 0, 255)


# ── choose_crop_offset (1D: source 1000 px, window 400 px) ─────────────────

def test_crop_two_faces_that_fit_are_both_kept_and_centred():
    spans = [(300, 400, 1.0), (550, 650, 1.0)]
    offset = ip.choose_crop_offset(spans, 1000, 400)
    assert offset <= 300 and offset + 400 >= 650
    # Group 300..650 centred in the window.
    assert offset == pytest.approx(275)


def test_crop_faces_too_far_apart_keeps_the_biggest_whole():
    spans = [(50, 150, 1.0), (750, 950, 4.0)]
    offset = ip.choose_crop_offset(spans, 1000, 400)
    assert offset <= 750 and offset + 400 >= 950
    # The smaller face is left fully out rather than cut in half.
    assert offset >= 150


def test_crop_never_centres_between_two_distant_faces():
    spans = [(50, 150, 1.0), (850, 950, 1.0)]
    offset = ip.choose_crop_offset(spans, 1000, 400)
    kept = [a >= offset and b <= offset + 400 for a, b, _w in spans]
    assert kept.count(True) == 1


@pytest.mark.parametrize("bystander_weight", [0.05, 0.5, 100.0])
def test_crop_selected_person_beats_bigger_bystander(bystander_weight):
    selected = (100, 200, 0.001)
    bystander = (700, 950, bystander_weight)
    offset = ip.choose_crop_offset(
        [selected, bystander], 1000, 400, selected=[True, False]
    )
    assert offset <= 100 and offset + 400 >= 200


def test_crop_keeps_largest_cluster_in_group_photo():
    cluster = [(500, 560, 1.0), (600, 660, 1.0), (700, 760, 1.0)]
    loner = (50, 110, 1.0)
    offset = ip.choose_crop_offset(cluster + [loner], 1000, 400)
    assert all(a >= offset and b <= offset + 400 for a, b, _w in cluster)


def test_crop_without_faces_or_room_is_centred():
    assert ip.choose_crop_offset([], 1000, 400) == 300
    assert ip.choose_crop_offset([(0, 10, 1.0)], 400, 400) == 0


def test_face_padding_keeps_the_head_inside():
    # A face touching y=0.1 would fit unpadded at offset 0.1*H, but the
    # padding above (hair) must be inside the crop as well.
    img = Image.new("RGB", (100, 1000))
    face = (0.4, 0.3, 0.6, 0.4, 0.02)
    padded = ip._padded_face(face)
    assert padded[1] == pytest.approx(0.3 - 0.1 * 0.6)
    statuses = ip._face_statuses((face,), 100, 1000, 0, 240, 100, 200)
    assert statuses == [ip.FACE_KEPT]
    statuses = ip._face_statuses((face,), 100, 1000, 0, 350, 100, 200)
    assert statuses == [ip.FACE_CUT]
    img.close()


@pytest.mark.parametrize("selected", [False, True])
def test_padding_never_discards_a_face_that_fits(selected):
    face = (0.35, 0.10, 0.65, 0.275, 0.0525, selected)
    with Image.new("RGB", (1000, 2000), "black") as image:
        image.paste("red", (350, 200, 650, 550))
        with ip.render_image(image, "cover", 1000, 562, _hints(face)) as cropped:
            assert cropped.getbbox() is not None
            assert sum(count for count, color in cropped.getcolors() if color == (255, 0, 0)) == 300 * 350


def test_oversized_face_keeps_visible_area_instead_of_background():
    offset = ip.choose_crop_offset([(50, 650, 1.0)], 2000, 400)
    assert min(650, offset + 400) - max(50, offset) == pytest.approx(400)


def test_oversized_selected_face_stays_ahead_of_whole_bystander():
    offset = ip.choose_crop_offset(
        [(50, 650, 0.01), (1400, 1500, 1.0)], 2000, 400,
        selected=[True, False],
    )
    assert min(650, offset + 400) - max(50, offset) == pytest.approx(400)


def test_selected_face_priority_survives_metadata_and_camera_conversion():
    from custom_components.album_slideshow import camera, immich

    faces = [
        {"imageWidth": 1000, "imageHeight": 400,
         "boundingBoxX1": 100, "boundingBoxY1": 100,
         "boundingBoxX2": 150, "boundingBoxY2": 150, "person": {"id": "selected"}},
        {"imageWidth": 1000, "imageHeight": 400,
         "boundingBoxX1": 700, "boundingBoxY1": 100,
         "boundingBoxX2": 900, "boundingBoxY2": 250, "person": {"id": "bystander"}},
    ]
    item = MediaItem(
        url="test", width=1000, height=400, mime_type=None, filename=None,
        faces=immich.parse_face_boxes(faces, {"selected"}),
    )
    hints = ip.CropHints(faces=camera._item_faces(item))
    with Image.new("RGB", (1000, 400), "black") as image:
        image.paste("red", (100, 100, 150, 150))
        image.paste("blue", (700, 100, 900, 250))
        with ip.render_image(image, "cover", 400, 400, hints) as cropped:
            assert cropped.getextrema()[0][1] == 255
            assert cropped.getextrema()[2][1] == 0


# ── debug overlay ────────────────────────────────────────────────────────────

def test_debug_overlay_draws_crosshair_on_source_centre():
    img = Image.new("RGB", (300, 100), color="black")
    out = ip.render_image(img, "cover", 100, 100, _hints(debug=True))
    assert out.size == (100, 100)
    # Centred crop: the photo's centre is the crop's centre.
    assert out.getpixel((50, 50)) == ip._CROSSHAIR_COLOR


def test_debug_overlay_crosshair_moves_with_the_crop():
    img = Image.new("RGB", (300, 100), color="black")
    face = (0.85, 0.4, 0.95, 0.6, 0.02)
    out = ip.render_image(img, "cover", 100, 100, _hints(face, debug=True))
    # Crop moved to the right edge, so the source centre (x=150) is gone.
    assert out.getpixel((50, 50)) != ip._CROSSHAIR_COLOR
    assert out.size == (100, 100)
    # ...and an arrow on the left edge points back towards it.
    assert out.getpixel((1, 50)) == ip._CROSSHAIR_COLOR


def test_debug_overlay_is_off_by_default():
    img = Image.new("RGB", (300, 100), color="black")
    out = ip.render_image(img, "cover", 100, 100, _hints())
    assert out.getpixel((50, 50)) == (0, 0, 0)


def test_debug_overlay_in_contain_and_blur_keeps_size():
    img = Image.new("RGB", (300, 100), color="black")
    for mode in ("contain", "blur"):
        out = ip.render_image(img, mode, 200, 200, _hints(debug=True))
        assert out.size == (200, 200)
        assert out.getpixel((100, 100)) == ip._CROSSHAIR_COLOR


def test_face_summary_distinguishes_unknown_from_none():
    assert ip._face_summary(None, []) == "no face data"
    assert ip._face_summary((), []).startswith("faces 0")


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


@pytest.mark.parametrize("portrait_canvas", [False, True])
@pytest.mark.parametrize("photo_position", [0, 1])
@pytest.mark.parametrize("fill_mode", ["cover", "contain", "blur"])
@pytest.mark.parametrize("divider, transparent", [(0, False), (8, False), (8, True)])
def test_render_pair_photo_keeps_only_selected_half(
    monkeypatch, portrait_canvas, photo_position, fill_mode, divider, transparent
):
    size = (90, 160) if portrait_canvas else (160, 90)
    with Image.new("RGB", (100, 100), (240, 0, 0)) as first:
        with Image.new("RGB", (100, 100), (0, 0, 240)) as second:
            with ip.pair_images(
                first, second, *size, fill_mode, portrait_canvas, divider,
                (0, 0, 0, 0) if transparent else (255, 255, 255), transparent,
            ) as paired:
                data = ip.encode_image(paired)
    length = size[1] if portrait_canvas else size[0]
    first_length = (length - divider) // 2
    start, end = (0, first_length) if photo_position == 0 else (first_length + divider, length)
    box = (0, start, size[0], end) if portrait_canvas else (start, 0, end, size[1])
    with ip.open_image(data) as paired:
        with paired.crop(box) as expected:
            expected_input = (expected.size, expected.tobytes())
    rendered_inputs = []
    original_render = ip.render_image

    def render_selected(photo, mode, width, height):
        rendered_inputs.append((photo.size, photo.tobytes()))
        return original_render(photo, mode, width, height)

    monkeypatch.setattr(ip, "render_image", render_selected)
    result = ip.render_pair_photo(data, photo_position, portrait_canvas, divider, fill_mode)
    assert rendered_inputs == [expected_input]
    with ip.open_image(result) as rendered:
        assert rendered.size == size
        center = rendered.getpixel((size[0] // 2, size[1] // 2))
        selected_channel = 0 if photo_position == 0 else 2
        other_channel = 2 if photo_position == 0 else 0
        assert center[selected_channel] > 220
        assert center[other_channel] < 15


def test_render_pair_photo_rejects_invalid_position():
    with pytest.raises(ValueError, match="position"):
        ip.render_pair_photo(_make_jpeg(100, 50), 2, False, 8, "contain")


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


# -- is_portrait_item_by_metadata -------------------------------------------

def test_metadata_only_portrait():
    item = MediaItem(url="x", width=100, height=200, mime_type=None, filename=None)
    assert ip.is_portrait_item_by_metadata(item) is True


def test_metadata_only_landscape():
    item = MediaItem(url="x", width=300, height=100, mime_type=None, filename=None)
    assert ip.is_portrait_item_by_metadata(item) is False


def test_metadata_only_unknown_returns_none():
    item = MediaItem(url="x", width=None, height=None, mime_type=None, filename=None)
    assert ip.is_portrait_item_by_metadata(item) is None


def test_metadata_only_zero_dims_returns_none():
    item = MediaItem(url="x", width=0, height=0, mime_type=None, filename=None)
    assert ip.is_portrait_item_by_metadata(item) is None


# -- encode_image (Android compatibility) -----------------------------------

def test_encoded_jpeg_is_baseline_not_progressive():
    # Baseline JPEGs start with SOF0 (0xFFC0). Progressive would be SOF2 (0xFFC2).
    # Scan the encoded bytes for an SOF marker.
    img = Image.new("RGB", (64, 64), color=(100, 150, 200))
    data = ip.encode_image(img)
    # Find first SOF marker (0xFFCn where n in {0,1,2,3})
    sof = None
    for i in range(len(data) - 1):
        if data[i] == 0xFF and data[i + 1] in (0xC0, 0xC1, 0xC2, 0xC3):
            sof = data[i + 1]
            break
    assert sof is not None, "No SOF marker found in JPEG"
    assert sof == 0xC0, f"Expected baseline (SOF0=0xC0), got 0x{sof:02X}"


# -- safe_close -------------------------------------------------------------

def test_safe_close_none_is_noop():
    ip.safe_close(None)  # must not raise


def test_safe_close_closes_image():
    img = Image.new("RGB", (10, 10))
    ip.safe_close(img)
    # Accessing .load on a closed image raises; we just assert no exception
    # from the close call itself.


# -- open_image draft-mode does not crash on non-JPEG -----------------------

def test_open_image_with_target_size_png_ok():
    # PNG has no draft support; open_image must not raise when given a target.
    data = _make_png_rgba(100, 100)
    img = ip.open_image(data, target_size=(50, 50))
    assert img.size == (100, 100)
    ip.safe_close(img)


def test_open_image_with_target_size_jpeg_ok():
    data = _make_jpeg(2000, 2000)
    img = ip.open_image(data, target_size=(200, 200))
    # Draft mode is best-effort; we don't require a specific downscale,
    # only that we got a usable image back.
    assert img.mode == "RGB"
    assert img.size[0] > 0 and img.size[1] > 0
    ip.safe_close(img)
