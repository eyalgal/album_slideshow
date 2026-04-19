# Performance Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move all PIL image processing off the HA event loop into executor threads, serve camera images from a pre-rendered framebuffer, and replace the download cache's entry-count cap with a user-configurable byte budget.

**Architecture:** A background asyncio Task (`_render_loop`) owns all rendering — fetch → open → render → encode — with each PIL step in a thread executor. `async_camera_image` becomes a one-liner that returns whatever is in `_framebuffer`. All pure PIL functions move to a new `image_processing.py` module with no HA dependencies.

**Tech Stack:** Python 3.11+, Pillow, asyncio, Home Assistant Core, pytest

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `custom_components/album_slideshow/image_processing.py` | **Create** | All synchronous PIL functions — no HA, no async |
| `custom_components/album_slideshow/camera.py` | **Modify** | Background render loop, framebuffer, byte-budget download cache |
| `custom_components/album_slideshow/store.py` | **Modify** | Add `image_cache_mb` setting |
| `custom_components/album_slideshow/number.py` | **Modify** | Add `ImageCacheMbNumber` entity (follows existing RestoreEntity pattern) |
| `custom_components/album_slideshow/const.py` | **Modify** | Add `CONF_IMAGE_CACHE_MB`, `DEFAULT_IMAGE_CACHE_MB` |
| `tests/__init__.py` | **Create** | Test package marker |
| `tests/test_image_processing.py` | **Create** | Unit tests for all public functions in `image_processing.py` |
| `tests/test_download_cache.py` | **Create** | Unit tests for `_DownloadCache` eviction logic |
| `tests/requirements.txt` | **Create** | `pytest` and `Pillow` |

> **Note on spec vs codebase:** The spec mentions adding `image_cache_mb` to `config_flow.py` as an OptionsFlow. Looking at the actual codebase, there is no OptionsFlow — all settings are HA entities that persist via `RestoreEntity` (see `number.py`, `select.py`). `image_cache_mb` follows this same pattern as a `NumberEntity`.

---

## Task 1: Create feature branch

**Files:** none

- [ ] **Step 1: Create and switch to feature branch**

```bash
git checkout -b fix/performance-event-loop
```

Expected: `Switched to a new branch 'fix/performance-event-loop'`

- [ ] **Step 2: Verify clean working tree**

```bash
git status
```

Expected: `nothing to commit, working tree clean`

---

## Task 2: Set up test infrastructure

**Files:**
- Create: `tests/__init__.py`
- Create: `tests/requirements.txt`

- [ ] **Step 1: Create test package**

```bash
mkdir -p tests
```

Create `tests/__init__.py` (empty file):

```python
```

- [ ] **Step 2: Create test requirements**

Create `tests/requirements.txt`:

```
pytest>=7.0
Pillow>=9.0
```

- [ ] **Step 3: Install test dependencies**

```bash
pip install -r tests/requirements.txt
```

Expected: packages install without error.

- [ ] **Step 4: Verify pytest runs**

```bash
python -m pytest tests/ -v
```

Expected: `no tests ran` (0 collected), exit code 5 (no tests found) or 0.

---

## Task 3: Create `image_processing.py` (TDD)

**Files:**
- Create: `custom_components/album_slideshow/image_processing.py`
- Create: `tests/test_image_processing.py`

The spec calls for extracting all PIL logic from `camera.py` into a pure module. Write the tests first, then extract.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_image_processing.py`:

```python
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
    assert ip.is_portrait_img(img) is False


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
```

- [ ] **Step 2: Run tests to confirm they fail (module not yet created)**

```bash
python -m pytest tests/test_image_processing.py -v 2>&1 | head -20
```

Expected: `ModuleNotFoundError` or `ImportError` — `image_processing` does not exist yet.

- [ ] **Step 3: Create `image_processing.py` by extracting from `camera.py`**

Create `custom_components/album_slideshow/image_processing.py`:

```python
from __future__ import annotations

