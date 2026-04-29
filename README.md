# 📸 Album Slideshow Camera for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/eyalgal/album_slideshow)](https://github.com/eyalgal/album_slideshow/releases)
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

### 🗓 Filter & Order by Date
- Date filter: last 7 / 30 / 365 days, this month, this year, **On this day** memories, or a custom range
- Order modes: random, album order, **newest taken**, **oldest taken**, **newest added**, **oldest added**
- Capture date and upload date exposed as camera attributes (with paired-photo support)

### ⏯ Pause / Resume
- Pause switch holds the current slide indefinitely
- Manual "Next slide" button still works while paused
- Survives Home Assistant restarts

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
| Number | Image cache size | 150 | 50–1000 (MB) | Memory budget for downloaded image data |
| Select | Fill mode | blur | blur, cover, contain | How images fill the canvas |
| Select | Orientation mismatch | pair | pair, single, avoid | Handling of portrait and landscape mismatch |
| Select | Order mode | random | random, album_order, newest_taken, oldest_taken, newest_added, oldest_added | Slide ordering behavior |
| Select | Aspect ratio | 16:9 | 16:9, 4:3, 1:1, 9:16, and more | Canvas aspect ratio |
| Select | Max resolution | 4K (2160p) | 480p, 720p, 1080p, 1440p, 4K (2160p), original | Cap output resolution by short edge; use original to render at native size |
| Select | Date filter | off | off, last_7_days, last_30_days, last_365_days, this_month, this_year, on_this_day, custom_range | Restrict the slideshow to a date window based on photo capture date |
| Text | Date filter (from) | empty | YYYY-MM-DD or empty | Start of custom date range (inclusive) |
| Text | Date filter (to) | empty | YYYY-MM-DD or empty | End of custom date range (inclusive) |
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

| Entity | Description |
|--------|------------|
| Album title | Title of the source album |
| Media count | Number of images currently available |
| Image cache usage *(diagnostic)* | Current download cache size in MB |

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
| `captured_at` | string \| list \| null | ISO-8601 capture date. List of `[primary, partner]` when paired (top/left first) |
| `captured_at_primary` | string \| null | Capture date of the primary image only |
| `uploaded_at` | string \| null | ISO-8601 date when added to the album (Google Photos only) |
| `byte_size` | int \| null | Original file size in bytes (Google Photos only) |
| `paused` | bool | Whether the slideshow is paused |
| `date_filter` | string | Active date filter mode |

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
