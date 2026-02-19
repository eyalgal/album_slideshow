# 📸 Album Slideshow Camera for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/eyalgal/album_slideshow)](https://github.com/eyalgal/album_slideshow/releases)
[![Buy Me A Coffee](https://img.shields.io/badge/buy_me_a-coffee-yellow)](https://www.buymeacoffee.com/eyalgal)

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
| Number | Slide interval | 30 | Any positive integer (seconds) | Time between slides |
| Number | Album refresh | 6 | Any positive integer (hours) | How often album contents refresh |
| Number | Pair divider size | 8 | 0 or positive integer (px) | Width of divider between paired images |
| Select | Fill mode | blur | blur, cover, contain | How images fill the canvas |
| Select | Orientation mismatch | pair | pair, single, avoid | Handling of portrait and landscape mismatch |
| Select | Order mode | shuffle | shuffle, album | Slide ordering behavior |
| Select | Aspect ratio | 16:9 | 16:9, 4:3, 1:1, 9:16, and more | Canvas aspect ratio |
| Text | Pair divider color | #FFFFFF | Hex, named colors, transparent | Divider color between paired images |

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

- Shared albums typically return only about 300 photos  
- This is an upstream API limitation  
- Videos are skipped  
- Internet connection required  

### General

- Images only  
- No video support  

---

## ❤️ Support

If you enjoy this card and want to support its development:

<a href="https://coff.ee/eyalgal" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="60">
</a>
