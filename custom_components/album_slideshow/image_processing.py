from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import NamedTuple

from PIL import Image, ImageColor, ImageDraw, ImageFilter, ImageFont, ImageOps

from .coordinator import MediaItem

_LOGGER = logging.getLogger(__name__)

# Re-export fill mode constants so callers can import from here.
FILL_COVER = "cover"
FILL_CONTAIN = "contain"
FILL_BLUR = "blur"

class FaceBox(NamedTuple):
    left: float
    top: float
    right: float
    bottom: float
    weight: float
    selected: bool = False


@dataclass(frozen=True)
class CropHints:
    """Per-photo inputs for face-aware cropping and the debug overlay.

    ``faces`` is None when no face data is known for the photo (not an
    Immich source, or not scanned yet) and an empty tuple when the photo
    was scanned and has no faces.
    """

    faces: tuple[FaceBox, ...] | None = None
    debug: bool = False
    name: str | None = None


# Padding around a face box, as a fraction of the face's own size, so the
# crop keeps the whole head rather than just the tight face box.
_FACE_PAD_SIDE = 0.3
_FACE_PAD_TOP = 0.6
_FACE_PAD_BOTTOM = 0.3
# A face that is cut by the crop edge counts this much worse than one that is
# left out entirely: half a face looks like a mistake, a missing face doesn't.
_PARTIAL_PENALTY = 1.5

FACE_KEPT = "kept"
FACE_CUT = "cut"
FACE_DROPPED = "dropped"

# Absolute pixel ceiling. A 20000x20000 JPEG decodes to ~1.2 GB of RGB; Pillow
# raises DecompressionBombError above MAX_IMAGE_PIXELS. We set this high enough
# that 4K+ sources still decode, but reject anything absurd to protect
# low-memory devices like the Home Assistant Green.
_MAX_IMAGE_PIXELS = 80_000_000  # ~8K x 10K
Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS


def open_image(
    data: bytes,
    target_size: tuple[int, int] | None = None,
) -> Image.Image:
    """Open image bytes, apply EXIF orientation, normalise to RGB/RGBA.

    If ``target_size`` is given, uses PIL's ``draft`` mode so libjpeg decodes
    at a reduced scale. Big speed/memory win on low-power devices when the
    source is much larger than the output canvas.
    """
    img = Image.open(io.BytesIO(data))
    if target_size is not None and img.format == "JPEG":
        try:
            img.draft("RGB", target_size)
        except Exception:
            pass
    img = ImageOps.exif_transpose(img)
    # Force pixel data into memory; BytesIO must stay reachable until here.
    img.load()
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


def safe_close(img: Image.Image | None) -> None:
    """Close a PIL image without raising. No-op on None."""
    if img is None:
        return
    try:
        img.close()
    except Exception:
        pass


def is_portrait_img(img: Image.Image) -> bool:
    try:
        w, h = img.size
        return h >= w
    except Exception:
        return False


def is_portrait_item(item: MediaItem, img: Image.Image | None = None) -> bool:
    by_meta = _is_portrait_dims(item.width, item.height)
    if by_meta is not None:
        return by_meta
    if img is not None:
        return is_portrait_img(img)
    return False


def is_portrait_item_by_metadata(item: MediaItem) -> bool | None:
    """Return portrait/landscape from item metadata only, or None if unknown."""
    return _is_portrait_dims(item.width, item.height)


def resolve_output_size(
    req_w: int | None,
    req_h: int | None,
    ratio: str,
    max_short_edge: int | None = None,
) -> tuple[int, int]:
    ratio_w, ratio_h = _parse_aspect_ratio(ratio)
    target = ratio_w / ratio_h

    if req_w is None and req_h is None:
        if ratio_w >= ratio_h:
            width = 3840
            height = max(1, int(round(width / target)))
        else:
            height = 3840
            width = max(1, int(round(height * target)))
    elif req_w is None:
        height = max(1, int(req_h or 2160))
        width = max(1, int(round(height * target)))
    elif req_h is None:
        width = max(1, int(req_w or 3840))
        height = max(1, int(round(width / target)))
    else:
        req_w = max(1, int(req_w))
        req_h = max(1, int(req_h))
        if (req_w / req_h) >= target:
            height = req_h
            width = max(1, int(round(height * target)))
        else:
            width = req_w
            height = max(1, int(round(width / target)))

    if max_short_edge is not None:
        short = min(width, height)
        if short > max_short_edge:
            scale = max_short_edge / short
            width = max(1, int(round(width * scale)))
            height = max(1, int(round(height * scale)))

    return (width, height)