import io
from pathlib import Path
from typing import Any

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
        return h > w
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
    """Render img into a (width × height) canvas using the given fill mode."""
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
```

- [ ] **Step 4: Run the tests**

```bash
python -m pytest tests/test_image_processing.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add custom_components/album_slideshow/image_processing.py tests/__init__.py tests/test_image_processing.py tests/requirements.txt
git commit -m "feat: extract PIL functions into image_processing module with tests"
```

---

## Task 4: Add `image_cache_mb` setting

**Files:**
- Modify: `custom_components/album_slideshow/const.py`
- Modify: `custom_components/album_slideshow/store.py`
- Modify: `custom_components/album_slideshow/number.py`

- [ ] **Step 1: Add constants to `const.py`**

Add to the end of `custom_components/album_slideshow/const.py`:

```python
CONF_IMAGE_CACHE_MB = "image_cache_mb"
DEFAULT_IMAGE_CACHE_MB = 150
```

- [ ] **Step 2: Add `image_cache_mb` to `SlideshowStore`**

In `custom_components/album_slideshow/store.py`, add the import and field:

```python
from .const import (
    DEFAULT_SLIDE_INTERVAL,
    DEFAULT_REFRESH_HOURS,
    DEFAULT_FILL_MODE,
    DEFAULT_ORIENTATION_MISMATCH_MODE,
    DEFAULT_ORDER_MODE,
    DEFAULT_ASPECT_RATIO,
    DEFAULT_PAIR_DIVIDER_PX,
    DEFAULT_PAIR_DIVIDER_COLOR,
    DEFAULT_IMAGE_CACHE_MB,
)
```

Add field to the `SlideshowStore` dataclass (after `pair_divider_color`):

```python
    image_cache_mb: int = DEFAULT_IMAGE_CACHE_MB
```

- [ ] **Step 3: Add `ImageCacheMbNumber` entity to `number.py`**

In `custom_components/album_slideshow/number.py`, add `CONF_IMAGE_CACHE_MB` to the `async_setup_entry` entity list and add the class.

Update `async_setup_entry` to include the new entity:

```python
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    store: SlideshowStore = hass.data[DOMAIN][entry.entry_id]["store"]
    coordinator: AlbumCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    async_add_entities(
        [
            SlideIntervalNumber(entry, store),
            RefreshHoursNumber(entry, store, coordinator),
            PairDividerWidthNumber(entry, store),
            ImageCacheMbNumber(entry, store),
        ]
    )
```

Add the new class at the end of `number.py`:

```python
class ImageCacheMbNumber(_BaseNumber):
    _attr_icon = "mdi:database-outline"
    _attr_native_min_value = 50
    _attr_native_max_value = 1000
    _attr_native_step = 50
    _attr_native_unit_of_measurement = "MB"

    def __init__(self, entry: ConfigEntry, store: SlideshowStore) -> None:
        super().__init__(entry, store)
        self._attr_unique_id = f"{entry.entry_id}_image_cache_mb"
        self._attr_name = "Image cache size (MB)"

    @property
    def native_value(self):
        return int(self.store.image_cache_mb)

    async def async_set_native_value(self, value: float) -> None:
        self.store.image_cache_mb = max(50, int(value))
        self.store.notify()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old and old.state not in (None, "unknown", "unavailable"):
            try:
                self.store.image_cache_mb = max(50, int(float(old.state)))
                self.store.notify()
            except Exception:
                return
