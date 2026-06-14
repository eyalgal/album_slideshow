# 📸 Album Slideshow Camera for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/eyalgal/album_slideshow)](https://github.com/eyalgal/album_slideshow/releases)
[![GitHub Downloads](https://img.shields.io/github/downloads/eyalgal/album_slideshow/total.svg)](https://github.com/eyalgal/album_slideshow/releases)
[![Community Forum](https://img.shields.io/badge/Community-Forum-5294E2.svg)](https://community.home-assistant.io/t/album-slideshow-google-photos-local/996986)
[![Buy Me A Coffee](https://img.shields.io/badge/buy_me_a-coffee-yellow)](https://www.buymeacoffee.com/eyalgal)

<img width="800" alt="banner" src="https://github.com/user-attachments/assets/591b3541-5e2a-43d0-a97a-145f365cff94" />

Turn a **Google Photos shared album** or **local/NAS folder** into a fully controllable Home Assistant camera slideshow.

Clean. Flexible. Fully runtime configurable. Designed for dashboards.

---

## ✨ What This Integration Does

Album Slideshow creates a **camera entity** that automatically cycles through images from:

- Google Photos shared albums  
- Local folders  
- NAS mounted directories  

All behavior is exposed as Home Assistant entities. Adjust everything live without YAML edits or restarts.

---

## 🚀 Key Features

### 📷 Slideshow Camera
- Auto advancing camera entity
- Configurable slide interval
- Manual next slide button
- Album refresh control

### 🖼 Image Sources
- Google Photos shared albums
- Local folder paths
- NAS mounted directories
- Optional recursive scanning

### 📍 EXIF & Location (local / NAS)
- Reads EXIF capture date (and `OffsetTimeOriginal` when present) so date-filter modes work for local files too
- Surfaces EXIF GPS as `latitude` / `longitude` camera attributes
- Optional reverse geocoding via OpenStreetMap Nominatim for a human-readable `location` (e.g. `"Lisbon, Portugal"`); per-album opt-out in the integration's Configure dialog

### 🗓 Filter & Order by Date
- Date filter: last 7 / 30 / 365 days, this month, this year, **On this day** memories
- Order modes: random, album order, **newest taken**, **oldest taken**, **newest added**, **oldest added**
- Capture date and upload date exposed as camera attributes (with paired-photo support)

### ⏯ Pause / Resume
- Pause switch holds the current slide indefinitely
- Manual "Next slide" button still works while paused
- Survives Home Assistant restarts

### ✨ Transitions
- Smooth slide transitions rendered in the browser, so they stay buttery even on lower-end hardware
- Effects: `random`, `none`, `fade`, `slide-left`, `slide-right`, `slide-up`, `slide-down`, `wipe-left`, `wipe-right`, `zoom`
- `random` picks a different effect per slide (and avoids repeating the previous one)
- Configurable duration and CSS easing
- Aspect ratio + fill mode inheritance from the camera entity (cover / contain / blur backdrop)

### 🎨 Smart Rendering Engine

#### Orientation Mismatch Handling

| Mode | Behavior |
|------|----------|
| **Pair** | Display two mismatched images side by side |
| **Single** | Render single image using selected fill mode |
| **Avoid** | Skip mismatched images |

#### Fill Modes

| Mode | Behavior |
|------|----------|
| **Blur** | Image over blurred background |
| **Cover** | Crop to fill canvas |
| **Contain** | Fit inside canvas with bars |

#### Layout Options
- Configurable aspect ratio such as 16:9, 4:3, 1:1, 9:16
- Shuffle or album order
- Pair divider size/color control

---

## 🎛 Runtime Configuration

The following entities allow you to adjust slideshow behavior without restarting Home Assistant.

| Entity Type | Name | Default | Accepted Values | Description |
|-------------|------|---------|----------------|-------------|
| Number | Slide interval | 60 | Any positive integer (seconds) | Time between slides |
| Number | Album refresh | 24 | Any positive integer (hours) | How often album contents refresh |
| Number | Pair divider size | 8 | 0-64 (px) | Width of divider between paired images |
| Number | Image cache size | 75 | 50-1000 (MB) | Memory budget for downloaded image data (per album) |
| Select | Fill mode | blur | blur, cover, contain | How images fill the canvas |
| Select | Orientation mismatch | pair | pair, single, avoid | Handling of portrait and landscape mismatch |
| Select | Order mode | random | random, album_order, newest_taken, oldest_taken, newest_added, oldest_added | Slide ordering behavior |
| Select | Aspect ratio | 16:9 | 16:9, 4:3, 1:1, 9:16, and more | Canvas aspect ratio |
| Select | Max resolution | 4K (2160p) | 480p, 720p, 1080p, 1440p, 4K (2160p), original | Cap output resolution by short edge; use original to render at native size |
| Select | Date filter | off | off, last_7_days, last_30_days, last_365_days, this_month, this_year, on_this_day | Restrict the slideshow to a date window based on photo capture date |
| Text | Pair divider color | #FFFFFF | Hex, named colors, transparent | Divider color between paired images |
| Switch | Pause slideshow | off | on / off | Hold the current frame; advances pause until turned off |

---

## 📦 Installation

### HACS (recommended)

Album Slideshow Camera is available in **HACS**.

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eyalgal&repository=album_slideshow)


### Manual Installation

1. Download the latest release  
2. Copy  
```
custom_components/album_slideshow
```
into
```
config/custom_components/
```

3. Restart Home Assistant  
4. Add the integration from **Devices & services**

---

## ⚙️ Setup Guide

### Google Photos

1. Open a shared Google Photos album  
2. Copy the shared link such as `https://photos.app.goo.gl/...`  
3. Add the integration  
4. Paste the link  

---

### Local Folder or NAS

Use any folder accessible to Home Assistant.

Helpful path mappings:

| Input | Resolves To |
|-------|------------|
| `/local/...` | `/config/www/...` |
| `media/...` | `/media/...` |
| `media/local/...` | `/media/...` |

For NAS:
- Mount it first
- Use the mounted path

#### 📍 EXIF capture date & location (local / NAS only)

For local-folder entries the integration reads EXIF metadata in the
background after every refresh:

- **Capture date** — `DateTimeOriginal` is preferred; if missing, the file's
  modification time is used so date-based ordering still works for
  screenshots and scans. When `OffsetTimeOriginal` is present (most modern
  cameras and phones) it is honoured; otherwise the timestamp is interpreted
  as the host's local time.
- **GPS coordinates** — `GPSLatitude`/`GPSLongitude` are exposed as the
  `latitude` / `longitude` camera attributes. `(0, 0)` "null island" stamps
  are ignored.
- **Reverse-geocoded location** — by default the integration calls the
  public [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap)
  service to translate coordinates into a human-readable label such as
  `"Lisbon, Portugal"`, exposed as the `location` attribute. Coordinates
  are rounded to **~100 m** before lookup and the answer is cached on disk,
  so the same neighbourhood is only ever fetched once. Nominatim's
  free-tier policy (1 req/sec, identifying User-Agent) is respected.

**Privacy / opt-out:** if you'd rather not send any coordinates to
OpenStreetMap, open *Settings → Devices & Services → Album Slideshow → your
album → Configure* and turn off **Reverse-geocode EXIF GPS coordinates**.
The `latitude`/`longitude` attributes still work; only the `location`
label is suppressed. The opt-out is per-album.

Progress for both phases is exposed as the **Enrichment progress**
diagnostic sensor (percent complete, with `phase`, `exif_done`,
`geocode_done` etc. as attributes).

---

## 🧩 Entities Created

Each album you configure creates the following entities in Home Assistant.

---

### 📷 Camera

| Entity | Description |
|--------|------------|
| Slideshow camera | The live slideshow feed rendered according to your current settings |

---

### 🔘 Buttons

| Entity | Description |
|--------|------------|
| Next slide | Immediately advances to the next image |
| Refresh album | Re-fetches album contents |

---

### 📊 Sensors

| Entity | Source | Description |
|--------|--------|-------------|
| Album title | All | Title of the source album |
| Media count | All | Number of images currently available |
| Image cache usage *(diagnostic)* | All | Current download cache size in MB |
| Enrichment progress *(diagnostic)* | Local folder | Percent of files whose EXIF/GPS has been read (and, when enabled, reverse-geocoded). Attributes include `phase`, `exif_done`/`exif_total`, `geocode_done`/`geocode_total`. |

---

### 📋 Camera Attributes

The slideshow camera exposes per-frame metadata as attributes (use with `state_attr('camera.x', '<name>')` in templates):

| Attribute | Type | Description |
|-----------|------|-------------|
| `album_title` | string | Title of the source album |
| `media_count` | int | Photos in the active playlist (after date filter) |
| `media_count_total` | int | Total photos available before filtering |
| `current_index` | int | Index of the current slide |
| `current_filename` | string \| null | Source filename when known |
| `current_url` | string \| null | URL of the current slide |
| `current_is_portrait` | bool \| null | Orientation of the current slide |
| `captured_at` | string \| list \| null | ISO-8601 capture date. List of `[primary, partner]` when paired (top/left first). For local files this is read from EXIF (or the file's mtime as a fallback). |
| `captured_at_primary` | string \| null | Capture date of the primary image only |
| `uploaded_at` | string \| null | ISO-8601 date when added to the album (Google Photos only) |
| `byte_size` | int \| null | Original file size in bytes (Google Photos only) |
| `latitude` | float \| null | EXIF GPS latitude in decimal degrees (local folder only) |
| `longitude` | float \| null | EXIF GPS longitude in decimal degrees (local folder only) |
| `location` | string \| null | Reverse-geocoded label (e.g. `"Lisbon, Portugal"`). Empty when reverse-geocoding is disabled or has not yet completed for this file. |
| `caption_frames` | list | Structured per-image caption metadata: one entry for a normal slide, two (top/left first) for a pair. Each entry has `captured_at`, `location`, `latitude`, `longitude`. Used by the card's caption overlay. |
| `pair_orientation` | string \| null | How a paired slide is split: `horizontal` (left/right) or `vertical` (top/bottom). `null` for single slides. |
| `paused` | bool | Whether the slideshow is paused |
| `date_filter` | string | Active date filter mode |
| `frame_id` | int | Monotonic counter incremented on every committed slide. Used by the [card](#-album-slideshow-card) to detect new frames |

---

## 🎞 Album Slideshow Card

The integration ships with a custom Lovelace card that does the slide-to-slide transition entirely in the browser. The server only renders one still per slide change; the card cross-fades in CSS, which the browser composites on the GPU. Result: a smooth dissolve on a Pi 4, even with several albums on screen.

The card is registered automatically when the integration loads; you do **not** need to add it as a HACS frontend repository or configure a Lovelace resource manually. After installing or upgrading, hard-refresh the dashboard once (Ctrl+Shift+R) so the browser picks up the script.

A visual editor is available - pick **Album Slideshow** from the card picker in Lovelace and the form will appear automatically.

### Minimal example

```yaml
type: custom:album-slideshow-card
entity: camera.album_slideshow_living_room
```

### Full options

```yaml
type: custom:album-slideshow-card
entity: camera.album_slideshow_living_room
transition: random          # random | none | fade | slide-left
                            #   | slide-right | slide-up | slide-down
                            #   | wipe-left | wipe-right | zoom
duration: 800               # ms; CSS transition length
easing: ease-in-out         # any CSS timing function (ease, linear, cubic-bezier(...))
aspect_ratio: 16/9          # CSS aspect-ratio value (16/9, 4/3, 1/1, auto)
fit: auto                   # auto | cover | contain
                            # auto inherits the camera's fill_mode (cover / contain / blur)
background: '#000'          # color shown behind contained images
tap_action: none            # none | more-info
caption:                    # overlay the photo's date and/or location
  show: [date, location]    #   any of: date, location (order = display order)
  position: bottom-left     #   top/center/bottom + -left/-center/-right, or center
  date_format: medium       #   medium | full | month_year | year | numeric
                            #     | weekday | relative, or a custom token string
                            #     (YYYY, MMMM, MMM, MM, M, DD, D, dddd, ddd)
  layout: stacked           #   stacked (one per line) | inline (same line)
  per_image: true           #   caption each half of a portrait pair separately
  color: '#ffffff'          #   any CSS color
  font_size: 14px           #   any CSS size
  shadow: true              #   drop shadow for readability on bright photos
```

### Notes

- `transition: random` picks a different effect per slide and avoids repeating the previous one.
- `fit: auto` reads the camera's `fill_mode` attribute. `blur` renders the slide as `contain` plus a blurred backdrop layer behind it.
- **Caption overlay:** omit the `caption:` block (or set `show: []`) to disable it. The date comes from `captured_at` (EXIF or file mtime); `location` comes from EXIF reverse-geocoding and is simply skipped when a photo has none, so Google Photos slides show only the date. On a portrait pair, `per_image: true` anchors each photo's own date/location to its half; set it to `false` for a single caption over the whole frame.
- `date_format` accepts a preset name or a custom token string. Presets are locale-aware (they follow your Home Assistant language). Example custom format: `'D MMMM YYYY'` -> `29 July 2023`.
- Every slide commit increments the camera's `frame_id` attribute. The card cache-busts the camera proxy URL with that value, so the browser refetches a fresh JPEG on every change instead of serving a stale cached image.
- If the entity is unavailable, the card shows a "Camera not ready" placeholder.

---

## 🎨 Transparent Divider

To remove visible spacing between paired images:

1. Set **Pair divider color** to `transparent`
2. Keep divider size greater than `0`

Also accepted values:
- `none`
- `clear`
- `rgba(0,0,0,0)`
- `transperant` common misspelling

When transparency is used, the integration outputs PNG to preserve alpha.

---

## ⚠️ Limitations

### Google Photos

- Public shared albums only (link sharing must be enabled)
- Up to 20,000 photos per album
- Videos are skipped
- Internet connection required
- Relies on Google's public web endpoints; if Google changes them, the integration falls back to a 300-photo limit until the scraper is updated
- The last successful album fetch is cached to disk; if a refresh fails or returns no photos, the slideshow keeps running with the cached list

### General

- Images only  
- No video support  

---

## ❤️ Support

If you enjoy this card and want to support its development:

<a href="https://coff.ee/eyalgal" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="60">
</a>
