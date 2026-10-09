<a id="-album-slideshow-camera-for-home-assistant"></a>
# Album Slideshow Camera for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/eyalgal/album_slideshow)](https://github.com/eyalgal/album_slideshow/releases)
[![GitHub Downloads](https://img.shields.io/github/downloads/eyalgal/album_slideshow/total.svg)](https://github.com/eyalgal/album_slideshow/releases)
[![Community Forum](https://img.shields.io/badge/Community-Forum-5294E2.svg)](https://community.home-assistant.io/t/album-slideshow-google-photos-local/996986)
[![Buy Me A Coffee](https://img.shields.io/badge/buy_me_a-coffee-yellow)](https://www.buymeacoffee.com/eyalgal)

<img width="800" alt="Album Slideshow dashboard preview" src="https://github.com/user-attachments/assets/591b3541-5e2a-43d0-a97a-145f365cff94" />

Turn a photo album, self-hosted library, or local/NAS folder into a Home Assistant
camera slideshow. The included dashboard card adds smooth transitions, captions,
and optional photo controls. Adjust slideshow settings live through Home
Assistant entities.

## Documentation

| Guide | What you will find |
|-------|--------------------|
| [Provider Setup](docs/provider-setup.md) | Connect your photo source, choose albums and quality, understand metadata and privacy, and troubleshoot |
| [Card Guide](docs/card-guide.md) | Add the card, use navigation and hide/restore controls, configure captions and transitions, and see full YAML options |
| [Reference](docs/reference.md) | Runtime settings, rendering options, entities, camera attributes, and automation actions |

<a id="-what-this-integration-does"></a><a id="-key-features"></a><a id="-slideshow-camera"></a>
## Features

- Automatic slideshows with adjustable timing, album refresh, and manual Previous/Next.
- Pause/resume and a configurable rendered-frame buffer for quick navigation.
- Hide and restore photos per slideshow without changing the source library.
- Optional on-demand or always-visible navigation and photo-management controls.
- Pair portrait or landscape photos, choose cover/contain/blur, and set the aspect ratio.
- Browser-side transitions and date, location, or description captions when the source provides them.
- Clock, date, and weather captions, with optional colored weather icons.
- Random or album order, capture/upload date ordering, and date filters including **On this day**.

<a id="-image-sources"></a><a id="️-setup-guide"></a>
## Supported Sources

Choose a source below for its setup instructions. The
[provider comparison](docs/provider-setup.md#choose-a-provider) shows which
sources provide dates, locations, and descriptions.

| Source | Connection |
|--------|------------|
| <a id="google-photos"></a>[Google Photos](docs/provider-setup.md#google-photos) | Shared album link |
| <a id="immich"></a><a id="choosing-what-to-show"></a><a id="image-quality"></a><a id="notes"></a>[Immich](docs/provider-setup.md#immich) | Server URL and API key; albums, people, favorites, or search |
| <a id="photoprism"></a><a id="choosing-what-to-show-1"></a><a id="image-quality-1"></a><a id="notes-1"></a>[PhotoPrism](docs/provider-setup.md#photoprism) | App password or account; albums, people, favorites, or search |
| <a id="icloud-shared-album"></a><a id="image-quality-2"></a><a id="notes-2"></a><a id="troubleshooting-slow-legacy-albums"></a>[iCloud](docs/provider-setup.md#icloud-shared-album) | Public Shared Album link |
| <a id="synology-photos"></a><a id="image-quality-3"></a><a id="notes-3"></a>[Synology Photos](docs/provider-setup.md#synology-photos) | DSM account; personal or shared library |
| <a id="nextcloud"></a><a id="authenticated-webdav-folder"></a><a id="public-album-link"></a><a id="image-quality-4"></a><a id="notes-4"></a>[Nextcloud](docs/provider-setup.md#nextcloud) | Authenticated WebDAV folder or public Photos album link |
| <a id="ente-photos"></a><a id="image-quality-5"></a><a id="self-hosted-ente"></a><a id="notes-5"></a>[Ente Photos](docs/provider-setup.md#ente-photos) | Public album link; decrypted inside Home Assistant |
| <a id="local-folder-or-nas"></a><a id="-exif-capture-date--location-local--nas-only"></a>[Local Folder / NAS](docs/provider-setup.md#local-folder-or-nas) | Folder accessible to Home Assistant |
| <a id="media-source-local-media-jellyfin-"></a><a id="how-to-find-the-media-source-id"></a><a id="️-metadata-limitation"></a>[Media Source](docs/provider-setup.md#media-source) | Home Assistant media browser, including local media and Jellyfin |
| <a id="ugreen-nas-ugos-photos"></a><a id="notes-6"></a>[UGREEN NAS (UGOS Photos)](docs/provider-setup.md#ugreen-nas-ugos-photos) | NAS account; experimental, undocumented API |

<a id="-installation"></a>
## Installation

### HACS (recommended)

1. Find **Album Slideshow** in HACS and download it.
2. Restart Home Assistant.
3. Go to **Settings > Devices & services > Add Integration**, choose
   **Album Slideshow**, and follow the setup guide for your source above.

[![Open Album Slideshow in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=eyalgal&repository=album_slideshow)

### Manual Installation

1. Download `album_slideshow.zip` from the [latest release](https://github.com/eyalgal/album_slideshow/releases/latest).
2. Extract its contents into `config/custom_components/album_slideshow/`.
   The ZIP has component files at its root: `manifest.json` must be directly
   inside that folder, not in another nested directory.
3. Restart Home Assistant and add the integration from **Devices & services**.

<a id="upgrading-to-v1110"></a>
### Updating

Update through HACS, restart Home Assistant, and reload the dashboard.
See the [release notes](https://github.com/eyalgal/album_slideshow/releases)
for version-specific upgrade instructions.

<a id="-album-slideshow-card"></a>
## Add a Dashboard Card

In the dashboard editor, add an **Album Slideshow** card and select your camera.
The card is registered automatically; no separate HACS frontend installation or
manual resource entry is needed. Hard-refresh the dashboard after an upgrade
if the new card script has not loaded.

### Minimal Example

```yaml
type: custom:album-slideshow-card
entity: camera.album_slideshow_living_room
```

Replace the example entity with your slideshow camera.

<a id="full-options"></a><a id="notes-6"></a><a id="-transitions"></a>
See the [Card Guide](docs/card-guide.md#full-options) for full YAML options,
transitions, and captions.

### Hide Photos From a Slideshow

In the card editor, choose **Interaction > Photo controls > On demand** or
**Always**. Displayed-card controls are **Off** by default.

Hide either photo in a pair or both, undo the last hide, and restore photos from
the hidden list. Exclusions persist per slideshow and never change the source
library. See the [hide/restore guide](docs/card-guide.md#hide-photos-from-a-slideshow)
for gestures, paired-photo choices, and storage safeguards.

## Settings and Automation

<a id="-runtime-configuration"></a><a id="-filter--order-by-date"></a>
- [Runtime settings](docs/reference.md#runtime-configuration): timing, ordering, date filters, resolution, and cache limits.

<a id="-smart-rendering-engine"></a><a id="orientation-mismatch-handling"></a><a id="fill-modes"></a><a id="layout-options"></a><a id="-transparent-divider"></a>
- [Rendering options](docs/reference.md#rendering-options): pairing, fill modes, aspect ratios, and transparent dividers.

<a id="-pause--resume"></a>
- [Pause and navigation](docs/reference.md#pause-and-navigation): manual navigation while paused and the rendered-frame buffer.

<a id="-entities-created"></a><a id="-camera"></a><a id="-buttons"></a><a id="-sensors"></a>
- [Entities](docs/reference.md#entities-created): camera, buttons, and diagnostic sensors.

<a id="-camera-attributes"></a>
- [Camera attributes](docs/reference.md#camera-attributes): photo metadata, exclusion state, and navigation status.

<a id="automation-actions"></a>
- [Automation actions](docs/reference.md#automation-actions): navigation, refresh, hide/undo/restore, and paginated hidden-photo lists.

<a id="️-limitations"></a><a id="general"></a>
## Limitations and Troubleshooting

- Still images only; video playback is not supported.
- <a id="-exif--location-local--nas--immich--photoprism--synology--nextcloud--ente"></a>Metadata varies by source. See [provider capabilities](docs/provider-setup.md#choose-a-provider) and the [EXIF privacy settings](docs/provider-setup.md#exif-capture-date-and-location).
- HEIC/HEIF support depends on the codecs available in your Home Assistant installation.
- <a id="google-photos-1"></a>See [Google Photos limits](docs/provider-setup.md#google-photos-limits), including the scraper fallback and cached albums.
- <a id="immich-1"></a>See [Immich requirements](docs/provider-setup.md#immich-limits) and [legacy iCloud troubleshooting](docs/provider-setup.md#troubleshooting-slow-legacy-albums).
- Renaming a local file or reimporting an asset can change its identity. See [photo identity and exclusions](docs/card-guide.md#storage-and-photo-identity).

<a id="️-support"></a>
## Support

Ask questions in the [community forum](https://community.home-assistant.io/t/album-slideshow-google-photos-local/996986)
or report problems on [GitHub Issues](https://github.com/eyalgal/album_slideshow/issues).
Include your Home Assistant version, integration version, provider, and steps
to reproduce. See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

If you find the integration useful, you can support its development:

<a href="https://coff.ee/eyalgal" target="_blank">
  <img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" alt="Buy Me A Coffee" height="60">
</a>