```

- [ ] **Step 4: Verify imports and syntax**

```bash
python -c "from custom_components.album_slideshow.store import SlideshowStore; s = SlideshowStore(); print(s.image_cache_mb)"
```

Expected: `150`

- [ ] **Step 5: Commit**

```bash
git add custom_components/album_slideshow/const.py custom_components/album_slideshow/store.py custom_components/album_slideshow/number.py
git commit -m "feat: add image_cache_mb setting with NumberEntity"
```

---

## Task 5: Refactor download cache to byte-budget

**Files:**
- Modify: `custom_components/album_slideshow/camera.py`
- Create: `tests/test_download_cache.py`

The download cache currently evicts when it exceeds 120 entries. Replace with a `_DownloadCache` class that evicts by total bytes, using `store.image_cache_mb` as the budget.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_download_cache.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

# _DownloadCache and _BytesCache are module-level in camera.py.
# We import them directly to test eviction logic without HA.
from custom_components.album_slideshow.camera import _DownloadCache


def test_put_and_get():
    cache = _DownloadCache(max_bytes=10_000)
    cache.put("http://a", b"hello")
    assert cache.get("http://a") == b"hello"


def test_get_missing_returns_none():
    cache = _DownloadCache(max_bytes=10_000)
    assert cache.get("http://missing") is None


def test_evicts_oldest_when_over_budget():
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://a", b"12345")   # 5 bytes
    cache.put("http://b", b"67890")   # 5 bytes — now at limit
    cache.put("http://c", b"ABCDE")   # 5 bytes — pushes over; evict oldest
    assert cache.get("http://a") is None   # evicted
    assert cache.get("http://b") is not None
    assert cache.get("http://c") is not None


def test_overwrite_updates_byte_count():
    cache = _DownloadCache(max_bytes=10)
    cache.put("http://a", b"12345")   # 5 bytes
    cache.put("http://a", b"XY")      # overwrite with 2 bytes
    assert cache.get("http://a") == b"XY"
    # Total is now 2 bytes; adding 9 more should not evict
    cache.put("http://b", b"123456789")  # 9 bytes → total 11, evict smallest (a=2)
    assert cache.get("http://b") is not None


def test_resize_evicts_to_fit():
    cache = _DownloadCache(max_bytes=20)
    cache.put("http://a", b"1234567890")  # 10 bytes
    cache.put("http://b", b"1234567890")  # 10 bytes — at limit
    cache.resize(max_bytes=10)             # shrink; a should be evicted
    assert cache.get("http://a") is None
    assert cache.get("http://b") is not None


def test_total_bytes_tracked_correctly():
    cache = _DownloadCache(max_bytes=100)
    cache.put("http://a", b"hello")
    cache.put("http://b", b"world!")
    assert cache.total_bytes == 11
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_download_cache.py -v 2>&1 | head -20
```

Expected: `ImportError` — `_DownloadCache` does not exist yet.

- [ ] **Step 3: Add `_DownloadCache` class to `camera.py`**

In `custom_components/album_slideshow/camera.py`, replace the `_BytesCache` dataclass and add `_DownloadCache`. Find the current `_BytesCache` definition:

```python
@dataclass
class _BytesCache:
    when: datetime
    data: bytes
```

Replace with:

```python
@dataclass
class _BytesCache:
    when: datetime
    data: bytes


class _DownloadCache:
    """Byte-budget LRU cache for downloaded image data."""

    def __init__(self, max_bytes: int) -> None:
        self._cache: dict[str, _BytesCache] = {}
        self._total_bytes: int = 0
        self._max_bytes: int = max(max_bytes, 1)

    @property
    def total_bytes(self) -> int:
        return self._total_bytes

    def get(self, url: str) -> bytes | None:
        entry = self._cache.get(url)
        return entry.data if entry else None

    def put(self, url: str, data: bytes) -> None:
        if url in self._cache:
            self._total_bytes -= len(self._cache[url].data)
        self._cache[url] = _BytesCache(when=datetime.utcnow(), data=data)
        self._total_bytes += len(data)
        self._evict()

    def resize(self, max_bytes: int) -> None:
        self._max_bytes = max(max_bytes, 1)
        self._evict()

    def _evict(self) -> None:
        while self._total_bytes > self._max_bytes and self._cache:
            oldest_key = min(self._cache, key=lambda k: self._cache[k].when)
            self._total_bytes -= len(self._cache[oldest_key].data)
            del self._cache[oldest_key]
```

- [ ] **Step 4: Update `_fetch_bytes` to use `_DownloadCache`**

In `AlbumSlideshowCamera.__init__`, replace:

```python
        self._download_cache: dict[str, _BytesCache] = {}
```

with:

```python
        self._download_cache = _DownloadCache(
            max_bytes=store.image_cache_mb * 1024 * 1024
        )
```

Add a store listener to resize the cache when `image_cache_mb` changes. In `__init__`, update `_on_store_change`:

```python
        def _on_store_change() -> None:
            self._download_cache.resize(self.store.image_cache_mb * 1024 * 1024)
            self._render_cache = None
            self.async_write_ha_state()

        store.add_listener(_on_store_change)
```

Update `_fetch_bytes` to use the new cache API. Find the existing `_fetch_bytes` method and replace its cache read/write logic:

