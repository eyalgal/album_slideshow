# Performance Fix Design

**Date:** 2026-04-19
**Branch:** to be created

## Problem

The integration causes Home Assistant to lag and become unresponsive immediately after installation. The root cause is that all PIL image processing runs synchronously on the HA async event loop, blocking it for seconds at a time. Secondary issues compound this: default 4K output, blur fill mode, a 2-second render cache, and a large download cache consume CPU and memory on 
constrained devices.

## Goals

- Move all CPU-bound image processing off the event loop
- Serve camera images instantly (never block waiting for a render)
- Cache rendered images intelligently (invalidate on slide change, not on a short TTL)
- Bound the download cache by memory in MB not image sizes, user-configurable
- Degrade gracefully when images fail; resume automatically when they recover

## Out of Scope

- Changing configurable defaults (resolution, blur fill mode, slide interval)
- Pre-fetching or parallelising image downloads
- On-disk caching

---

## Architecture

Three files change; one new file is created. `coordinator.py` is unchanged.

```
image_processing.py   (new)  — pure synchronous PIL functions, no HA dependencies
camera.py             (mod)  — background render task + framebuffer; async_camera_image is a fast read
store.py              (mod)  — adds image_cache_mb setting
config_flow.py        (mod)  — exposes image_cache_mb in Options flow
const.py              (mod)  — adds CONF_IMAGE_CACHE_MB, DEFAULT_IMAGE_CACHE_MB
```

---

## image_processing.py (new)

Pure synchronous functions extracted verbatim from `camera.py`. No async code, no HA imports.

**Public API:**
- `open_image(data: bytes) -> Image.Image` — EXIF transpose + mode normalisation
- `is_portrait_img(img) -> bool`
- `is_portrait_item(item, img) -> bool`
- `render_image(img, fill_mode, width, height) -> Image.Image` — dispatches to cover/contain/blur
- `pair_images(img1, img2, target_w, target_h, fill_mode, portrait_canvas, divider, divider_fill, transparent_divider) -> Image.Image`
- `encode_image(img) -> bytes` — JPEG or PNG

**Private helpers** (unchanged from today):
`_resize_cover`, `_resize_contain`, `_blur_fill`, `_parse_divider_color`, `_resolve_output_size`, `_parse_aspect_ratio`

These functions have no side effects and no HA dependencies. They can be called from any thread.

---

## camera.py — Background Render Task (framebuffer model)

### Core idea

A long-running background coroutine (`_render_loop`) owns all rendering. `async_camera_image` only reads from a framebuffer — it never renders anything.

```
_render_loop (background task)
  └─ fetch bytes         (async network/disk I/O)
  └─ open_image          (executor thread — PIL)
  └─ render_image        (executor thread — PIL)
  └─ encode_image        (executor thread — PIL)
  └─ _framebuffer = out  (atomic assignment)
  └─ wait slide_interval (interruptible)

async_camera_image
  └─ return self._framebuffer   (instant)
```

### `_render_loop` lifecycle

- Started in `async_added_to_hass`, cancelled in `async_will_remove_from_hass`
- Never exits voluntarily; exceptions are caught, logged, and the loop continues
- Uses `asyncio.wait_for(self._interrupt_event.wait(), timeout=slide_interval)` to sleep between slides — this makes the sleep interruptible

### Interrupt event (`_interrupt_event: asyncio.Event`)

Two callers set this event to wake the loop early:

| Caller | Action |
|--------|--------|
| `async_force_next` | Sets `_force_next = True`, then sets the event |
| `_on_store_change` | Sets the event (leaves `_force_next` unchanged) |

On wakeup the loop always does the same thing: **advance the index if `_force_next` is True (then clear the flag), then render the current slide.** Settings changes are picked up automatically because the loop reads from the store at render time — no special handling required. If a settings change and a force-next arrive before the loop wakes, the result is correct: the index advances and the new settings are applied to the new slide.

### Sequencing

All steps within a single render cycle are sequential (each `await` completes before the next begins):

```python
cur_bytes = await self._fetch_bytes(cur.url)                               # async I/O
img       = await hass.async_add_executor_job(open_image, cur_bytes)       # CPU thread
composed  = await hass.async_add_executor_job(render_image, img, ...)      # CPU thread
out       = await hass.async_add_executor_job(encode_image, composed)      # CPU thread
self._framebuffer = out                                                    # atomic
```

Portrait mismatch handling (`_skip_mismatch_and_render`, `_find_next_mismatch_image`) also runs inside `_render_loop` with the same sequential `await` pattern — each fetch + open completes before the next begins, as today.

### Framebuffer

`_framebuffer: bytes | None` — initialised to `None`. `async_camera_image` returns it directly:

```python
async def async_camera_image(self, width, height):
    return self._framebuffer
```

No TTL, no lock, no PIL work. Width/height parameters are ignored — the background task renders at the configured aspect ratio and resolution as it does today. (This is unchanged behaviour.)

---

## Download Cache

Unchanged eviction strategy (oldest-first), but the cap changes from entry count to total bytes.

**Budget:** Configured by the user as `image_cache_mb` (see Config Flow section). Default: 150 MB. This is sized for the Google Photos use case (1–2 MB per image, up to 31 lookahead images in avoid mode ≈ 50 MB peak). Local file re-reads are cheap executor jobs and do not need to be cached aggressively.

**Tracking:** `_cache_bytes: int` is maintained incrementally — incremented when an image is added, decremented when entries are evicted. No full scan on every access.

**Eviction:** When `_cache_bytes` would exceed the budget after adding a new entry, evict oldest entries until there is room.

---

## Error Handling

Inside `_render_loop`, each render attempt is wrapped in try/except:

- On failure: log a warning, keep the existing framebuffer (stale image beats blank), advance to the next slide
- Track `_consecutive_failures: int`
- Before each retry: `await asyncio.sleep(min(2 ** _consecutive_failures, 60))`
- On success: reset `_consecutive_failures = 0`

This means a fully broken album backs off to retrying every 60 seconds. A single bad image in an otherwise healthy album is skipped with no backoff (counter resets immediately on the next success).

The loop never exits — if the internet comes back or images are fixed, the next successful render automatically resumes normal operation.

---

## Config Flow Changes

### const.py
```python
CONF_IMAGE_CACHE_MB = "image_cache_mb"
DEFAULT_IMAGE_CACHE_MB = 150
```

### store.py
`image_cache_mb` added alongside existing settings (`slide_interval`, `fill_mode`, etc.). Read/written through the same store listener pattern.

### config_flow.py
`image_cache_mb` added to the **Options flow only** (not initial setup), as a `NumberSelector` with `min=50, max=1000, step=50, unit_of_measurement="MB"`, default 150. Appears alongside the existing interval/fill/pairing controls.

---

## Files Changed Summary

| File | Change |
|------|--------|
| `image_processing.py` | New — all PIL functions |
| `camera.py` | Background render loop, framebuffer, executor calls, updated download cache |
| `store.py` | Add `image_cache_mb` |
| `config_flow.py` | Add `image_cache_mb` to Options flow |
| `const.py` | Add `CONF_IMAGE_CACHE_MB`, `DEFAULT_IMAGE_CACHE_MB` |
