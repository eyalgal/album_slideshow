from __future__ import annotations

import io

from PIL import Image, ImageColor, ImageFilter, ImageOps

from .coordinator import MediaItem

# Re-export fill mode and orientation constants so callers can import from here
FILL_COVER = "cover"
FILL_CONTAIN = "contain"
FILL_BLUR = "blur"


def open_image(data: bytes) -> Image.Image:
    """Open image bytes, apply EXIF orientation, normalise to RGB/RGBA."""
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    return img


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


def resolve_output_size(req_w: int | None, req_h: int | None, ratio: str) -> tuple[int, int]:
    ratio_w, ratio_h = _parse_aspect_ratio(ratio)
    target = ratio_w / ratio_h

    if req_w is None and req_h is None:
        if ratio_w >= ratio_h:
            width = 3840
            height = max(1, int(round(width / target)))
        else:
            height = 3840
            width = max(1, int(round(height * target)))
        return (width, height)

    if req_w is None:
        height = max(1, int(req_h or 2160))
        width = max(1, int(round(height * target)))
        return (width, height)

    if req_h is None:
        width = max(1, int(req_w or 3840))
        height = max(1, int(round(width / target)))
        return (width, height)

    req_w = max(1, int(req_w))
    req_h = max(1, int(req_h))
    if (req_w / req_h) >= target:
        height = req_h
        width = max(1, int(round(height * target)))
    else:
        width = req_w
        height = max(1, int(round(width / target)))

    return (width, height)


def render_image(img: Image.Image, fill_mode: str, width: int, height: int) -> Image.Image:
    """Render img into a (width x height) canvas using the given fill mode."""
    if fill_mode == FILL_CONTAIN:
        return _resize_contain(img, width, height)
    if fill_mode == FILL_BLUR:
        return _blur_fill(img, width, height)
    return _resize_cover(img, width, height)


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
) -> Image.Image:
    canvas_mode = "RGBA" if transparent_divider else "RGB"
    canvas = Image.new(canvas_mode, (target_w, target_h), divider_fill)

    if portrait_canvas:
        top_h = max(1, (target_h - divider) // 2)
        bottom_h = max(1, target_h - divider - top_h)
        top_img = render_image(img1, fill_mode, target_w, top_h)
        bottom_img = render_image(img2, fill_mode, target_w, bottom_h)
        canvas.paste(top_img.convert(canvas_mode), (0, 0))
        canvas.paste(bottom_img.convert(canvas_mode), (0, top_h + divider))
        return canvas

    left_w = max(1, (target_w - divider) // 2)
    right_w = max(1, target_w - divider - left_w)
    left_img = render_image(img1, fill_mode, left_w, target_h)
    right_img = render_image(img2, fill_mode, right_w, target_h)
    canvas.paste(left_img.convert(canvas_mode), (0, 0))
    canvas.paste(right_img.convert(canvas_mode), (left_w + divider, 0))
    return canvas


def encode_image(img: Image.Image) -> bytes:
    """Encode a PIL image to JPEG (RGB) or PNG (RGBA)."""
    out = io.BytesIO()
    if "A" in img.getbands():
        img.save(out, format="PNG", optimize=True)
        return out.getvalue()
    img.convert("RGB").save(out, format="JPEG", quality=88, optimize=True)
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


# ── Private helpers ──────────────────────────────────────────────────────────

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


def _resize_cover(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = max(target_w / src_w, target_h / src_h)
    new_w = max(1, int(round(src_w * scale)))
    new_h = max(1, int(round(src_h * scale)))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = max(0, int(round((new_w - target_w) / 2)))
    top = max(0, int(round((new_h - target_h) / 2)))
    return resized.crop((left, top, left + target_w, top + target_h))


def _resize_contain(img: Image.Image, target_w: int, target_h: int, bg=(0, 0, 0)) -> Image.Image:
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((target_w, target_h))
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), bg)
    canvas.paste(resized.convert("RGB"), ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def _blur_fill(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    bg = _resize_cover(img, target_w, target_h).filter(ImageFilter.GaussianBlur(radius=24))
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return bg
    scale = min(target_w / src_w, target_h / src_h)
    new_w = max(1, int(src_w * scale))
    new_h = max(1, int(src_h * scale))
    fg = img.resize((new_w, new_h), Image.Resampling.LANCZOS).convert("RGB")
    bg.paste(fg, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return bg