Old cache read (around line 522):
```python
        now = datetime.utcnow()
        cached = self._download_cache.get(url)
        if cached and (now - cached.when) < timedelta(minutes=10):
            return cached.data
```

New cache read:
```python
        cached = self._download_cache.get(url)
        if cached is not None:
            return cached
```

Old cache write (around line 545):
```python
        self._download_cache[url] = _BytesCache(when=now, data=data)

        if len(self._download_cache) > 120:
            oldest = sorted(self._download_cache.items(), key=lambda kv: kv[1].when)[:25]
            for k, _ in oldest:
                self._download_cache.pop(k, None)
```

New cache write:
```python
        self._download_cache.put(url, data)
```

Also remove `now = datetime.utcnow()` from `_fetch_bytes` if it is no longer used there.

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit**

```bash
git add custom_components/album_slideshow/camera.py tests/test_download_cache.py
git commit -m "refactor: replace download cache entry-count limit with byte-budget eviction"
```

---

## Task 6: Implement the background render loop

**Files:**
- Modify: `custom_components/album_slideshow/camera.py`

This is the largest change. We replace the on-demand render logic in `async_camera_image` with a background task (`_render_loop`) that pre-renders slides into `_framebuffer`. PIL calls move to executor threads via `hass.async_add_executor_job`. `camera.py` now imports from `image_processing`.

- [ ] **Step 1: Add `image_processing` import to `camera.py`**

At the top of `camera.py`, add:

```python
from . import image_processing as ip
```

- [ ] **Step 2: Add new instance variables to `__init__`**

In `AlbumSlideshowCamera.__init__`, add after the existing instance variable declarations:

```python
        self._framebuffer: bytes | None = None
        self._interrupt_event: asyncio.Event = asyncio.Event()
        self._force_next: bool = False
        self._consecutive_failures: int = 0
        self._render_task: asyncio.Task | None = None
```

Also add `asyncio` to the imports at the top of `camera.py` if not already present:

```python
import asyncio
```

Remove the `_render_cache` variable from `__init__` (it is replaced by `_framebuffer`):

Remove this line:
```python
        self._render_cache: _BytesCache | None = None
```

- [ ] **Step 3: Update `_on_store_change` to use interrupt event**

In `__init__`, update the `_on_store_change` closure:

```python
        def _on_store_change() -> None:
            self._download_cache.resize(self.store.image_cache_mb * 1024 * 1024)
            self._interrupt_event.set()
            self.async_write_ha_state()
```

Also update the coordinator listener to trigger a rerender when new album data arrives. Replace:

```python
        coordinator.async_add_listener(self.async_write_ha_state)
```

with:

```python
        def _on_coordinator_update() -> None:
            self._interrupt_event.set()
            self.async_write_ha_state()

        coordinator.async_add_listener(_on_coordinator_update)
```

- [ ] **Step 4: Add `async_added_to_hass` and `async_will_remove_from_hass`**

Add these methods to `AlbumSlideshowCamera`:

```python
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._render_task = self.hass.loop.create_task(self._render_loop())

    async def async_will_remove_from_hass(self) -> None:
        if self._render_task is not None:
            self._render_task.cancel()
            try:
                await self._render_task
            except asyncio.CancelledError:
                pass
```

- [ ] **Step 5: Simplify `async_camera_image`**

Replace the entire existing `async_camera_image` method with:

```python
    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        return self._framebuffer
```

- [ ] **Step 6: Update `async_force_next`**

Replace the existing `async_force_next` method with:

```python
    async def async_force_next(self) -> None:
        self._force_next = True
        self._interrupt_event.set()
        self.async_write_ha_state()
```

- [ ] **Step 7: Update `async_force_refresh`**

Replace the existing `async_force_refresh` method with:

```python
    async def async_force_refresh(self) -> None:
        await self.coordinator.async_request_refresh()
```

(The coordinator update listener will fire after the refresh and trigger a rerender via `_interrupt_event`.)

- [ ] **Step 8: Add `_render_loop`**

Add this method to `AlbumSlideshowCamera`:

