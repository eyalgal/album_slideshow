# Card Guide

[Overview](../README.md) | [Provider Setup](provider-setup.md) | [Reference](reference.md)

- [Add the card](#add-the-card)
- [Hide photos and use the toolbar](#hide-photos-from-a-slideshow)
- [Full YAML options](#full-options)
- [Transitions and captions](#transitions-and-captions)
- [Multiple captions and clocks](#multiple-captions-and-clocks)
- [Full-screen dashboard example](#full-screen-dashboard-example)

## Add the Card

The integration ships with a custom Lovelace card that does the slide-to-slide
transition entirely in the browser. The server renders one still per slide
change; the card handles transitions in CSS.

The card is registered automatically when the integration loads; you do **not**
need to add it as a HACS frontend repository or configure a Lovelace resource
manually. After installing or upgrading, hard-refresh the dashboard once
(Ctrl+Shift+R) so the browser picks up the script.

A visual editor is available: pick **Album Slideshow** from the card picker
in Lovelace and select your slideshow camera.

### Minimal Example

```yaml
type: custom:album-slideshow-card
entity: camera.album_slideshow_living_room
```

Use your own camera entity ID in place of the example.

## Hide Photos From a Slideshow

Hide photos from the ambient display without deleting, archiving, or changing
anything in the source library. Exclusions belong to **one configured slideshow**,
persist across restarts and album refreshes, and apply to every card using that
slideshow's camera. Other configured slideshows are unaffected.

### Toolbar Modes

In the card editor, use the navigation, pause, and photo-management controls
below the form. For controls on the displayed card, choose
**Interaction > Photo controls**:

| Mode | Behavior |
|------|----------|
| **Off** (default) | No controls on the displayed card; they remain available in the editor |
| **On demand** | Hold the photo for half a second to reveal the toolbar; desktop hover or keyboard focus also reveals it |
| **Always** | Keep the toolbar visible |

The toolbar groups **Previous**, **Pause/Resume**, and **Next** separately from
**Hide**, **Undo hide**, and **Hidden photos**. Previous is disabled when no earlier
frame is cached. Previous/Next update the displayed photo even while the toolbar
is holding it, and still work while the slideshow is paused. Pause/Resume controls
the slideshow's existing pause switch, so it affects all cards using that camera;
the icon follows the actual Home Assistant state. **Refresh album** stays in the
editor's Actions section.

With **On demand**, the displayed photo stays steady while choosing an action.
This holds only that card's display; other cards continue normally unless you
use **Pause/Resume** to pause the slideshow itself.
The toolbar dismisses immediately when the mouse leaves the card. It also dismisses
after five seconds of inactivity, a click outside the card, or Escape.
It stays open while a dialog or action is active, or while it has
keyboard focus. Normal taps keep their configured behavior; a long press only
reveals controls and never hides a photo by itself. Scrolling cancels a pending
long press. Exclusion updates can still clear a held photo immediately.

For YAML, use `photo_controls: off`, `on_demand`, or `always`. Existing
`photo_controls: true` settings still mean Always, and `false` still means Off.
Paired slides offer explicit **left/right** or **top/bottom** choices, plus
**Hide both photos**.

### Undo and Restore

**Undo hide** restores the last hide action, including both photos if they were
hidden together. **Hidden photos** opens a paginated list where individual photos
can be restored. **Restore all** requires confirmation in the card. Hidden photos
remain restorable even when they are no longer in the source album.

### Replacement Frames

Hiding or restoring clears previous-frame history and removes unsafe preloaded
frames, so navigation cannot bring back hidden photos. Safe rendered frames are
reused immediately and the upcoming buffer is refilled in the background. If every
ready pair includes the hidden photo, the surviving half can be reused as a single
photo without fetching its source again. That replacement retains the pair's crop
and detail until a normal full-source frame is shown. If no safe cached frame or
half is available, preparing a replacement still requires a new render. Downloads
from another slideshow no longer block hide/restore behind the shared
image-processing lock; image-processing jobs remain serialized across albums.

Hiding the last eligible photo clears the display;
undo and management controls remain available. The **Hidden photos** sensor shows
the exclusion count without exposing the full list in entity history.

### Storage and Photo Identity

Exclusions are loaded before the slideshow starts. If they cannot be loaded,
setup fails instead of displaying photos without applying the hidden list. A
failed save leaves the existing exclusions unchanged. Check **Settings > System >
Logs** for the error and retry after resolving the storage problem.

Photo IDs come from the source, not filenames or expiring download URLs. Local
files use normalized full paths: renaming or moving a file changes its identity.
Replacing a source asset with a new ID also makes it a new photo. A photo without
a usable ID cannot be hidden; refresh an older cached album to obtain IDs. Once
a slideshow has exclusions, unidentified photos are skipped rather than risk
redisplaying a hidden photo.

For button entities, ID-based actions, and the hidden-list response, see the
[automation reference](reference.md#automation-actions).

## Full Options

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
photo_controls: on_demand
caption:                    # overlay selected photo metadata
  show: [date, location]    #   fields below; order = display order
  position: bottom-left     #   top/center/bottom + -left/-center/-right, or center
  date_format: medium       #   medium | full | month_year | year | numeric
                            #     | weekday | relative, or a custom token string
                            #     (YYYY, MMMM, MMM, MM, M, DD, D, dddd, ddd, REL)
  per_image: true           #   caption each half of a portrait pair separately
  color: '#ffffff'          #   any CSS color
  font_size: 14px           #   any CSS size
  font_weight: medium       #   light | normal | medium | semibold | bold
  shadow: true              #   drop shadow for readability on bright photos
```

## Transitions and Captions

- `transition: random` picks a different effect per slide and avoids repeating the previous one. Effects are `none`, `fade`, `slide-left`, `slide-right`, `slide-up`, `slide-down`, `wipe-left`, `wipe-right`, and `zoom`; `duration` and `easing` control the timing.
- `fit: auto` reads the camera's `fill_mode` attribute. `blur` renders the slide as `contain` plus a blurred backdrop layer behind it.
- `photo_controls` defaults to Off. See [Hide Photos From a Slideshow](#hide-photos-from-a-slideshow) for toolbar modes, gestures, paired-photo choices, and restore behavior.
- **Caption overlays:** use `captions:` for multiple independently styled overlays; the original single `caption:` configuration still works. Omit both to disable captions, or set `captions: []`. Availability varies by [provider](provider-setup.md#choose-a-provider), and missing fields are skipped. Google enrichment supplies descriptions and camera metadata; generic Media Source has no equivalent metadata path. On a pair, `per_image: true` anchors each photo's own metadata to its half; set it to `false` for a single caption over the whole frame.
- `date_format` accepts a preset name or a custom token string. Presets are locale-aware (they follow your Home Assistant language). Example custom format: `'D MMMM YYYY'` -> `29 July 2023`. The `REL` token inserts relative time, so `'D MMMM YYYY - REL'` -> `29 July 2023 - 3 years ago`.
- Every slide commit increments the camera's `frame_id` attribute. The card cache-busts the camera proxy URL with that value, so the browser refetches a fresh JPEG on every change instead of serving a stale cached image.
- If the entity is unavailable, the card shows a "Camera not ready" placeholder.

### Caption Fields

Choose these in a caption's **Content > Add** picker, or list them in `caption.show`:

| Field | Display |
|-------|---------|
| `date` | Photo capture date in the chosen date format |
| `current_date` | Today's date, using Home Assistant's time zone and the chosen date format |
| `current_time` | Live time, using Home Assistant's time zone and the chosen time format |
| `weather` | Current condition and temperature, a weather icon and temperature, or the temperature alone from a selected weather entity, or the selected sensor's state and units |
| `location` | Source location or reverse-geocoded place name |
| `description` | Photo description |
| `camera` | Camera make and model together, without a duplicated brand |
| `camera_make` | Camera brand only |
| `camera_model` | Camera model only |
| `focal_length_mm` | Focal length, such as `5.28 mm` |
| `aperture_f_number` | Aperture, such as `f/1.7` |
| `iso` | ISO sensitivity, such as `ISO 116` |
| `exposure_time_seconds` | Exposure, such as `1/125 s` or `2.5 s` |

Exposure fractions are rounded for readability. Numeric metadata must be
available as a positive finite number; missing or invalid values leave no empty
line. Only Google enrichment currently populates camera/exposure attributes.
Existing caption positions, font settings, and per-image layout apply to every
field. Keep the field selection and font size appropriate for small paired cards;
captions wrap within their own photo region and shrink only when needed to fit.
The chosen font size returns when more space is available.

## Multiple Captions and Clocks

In the card editor, expand **Captions > Add caption**. The caption list is
collapsed by default, like the other editor sections. Each caption has its own
enabled toggle, position, content selection, color, font size, weight, shadow,
and paired-photo setting. Use the copy icon to duplicate a caption at another
available position, or the delete icon to remove it. The same field can appear
in several captions, with different styling in each.

Each caption's header shows its placement, with its selected content summarized
on one line underneath. The **Content** field uses draggable chips: use **Add**
to search for a field, drag selected chips to change their display order, or
use a chip's remove button to drop it. The order is saved in `show` and controls
the caption's line order.

**Photo date** is the date the displayed picture was taken. **Today's date**
and **Current time** are independent of the photo and keep updating while the
slideshow is paused. **Photo date format** and **Today's date format** have
separate preset and custom-format selectors, even when both dates appear in the
same caption. Custom formats such as `DD MMMM YYYY - REL` still combine the
photo date with localized relative time, as in issue #31.
The clock offers HA's time-format preference, explicit
12-hour or 24-hour display, and optional seconds. These fields are rendered in
the browser; they need no additional sensors or backend requests.

Captions containing only today's date, time, or weather appear once over the whole
frame. Captions that also include photo metadata follow their own `per_image`
setting. Captions sharing the same position stack in list order. Other
positions reserve space for one another, and crowded text shrinks to fit.

```yaml
type: custom:album-slideshow-card
entity: camera.album_slideshow_living_room
captions:
  - show: [date, location]
    position: bottom-left
    font_size: 16px
    per_image: true
  - show: [description, camera]
    position: bottom-right
    font_size: 14px
    per_image: true
  - show: [current_date]
    position: top-left
    current_date_format: weekday
    font_size: 14px
  - show: [current_time]
    position: top-right
    time_format: 24h        # auto (HA preference) | 12h | 24h
    time_seconds: false
    font_size: 28px
    font_weight: semibold
```

Each list item accepts the same styling options as the original `caption:`
block. Add `enabled: false` to keep a caption configured but hidden. An explicit
`captions:` list takes precedence if a legacy `caption:` block is also present;
existing single-caption configurations need no migration.
For today's date, `current_date_format` takes precedence over `date_format`;
existing configurations that used only `date_format` continue to work.

### Full-Screen Dashboard Example

![Full-screen photo dashboard with a clock, temperature, and photo date and location](fullscreen-dashboard.jpg)

This example uses three independently placed captions: photo date and location
at the top right, temperature from a selected sensor at the bottom left, and
a large live clock at the bottom right. Use **Captions > Add caption** for each
overlay, then choose its placement, content, and font size. The same settings
also work in an ordinary dashboard card.

Full-screen or kiosk display and automatic screensaver activation are provided
by your dashboard, browser, or device configuration, not by the card. The card
supplies the slideshow and captions; it does not turn screensaver mode on.

### Weather Captions

Select **Weather** in a caption's **Content > Add** picker, then choose the required
**Weather source**. The picker accepts an existing `weather.*` entity or
`sensor.*` entity. No source is chosen automatically and no new sensor is created.

A weather entity displays its localized condition and current temperature,
such as "Partly cloudy, 22.5 C". A sensor displays its state with its configured
units. The caption follows that entity's HA updates, including while a photo
is paused, without fetching or changing the slide. A missing, unknown, or
unavailable source leaves the weather line hidden. Other selected caption
fields continue to display.

For a weather entity, **Weather display** (`weather_display`) chooses the format:

| `weather_display` | Example |
|---|---|
| `condition_temperature` (default) | Partly cloudy, 22.5 C |
| `icon_temperature` | A colored partly cloudy icon, then 22.5 C |
| `temperature` | 22.5 C |

The icon scales with the caption's font size and gets a drop shadow when
**Text shadow** is on. After sunset (from `sun.sun`), partly cloudy shows a
moon instead of the sun. A condition without an icon is shown as text, and a
sensor always shows its state.

With the icon display, **Weather icons** (`weather_icons`) picks the icon set:

| `weather_icons` | Icons |
|---|---|
| `yr` (default) | [Yr weather symbols](https://github.com/metno/weathericons) (MIT license), with wind and warning icons from Meteocons |
| `meteocons` | [Meteocons](https://github.com/basmilius/meteocons) Fill icons by Bas Milius (MIT license) |
| `home_assistant` | The icons of Home Assistant's own weather forecast card (Apache License 2.0). They follow your theme's `--weather-icon-*` colors. An exceptional condition is shown as text |

Each caption can select a different source and use its own placement and style:

```yaml
captions:
  - show: [weather]
    weather_entity: weather.home
    weather_display: icon_temperature
    weather_icons: yr       # yr | meteocons | home_assistant
    position: top-right
    font_size: 18px
```

Use your own weather entity or sensor ID. This reads current HA state only;
it does not request forecasts or contact an additional weather service.

For example, `show: [date, description, camera, iso, exposure_time_seconds]`
adds photo details without creating any sensor entities. The default remains
`[date, location]` when captions are enabled without a field selection.

For Google location captions, enable both [location privacy options](provider-setup.md#google-location-and-privacy)
in the integration's Configure dialog, then select `location` in the card.
GPS reading alone supplies coordinates as attributes, not a place-name caption.
Both location options are off by default and independent of camera metadata.

The card editor also exposes **Custom lookback days** and **Shuffle age bias**
under Slideshow settings when the corresponding modes are selected. These are
integration controls, not card YAML settings. See the
[lookback and shuffle reference](reference.md#custom-lookback-and-age-bias).

See the [rendering reference](reference.md#rendering-options) for fill modes,
orientation pairing, aspect ratios, and transparent dividers.