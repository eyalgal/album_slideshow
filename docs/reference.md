# Reference

[Overview](../README.md) | [Provider Setup](provider-setup.md) | [Card Guide](card-guide.md)

- [Runtime configuration](#runtime-configuration)
- [Rendering options](#rendering-options)
- [Pause and navigation](#pause-and-navigation)
- [Entities created](#entities-created)
- [Camera attributes](#camera-attributes)
- [Automation actions](#automation-actions)

## Runtime Configuration

The following entities allow you to adjust slideshow behavior without restarting Home Assistant.

| Entity Type | Name | Default | Accepted Values | Description |
|-------------|------|---------|----------------|-------------|
| Number | Slide interval | 60 | Any positive integer (seconds) | Time between slides |
| Number | Album refresh | 24 | Any positive integer (hours) | How often album contents refresh |
| Number | Pair divider size | 8 | 0-64 (px) | Width of divider between paired images |
| Number | Pair minimum gap | 0 (off) | 0-50 (% of album) | Above 0, shuffles pairing candidates outside a circular index gap, capped to keep candidates available. Helps avoid nearby photos but does not detect photo sessions. At 0, keeps the original nearest-candidate search |
| Number | Navigation buffer | 2 | 0-10 (slides) | Fully rendered slides cached before and after the current frame for immediate Previous/Next navigation |
| Number | Image cache size | 75 | 50-1000 (MB) | Memory budget for downloaded image data (per album) |
| Select | Fill mode | blur | blur, cover, contain | How images fill the canvas |
| Select | Orientation mismatch | pair | pair, single, avoid | Handling of portrait and landscape mismatch |
| Select | Order mode | random | random, album_order, newest_taken, oldest_taken, newest_added, oldest_added | Slide ordering behavior |
| Select | Aspect ratio | 16:9 | 16:9, 4:3, 1:1, 9:16, and more | Canvas aspect ratio |
| Select | Max resolution | 4K (2160p) | 480p, 720p, 1080p, 1440p, 4K (2160p), original | Cap output resolution by short edge; use original to render at native size |
| Select | Date filter | off | off, last_7_days, last_30_days, last_365_days, this_month, this_year, on_this_day | Restrict the slideshow to a date window based on photo capture date |
| Text | Pair divider color | #FFFFFF | Hex, named colors, transparent | Divider color between paired images |
| Switch | Pause slideshow | off | on / off | Hold the current frame; advances pause until turned off |
| Switch | Crop debug overlay | off | on / off | Show detected face boxes, preferred padding, and the original photo center; see [crop diagnostics](provider-setup.md#crop-debug-overlay) |

## Rendering Options

### Orientation Mismatch Handling

| Mode | Behavior |
|------|----------|
| **Pair** | Display two mismatched images side by side |
| **Single** | Render single image using selected fill mode |
| **Avoid** | Skip mismatched images |

### Fill Modes

| Mode | Behavior |
|------|----------|
| **Blur** | Image over blurred background |
| **Cover** | Crop to fill canvas |
| **Contain** | Fit inside canvas with bars |

### Layout Options

- Configurable aspect ratio such as 16:9, 4:3, 1:1, 9:16
- Shuffle or album order
- Pair divider size/color control

### Transparent Divider

To remove visible spacing between paired images:

1. Set **Pair divider color** to `transparent`
2. Keep divider size greater than `0`

Also accepted values:
- `none`
- `clear`
- `rgba(0,0,0,0)`
- `transperant` common misspelling

When transparency is used, the integration outputs PNG to preserve alpha.

## Pause and Navigation

- Pause switch holds the current slide indefinitely
- Manual "Previous slide" and "Next slide" buttons still work while paused
- Previous and Next swap pre-rendered frames for immediate navigation
- A configurable navigation buffer retains previous frames and pre-renders upcoming frames (even in random order)
- Pause state survives Home Assistant restarts

For the on-card toolbar and its temporary display hold, see the
[Card Guide](card-guide.md#hide-photos-from-a-slideshow).

## Entities Created

Each album you configure creates the following entities in Home Assistant,
along with the [runtime controls](#runtime-configuration) above.

### Camera

| Entity | Description |
|--------|------------|
| Slideshow camera | The live slideshow feed rendered according to your current settings |

### Buttons

| Entity | Description |
|--------|------------|
| Previous slide | Steps back to the previously shown image |
| Next slide | Immediately advances to the next image |
| Refresh album | Re-fetches album contents |
| Hide current photo | Excludes a single displayed photo from this slideshow; pairs require an explicit choice in the card or action |
| Undo hide | Restores the most recent hide action |

### Sensors

| Entity | Source | Description |
|--------|--------|-------------|
| Album title | All | Title of the source album |
| Media count | All | Number of images currently available |
| Hidden photos | All | Number of persisted exclusions for this slideshow |
| Image cache usage *(diagnostic)* | All | Current download cache size in MB |
| Enrichment progress *(diagnostic)* | Local folder / Immich / Nextcloud / Ente | Percent of items whose metadata has been processed (EXIF/GPS for local folder and Nextcloud, per-asset detail for Immich, reverse-geocoding for Ente). Attributes include `phase`, `exif_done`/`exif_total`, `geocode_done`/`geocode_total`. |

## Camera Attributes

The slideshow camera exposes per-frame metadata as attributes (use with `state_attr('camera.x', '<name>')` in templates):

| Attribute | Type | Description |
|-----------|------|-------------|
| `album_title` | string | Title of the source album |
| `media_count` | int | Photos in the active playlist (after date filter and exclusions) |
| `media_count_total` | int | Total photos available before filtering |
| `current_index` | int | Index of the current slide |
| `current_filename` | string \| null | Source filename when known |
| `current_url` | string \| null | URL of the current slide. For Ente this is an internal `ente://<id>` reference, since the image is decrypted locally rather than fetched from a URL |
| `current_is_portrait` | bool \| null | Orientation of the current slide |
| `captured_at` | string \| list \| null | ISO-8601 capture date. List of `[primary, partner]` when paired (top/left first). For local files this is read from EXIF (or the file's mtime as a fallback). |
| `captured_at_primary` | string \| null | Capture date of the primary image only |
| `uploaded_at` | string \| null | ISO-8601 date when added to the album (Google Photos only) |
| `byte_size` | int \| null | Original file size in bytes (Google Photos only) |
| `latitude` | float \| null | GPS latitude in decimal degrees (local folder + Immich) |
| `longitude` | float \| null | GPS longitude in decimal degrees (local folder + Immich) |
| `location` | string \| null | Reverse-geocoded label (e.g. `"Lisbon, Portugal"`). Empty when reverse-geocoding is disabled or has not yet completed for this file. |
| `description` | string \| null | Free-text photo caption. From EXIF `ImageDescription` / IPTC `Caption-Abstract` / XMP `dc:description` (local folder), or the Immich photo description (Immich provider). |
| `caption_frames` | list | Structured per-image caption metadata: one entry for a normal slide, two (top/left first) for a pair. Each entry has `captured_at`, `location`, `latitude`, `longitude`, `description`. Used by the card's caption overlay. |
| `pair_orientation` | string \| null | How a paired slide is split: `horizontal` (left/right) or `vertical` (top/bottom). `null` for single slides. |
| `paused` | bool | Whether the slideshow is paused |
| `date_filter` | string | Active date filter mode |
| `frame_id` | int | Monotonic counter incremented on every committed slide. Used by the [card](card-guide.md) to detect new frames |
| `entry_id` | string | Config entry ID for slideshow actions |
| `displayed_photo_ids` | list | Opaque IDs for the rendered photo(s), first = left/top. An unavailable ID is `null`; an empty list means no ready photo |
| `hidden_photo_count` | int | Number of persisted exclusions, including photos no longer in the source album |
| `hidden_revision` | int | Persisted exclusion-list revision; changes when exclusions change, allowing cards to discard stale frames |
| `undo_hide_available` | bool | Whether the last hide action can be undone |
| `empty_reason` | string \| null | `all_hidden` or `no_matching_photos` when the playlist is empty |
| `navigation_buffer_size` | int | Configured number of fully rendered slides retained in each direction |
| `previous_frames_cached` | int | Previous rendered frames currently available for immediate navigation |
| `next_frames_preloaded` | int | Upcoming rendered frames currently available for immediate navigation |
| `navigation_preloading` | bool | Whether the background worker is currently filling the upcoming-frame buffer |
| `last_navigation_outcome` | string \| null | Result of the latest manual action: `pending`, `displayed`, `not_available`, or `error` |
| `last_navigation_error` | string \| null | Error from the latest manual navigation attempt, when present |

## Automation Actions

Every action below requires `entry_id` in its `data`, using the camera's
`entry_id` attribute. This is the configured slideshow's ID, not its camera
entity ID.

| Action | Additional fields |
|--------|-------------------|
| `album_slideshow.previous_slide` | None; shows the previous cached frame |
| `album_slideshow.next_slide` | None; advances to the next frame, including while paused |
| `album_slideshow.refresh_album` | None; re-fetches the source album |
| `album_slideshow.hide_photo` | Optional `photo_ids` list from `displayed_photo_ids`, or `position`: `first`, `second`, `both`. Optional `frame_id` rejects a stale current-frame action. |
| `album_slideshow.undo_hide` | None |
| `album_slideshow.restore_photos` | `photo_ids` list |
| `album_slideshow.restore_all_photos` | None; makes all excluded photos eligible again |
| `album_slideshow.list_hidden_photos` | Optional `offset` (default 0) and `limit` (default 50, range 1-100); returns `photos`, `total`, and `offset` as response data |

The **Hide current photo** button handles single-photo slides. For pairs, use
the [card controls](card-guide.md#hide-photos-from-a-slideshow) or specify a
position/IDs in the action. ID-based actions target the chosen photo even if the
slideshow advances before the request arrives.

`list_hidden_photos` requires a response: set `response_variable` when calling
it from a script or automation. Each entry in `photos` contains `photo_id`,
`name` (the source filename when available, otherwise an ID-based label), and
`in_album` (whether the photo is still in the cached source album). `total` is
the full exclusion count, not the page length. Increase `offset` to fetch the
next page; pass the returned `photo_id` values to `restore_photos`.

Pause/Resume uses the existing **Pause slideshow** switch with `switch.turn_on` /
`switch.turn_off`, not a separate slideshow action.