```python
    async def _render_loop(self) -> None:
        """Background task: render slides into _framebuffer, advance on timer or interrupt."""
        should_advance = False  # Don't advance on the very first render
        while True:
            try:
                await self._render_cycle(advance=should_advance)
                self._consecutive_failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._consecutive_failures += 1
                backoff = min(2 ** self._consecutive_failures, 60)
                _LOGGER.warning(
                    "Render cycle failed (attempt %d), retrying in %ds: %s",
                    self._consecutive_failures, backoff, err,
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                should_advance = True  # Skip the broken image on retry
                continue

            # Wait for slide_interval, or wake early on interrupt
            self._interrupt_event.clear()
            interval = int(self.store.slide_interval)
            try:
                await asyncio.wait_for(self._interrupt_event.wait(), timeout=float(interval))
                # Woken by interrupt: advance only if force_next was set
                should_advance = self._force_next
                self._force_next = False
            except asyncio.TimeoutError:
                # Normal timer expiry: advance to next slide
                should_advance = True
```

- [ ] **Step 9: Add `_render_cycle`**

Add this method to `AlbumSlideshowCamera`:

```python
    async def _render_cycle(self, advance: bool) -> None:
        """Fetch, render, and store one frame into _framebuffer."""
        data = self.coordinator.data or {}
        items: list[MediaItem] = data.get("items", [])
        if not items:
            return

        count = len(items)
        if advance:
            self._do_advance(count)

        out = await self._render_current(items)
        if out is not None:
            self._framebuffer = out
            self.async_write_ha_state()
```

- [ ] **Step 10: Add `_do_advance` (replaces the advance logic from old `_advance`)**

Add this method to `AlbumSlideshowCamera`:

```python
    def _do_advance(self, count: int) -> None:
        """Advance _index to the next slide."""
        if count <= 0:
            self._index = 0
            return

        self._index %= count
        order_mode = self.store.order_mode

        if order_mode == ORDER_ALBUM:
            self._index = (self._index + 1) % count
            return

        self._index = self._next_random_index(count)
        cur_url = self.coordinator.data["items"][self._index].url
        self._recent_urls.append(cur_url)
        keep = min(20, max(1, count - 1))
        if len(self._recent_urls) > keep:
            self._recent_urls = self._recent_urls[-keep:]
```

- [ ] **Step 11: Add `_render_current`**

Add this method to `AlbumSlideshowCamera`:

```python
    async def _render_current(self, items: list[MediaItem]) -> bytes | None:
        """Render the image at self._index and return encoded bytes."""
        fill_mode = self.store.fill_mode
        portrait_mode = self.store.portrait_mode
        divider = max(0, int(self.store.pair_divider_px))
        divider_fill, transparent_divider = ip.parse_divider_color(self.store.pair_divider_color)
        width, height = ip.resolve_output_size(None, None, self.store.aspect_ratio)

        cur = items[self._index]
        cur_bytes = await self._fetch_bytes(cur.url)
        if not cur_bytes:
            raise RuntimeError(f"Failed to fetch image: {cur.url}")

        img = await self.hass.async_add_executor_job(ip.open_image, cur_bytes)
        cur_is_portrait = ip.is_portrait_item(cur, img)
        self._last_is_portrait = cur_is_portrait

        is_portrait_canvas = height > width
        orientation_mismatch = cur_is_portrait != is_portrait_canvas

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_AVOID:
            return await self._skip_mismatch_and_render(items, width, height, fill_mode, is_portrait_canvas)

        if orientation_mismatch and portrait_mode == ORIENTATION_MISMATCH_PAIR:
            other = await self._find_next_mismatch_image(items, is_portrait_canvas, limit=12)
            if other:
                composed = await self.hass.async_add_executor_job(
                    ip.pair_images, img, other, width, height, fill_mode,
                    is_portrait_canvas, divider, divider_fill, transparent_divider,
                )
            else:
                composed = await self.hass.async_add_executor_job(
                    ip.render_image, img, fill_mode, width, height,
                )
            return await self.hass.async_add_executor_job(ip.encode_image, composed)

        composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
        return await self.hass.async_add_executor_job(ip.encode_image, composed)
```

- [ ] **Step 12: Update `_skip_mismatch_and_render` to use executor jobs**

Replace the existing `_skip_mismatch_and_render` method with:

```python
    async def _skip_mismatch_and_render(
        self,
        items: list[MediaItem],
        width: int,
        height: int,
        fill_mode: str,
        is_portrait_canvas: bool,
    ) -> bytes | None:
        count = len(items)
        if count <= 0:
            return None

        start = self._index
        for _ in range(min(count, 30)):
            cur = items[self._index]
            b = await self._fetch_bytes(cur.url)
            if not b:
                self._do_advance(count)
                continue
            img = await self.hass.async_add_executor_job(ip.open_image, b)
            if ip.is_portrait_item(cur, img) != is_portrait_canvas:
                self._do_advance(count)
                continue

            self._last_is_portrait = is_portrait_canvas
            composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
            return await self.hass.async_add_executor_job(ip.encode_image, composed)

        self._index = start
        cur = items[self._index]
        b = await self._fetch_bytes(cur.url)
        if not b:
            return None
        img = await self.hass.async_add_executor_job(ip.open_image, b)
        self._last_is_portrait = ip.is_portrait_item(cur, img)
        composed = await self.hass.async_add_executor_job(ip.render_image, img, fill_mode, width, height)
        return await self.hass.async_add_executor_job(ip.encode_image, composed)
```

- [ ] **Step 13: Update `_find_next_mismatch_image` to use executor jobs**

Replace the existing `_find_next_mismatch_image` method with:

```python
    async def _find_next_mismatch_image(
        self,
        items: list[MediaItem],
        is_portrait_canvas: bool,
        limit: int = 10,
    ) -> Image.Image | None:
        if not items:
            return None
        n = len(items)
        tries = 0
        offset = 1
        while tries < limit and offset < n:
            idx = (self._index + offset) % n
            it = items[idx]
            offset += 1
            tries += 1

            if it.url in self._recent_urls:
                continue

            b = await self._fetch_bytes(it.url)
            if not b:
                continue

            try:
                img = await self.hass.async_add_executor_job(ip.open_image, b)
            except Exception:
                continue

            if ip.is_portrait_item(it, img) != is_portrait_canvas:
                return img

        return None
```

- [ ] **Step 14: Remove the old `_advance` method**

Delete the old `_advance` method from `AlbumSlideshowCamera` (it is replaced by `_do_advance`). The method starts at:

```python
    def _advance(self, count: int, force: bool) -> None:
```

- [ ] **Step 15: Remove dead PIL imports and dead code from `camera.py`**

Replace the PIL import line:

```python
from PIL import Image, ImageColor, ImageFilter, ImageOps
```

with just:

```python
from PIL import Image
```

(`Image` is still needed for the `Image.Image` return type annotation in `_find_next_mismatch_image`. `ImageColor`, `ImageFilter`, `ImageOps` are no longer used in `camera.py`.)

Remove these now-dead module-level functions from `camera.py` (all logic has moved to `image_processing.py`):
- `_open_image`
- `_is_portrait_img`
- `_is_portrait_dims`
- `_is_portrait_item`
- `_parse_aspect_ratio`
- `_resolve_output_size`
- `_resize_cover`
- `_resize_contain`
- `_blur_fill`
- `_render_by_fill_mode`
- `_pair_images`
- `_parse_divider_color`

Remove the `_encode_image` **instance method** from `AlbumSlideshowCamera` (it is now `ip.encode_image`).

- [ ] **Step 16: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 17: Verify no syntax errors in the modified camera.py**

```bash
python -c "from custom_components.album_slideshow import camera; print('OK')"
```

Expected: `OK`

- [ ] **Step 18: Commit**

```bash
git add custom_components/album_slideshow/camera.py custom_components/album_slideshow/image_processing.py
git commit -m "fix: move PIL rendering to executor threads via background framebuffer task"
```

---

## Task 7: Final verification and push

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS, no warnings.

- [ ] **Step 2: Check for any remaining references to deleted functions**

```bash
grep -r "_render_cache\|_open_image\|_is_portrait_img\|_resize_cover\|_blur_fill\|_render_by_fill_mode\|_pair_images\|_parse_divider_color\|_encode_image\|_advance(" custom_components/
```

Expected: no output (all deleted/renamed).

- [ ] **Step 3: Push the branch**

```bash
git push -u origin fix/performance-event-loop
```

Expected: branch pushed to `cdmicacc/album_slideshow`.