def render_image(
    img: Image.Image,
    fill_mode: str,
    width: int,
    height: int,
    hints: CropHints | None = None,
) -> Image.Image:
    """Render img into a (width x height) canvas using the given fill mode."""
    hints = hints or CropHints()
    if fill_mode == FILL_CONTAIN:
        return _resize_contain(img, width, height, hints=hints)
    if fill_mode == FILL_BLUR:
        return _blur_fill(img, width, height, hints=hints)
    return _resize_cover(img, width, height, hints)


def pair_images(
    img1: Image.Image,
    img2: Image.Image,
    target_w: int,
    target_h: int,
    fill_mode: str,
    portrait_canvas: bool,
    divider: int,
    divider_fill: tuple[int, int, int] | tuple[int, int, int, int],
    transparent_divider: bool,
    hints1: CropHints | None = None,
    hints2: CropHints | None = None,
) -> Image.Image:
    canvas_mode = "RGBA" if transparent_divider else "RGB"
    canvas = Image.new(canvas_mode, (target_w, target_h), divider_fill)

    if portrait_canvas:
        top_h = max(1, (target_h - divider) // 2)
        bottom_h = max(1, target_h - divider - top_h)
        top_img = render_image(img1, fill_mode, target_w, top_h, hints1)
        bottom_img = render_image(img2, fill_mode, target_w, bottom_h, hints2)
        canvas.paste(top_img.convert(canvas_mode), (0, 0))
        canvas.paste(bottom_img.convert(canvas_mode), (0, top_h + divider))
        safe_close(top_img)
        safe_close(bottom_img)
        return canvas

    left_w = max(1, (target_w - divider) // 2)
    right_w = max(1, target_w - divider - left_w)
    left_img = render_image(img1, fill_mode, left_w, target_h, hints1)
    right_img = render_image(img2, fill_mode, right_w, target_h, hints2)
    canvas.paste(left_img.convert(canvas_mode), (0, 0))
    canvas.paste(right_img.convert(canvas_mode), (left_w + divider, 0))
    safe_close(left_img)
    safe_close(right_img)
    return canvas


def render_pair_photo(
    data: bytes,
    photo_position: int,
    portrait_canvas: bool,
    divider: int,
    fill_mode: str,
) -> bytes:
    """Render one safe photo from an already composed pair without source I/O."""
    if photo_position not in (0, 1):
        raise ValueError("Invalid paired-photo position")
    with open_image(data) as paired:
        width, height = paired.size
        length = height if portrait_canvas else width
        first_length = max(1, (length - divider) // 2)
        start, end = (
            (0, first_length) if photo_position == 0
            else (first_length + divider, length)
        )
        if not 0 <= start < end <= length:
            raise ValueError("Paired photo is outside the rendered canvas")
        box = (0, start, width, end) if portrait_canvas else (start, 0, end, height)
        with paired.crop(box) as photo:
            with render_image(photo, fill_mode, width, height) as rendered:
                return encode_image(rendered)


def encode_image(img: Image.Image) -> bytes:
    """Encode a PIL image to a client-compatible JPEG or PNG.

    JPEGs are written as baseline (non-progressive) with 4:2:0 subsampling and
    without EXIF, which maximises compatibility with Android WebView and older
    clients. RGBA images are encoded as PNG to preserve alpha.
    """
    out = io.BytesIO()
    if "A" in img.getbands():
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    rgb = img if img.mode == "RGB" else img.convert("RGB")
    rgb.save(
        out,
        format="JPEG",
        quality=88,
        optimize=True,
        progressive=False,
        subsampling=2,
    )
    if rgb is not img:
        safe_close(rgb)
    return out.getvalue()


def parse_divider_color(color: str) -> tuple[tuple[int, int, int] | tuple[int, int, int, int], bool]:
    raw = (color or "").strip().lower()
    compact = raw.replace(" ", "")
    if compact in ("transparent", "transperant", "none", "clear", "rgba(0,0,0,0)"):
        return (0, 0, 0, 0), True
    try:
        return ImageColor.getrgb(color), False
    except Exception:
        return (255, 255, 255), False


# -- Private helpers ---------------------------------------------------------

def _is_portrait_dims(width: int | None, height: int | None) -> bool | None:
    if not width or not height:
        return None
    try:
        w, h = int(width), int(height)
        if w <= 0 or h <= 0:
            return None
        return h >= w
    except Exception:
        return None


def _parse_aspect_ratio(ratio: str) -> tuple[int, int]:
    try:
        left, right = ratio.split(":", maxsplit=1)
        w, h = int(left), int(right)
        if w > 0 and h > 0:
            return (w, h)
    except Exception:
        pass
    return (16, 9)


def _padded_face(face: FaceBox) -> tuple[float, float, float, float]:
    x1, y1, x2, y2 = face[:4]
    fw, fh = x2 - x1, y2 - y1
    return (
        max(0.0, x1 - fw * _FACE_PAD_SIDE),
        max(0.0, y1 - fh * _FACE_PAD_TOP),
        min(1.0, x2 + fw * _FACE_PAD_SIDE),
        min(1.0, y2 + fh * _FACE_PAD_BOTTOM),
    )


def _span_status(a: float, b: float, offset: float, window: float) -> str:
    # One pixel of slack absorbs rounding when the image is scaled to fit
    # an axis exactly.
    if a >= offset - 1 and b <= offset + window + 1:
        return FACE_KEPT
    if b <= offset or a >= offset + window:
        return FACE_DROPPED
    return FACE_CUT


def choose_crop_offset(
    spans: list[tuple[float, float, float]],
    src_len: float,
    window: float,
    *,
    selected: list[bool] | None = None,
    padded_spans: list[tuple[float, float]] | None = None,
) -> float:
    """Pick where a crop window of ``window`` px starts along one axis.

    ``spans`` are unpadded ``(start, end, weight)`` face intervals. Selected
    people rank ahead of all bystanders. Keep whole faces where possible,
    retain visible face area otherwise, and use padding as a preference.
    """
    max_offset = max(0.0, src_len - window)
    centred = max_offset / 2
    if max_offset <= 0 or not spans:
        return centred

    def clamp(value: float) -> float:
        return max(0.0, min(max_offset, value))

    priorities = selected if selected is not None else [False] * len(spans)
    padding = padded_spans if padded_spans is not None else [span[:2] for span in spans]

    def visible_fraction(start: float, end: float, offset: float) -> float:
        overlap = max(0.0, min(end, offset + window) - max(start, offset))
        return overlap / (end - start)

    def score(offset: float) -> tuple[float, ...]:
        tiers = []
        for priority in (True, False):
            kept_weight = cut_weight = visible_weight = 0.0
            for (start, end, weight), is_selected in zip(spans, priorities):
                if is_selected != priority:
                    continue
                state = _span_status(start, end, offset, window)
                if state == FACE_KEPT:
                    kept_weight += weight
                elif state == FACE_CUT:
                    cut_weight += weight
                visible_weight += weight * visible_fraction(start, end, offset)
            tiers.extend((
                kept_weight,
                -_PARTIAL_PENALTY * cut_weight if kept_weight else visible_weight,
            ))
        tiers.append(sum(
            span[2] * visible_fraction(start, end, offset)
            for span, (start, end) in zip(spans, padding)
        ))
        return tuple(round(value, 12) for value in tiers)

    candidates = {0.0, centred, max_offset}
    for start, end in [span[:2] for span in spans] + padding:
        for offset in (start, end - window, end, start - window, (start + end - window) / 2):
            candidates.add(clamp(offset))
    best = max(candidates, key=lambda offset: (score(offset), -abs(offset - centred), -offset))

    kept = [
        index for index, (start, end, _weight) in enumerate(spans)
        if _span_status(start, end, best, window) == FACE_KEPT
    ]
    if not kept:
        return best
    group_start = min(padding[index][0] for index in kept)
    group_end = max(padding[index][1] for index in kept)
    if group_end - group_start > window:
        group_start = min(spans[index][0] for index in kept)
        group_end = max(spans[index][1] for index in kept)
    target = clamp((group_start + group_end - window) / 2)
    candidates.add(target)
    return max(candidates, key=lambda offset: (score(offset), -abs(offset - target), -offset))


def _face_statuses(
    faces: tuple[FaceBox, ...],
    new_w: int,
    new_h: int,
    left: int,
    top: int,
    target_w: int,
    target_h: int,
) -> list[str]:
    statuses = []
    for face in faces:
        px1, py1, px2, py2 = face[:4]
        x = _span_status(px1 * new_w, px2 * new_w, left, target_w)
        y = _span_status(py1 * new_h, py2 * new_h, top, target_h)
        if FACE_DROPPED in (x, y):
            statuses.append(FACE_DROPPED)
        elif FACE_CUT in (x, y):
            statuses.append(FACE_CUT)
        else:
            statuses.append(FACE_KEPT)
    return statuses


def _resize_cover(
    img: Image.Image,
    target_w: int,
    target_h: int,
    hints: CropHints | None = None,
) -> Image.Image:
    hints = hints or CropHints()
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

    faces = tuple(FaceBox(*face) for face in hints.faces or ())
    padded = [_padded_face(face) for face in faces]
    left = choose_crop_offset(
        [(face.left * new_w, face.right * new_w, face.weight) for face in faces],
        new_w,
        target_w,
        selected=[face.selected for face in faces],
        padded_spans=[(box[0] * new_w, box[2] * new_w) for box in padded],
    )
    top = choose_crop_offset(
        [(face.top * new_h, face.bottom * new_h, face.weight) for face in faces],
        new_h,
        target_h,
        selected=[face.selected for face in faces],
        padded_spans=[(box[1] * new_h, box[3] * new_h) for box in padded],
    )
    left = max(0, min(max(0, new_w - target_w), int(round(left))))
    top = max(0, min(max(0, new_h - target_h), int(round(top))))

    statuses = _face_statuses(faces, new_w, new_h, left, top, target_w, target_h)
    summary = _face_summary(hints.faces, statuses)
    if faces:
        _LOGGER.debug("Crop %s: %s", hints.name or "photo", summary)
    if hints.debug:
        _draw_debug_marks(resized, faces, statuses)

    cropped = resized.crop((left, top, left + target_w, top + target_h))
    if cropped is not resized:
        safe_close(resized)
    if hints.debug:
        _draw_offscreen_centre(cropped, new_w / 2 - left, new_h / 2 - top)
        _draw_debug_label(cropped, summary)
    return cropped


def _face_summary(faces: tuple[FaceBox, ...] | None, statuses: list[str]) -> str:
    if faces is None:
        return "no face data"
    return (
        f"faces {len(statuses)} · kept {statuses.count(FACE_KEPT)} · "
        f"cut {statuses.count(FACE_CUT)} · dropped {statuses.count(FACE_DROPPED)}"
    )


_DEBUG_COLORS = {
    FACE_KEPT: (0, 220, 0),
    FACE_CUT: (255, 40, 40),
    FACE_DROPPED: (160, 160, 160),
}
_CROSSHAIR_COLOR = (255, 220, 0)


def _draw_debug_marks(
    img: Image.Image, faces: tuple[FaceBox, ...], statuses: list[str]
) -> None:
    """Draw face boxes and a crosshair on the photo's own centre.

    Drawn before cropping, so the marks get cut exactly like the photo: a
    crosshair that is off-centre or missing shows how much was cropped away.
    """
    w, h = img.size
    draw = ImageDraw.Draw(img)
    line = max(2, round(min(w, h) / 200))
    for face, status in zip(faces, statuses):
        color = _DEBUG_COLORS[status]
        x1, y1, x2, y2 = face[:4]
        draw.rectangle((x1 * w, y1 * h, x2 * w, y2 * h), outline=color, width=line)
        px1, py1, px2, py2 = _padded_face(face)
        draw.rectangle(
            (px1 * w, py1 * h, px2 * w, py2 * h),
            outline=color,
            width=max(1, line // 2),
        )
    cx, cy = w / 2, h / 2
    arm = max(12, round(min(w, h) / 12))
    for width, color in ((line + 2, (0, 0, 0)), (line, _CROSSHAIR_COLOR)):
        draw.line((cx - arm, cy, cx + arm, cy), fill=color, width=width)
        draw.line((cx, cy - arm, cx, cy + arm), fill=color, width=width)
    radius = arm / 3
    draw.ellipse(
        (cx - radius, cy - radius, cx + radius, cy + radius),
        outline=_CROSSHAIR_COLOR,
        width=line,
    )


def _draw_offscreen_centre(img: Image.Image, cx: float, cy: float) -> None:
    """When the photo's centre was cropped away, point at it from the edge."""
    w, h = img.size
    if 0 <= cx <= w and 0 <= cy <= h:
        return
    size = max(10, round(min(w, h) / 20))
    x = max(size, min(w - size, cx))
    y = max(size, min(h - size, cy))
    if cx < 0:
        points = [(0, y), (size, y - size), (size, y + size)]
    elif cx > w:
        points = [(w, y), (w - size, y - size), (w - size, y + size)]
    elif cy < 0:
        points = [(x, 0), (x - size, size), (x + size, size)]
    else:
        points = [(x, h), (x - size, h - size), (x + size, h - size)]
    ImageDraw.Draw(img).polygon(points, fill=_CROSSHAIR_COLOR, outline=(0, 0, 0))


def _debug_font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1 has a fixed-size default font
        return ImageFont.load_default()


def _draw_debug_label(img: Image.Image, text: str) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    size = max(12, round(min(w, h) / 30))
    font = _debug_font(size)
    pad = max(4, size // 3)
    x1, y1, x2, y2 = draw.textbbox((pad, pad), text, font=font)
    draw.rectangle((x1 - pad, y1 - pad, x2 + pad, y2 + pad), fill=(0, 0, 0))
    draw.text((pad, pad), text, fill=(255, 255, 255), font=font)


def _resize_contain(
    img: Image.Image,
    target_w: int,
    target_h: int,
    bg=(0, 0, 0),
    hints: CropHints | None = None,
) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    _debug_uncropped(resized, hints)
    canvas = Image.new("RGB", (target_w, target_h), bg)
    rgb_resized = resized if resized.mode == "RGB" else resized.convert("RGB")
    canvas.paste(rgb_resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    if rgb_resized is not resized:
        safe_close(rgb_resized)
    safe_close(resized)
    return canvas


def _debug_uncropped(img: Image.Image, hints: CropHints | None) -> None:
    """Debug marks for fill modes that never crop: every face is kept."""
    if hints is None or not hints.debug:
        return
    faces = hints.faces or ()
    statuses = [FACE_KEPT] * len(faces)
    _draw_debug_marks(img, faces, statuses)
    _draw_debug_label(img, _face_summary(hints.faces, statuses))


def _blur_fill(
    img: Image.Image,
    target_w: int,
    target_h: int,
    hints: CropHints | None = None,
) -> Image.Image:
    bg = _resize_cover(img, target_w, target_h).filter(ImageFilter.GaussianBlur(radius=24))
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return bg
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    _debug_uncropped(fg, hints)
    rgb_fg = fg if fg.mode == "RGB" else fg.convert("RGB")
    bg.paste(rgb_fg, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    if rgb_fg is not fg:
        safe_close(rgb_fg)
    safe_close(fg)
    return bg
