# Provider Setup

[Overview](../README.md) | [Card Guide](card-guide.md) | [Reference](reference.md)

Pick the provider that matches where your photos live. All providers show still
images, not videos. Install the integration first using the
[installation instructions](../README.md#installation).

## Choose a Provider

| Provider | Best for | Date filter / ordering | Location | Description caption |
|----------|----------|:---:|:---:|:---:|
| [Google Photos](#google-photos) | A shared album link | Yes (dates only) | No | No |
| [Immich](#immich) | An Immich server (album, person, favorites, all, search) | Yes | Yes | Yes |
| [PhotoPrism](#photoprism) | A PhotoPrism server (album, person, favorites, all, search) | Yes | Yes | Yes |
| [iCloud](#icloud-shared-album) | An iCloud Shared Album public link | Yes | No | Yes |
| [Synology](#synology-photos) | A Synology Photos library (favorites, albums, people, places, tags, subjects) | Yes | Yes | Yes |
| [Nextcloud (folder)](#authenticated-webdav-folder) | Any folder in your Nextcloud files (WebDAV, app password) | Yes | Yes | Yes |
| [Nextcloud (public link)](#public-album-link) | A public Nextcloud Photos album share link (no login) | Yes | Yes | Yes |
| [Ente Photos](#ente-photos) | A public Ente album link (no login, end-to-end encrypted) | Yes | Yes | Yes |
| [Local Folder](#local-folder-or-nas) | Files on the HA host / NAS | Yes | Yes | Yes |
| [Media Source](#media-source) | Any HA media source with no API (local media, Jellyfin, ...) | No | No | No |

> Media Source and Google Photos serve photos as URLs, so there is no EXIF
> to read. For full metadata (dates, location, description), use **Local
> Folder** for local/NAS files or the **Immich** / **PhotoPrism** provider for
> a self-hosted photo server. The Media Source route also works with those but
> without metadata, so prefer the direct provider when you have one.

## Google Photos

1. Open a shared Google Photos album.
2. Copy the shared link such as `https://photos.app.goo.gl/...`.
3. Add the integration.
4. Paste the link.

### Google Photos Limits

- Public shared albums only (link sharing must be enabled)
- Up to 20,000 photos per album
- Videos are skipped
- Internet connection required
- Relies on Google's public web endpoints; if Google changes them, the integration falls back to a 300-photo limit until the scraper is updated
- The last successful album fetch is cached to disk; if a refresh fails or returns no photos, the slideshow keeps running with the cached list

## Immich

The **Immich** provider connects straight to your [Immich](https://immich.app/)
server for **full photo metadata**: capture date, GPS/location, and description
all work, and you can slideshow far more than just an album. If you have an
Immich server, prefer this over the Media Source route.

1. In Immich, create an API key: **Account Settings > API Keys > New API Key**.
   Read scopes are enough: `server.about`, `asset.read`, `asset.view`,
   `asset.download`, `album.read`, `person.read`. `server.about` is what the
   setup step uses to check the URL and key, so setup fails without it.
2. Add the integration and choose **Immich (direct API, full metadata)**.
3. Enter your Immich URL (e.g. `http://192.168.1.10:2283`) and the API key.
4. Give it a name, tick what you want to show, and choose the image quality.

### Choosing What to Show

Tick any mix of these and they are combined into one slideshow:

| Option | What it adds |
|--------|--------------|
| **Albums** | Photos from the albums you pick (searchable, with **Select all**) |
| **People** | Photos of the people you pick (searchable, with **Select all**) |
| **Include favorites** | Everything you have favorited in Immich |

Immich has no "OR" search, so the integration queries each album and each
person separately and merges the results, deduplicated. That means **People**
gives you every photo that includes any of them (not only the group shots where
they all appear together), and you can freely mix albums, people and favorites -
for example "the Family album OR these 5 people OR my favorites". **Leave
everything empty to show your whole library (all photos).**

**Advanced:** you can also add an Immich search filter (JSON) to fold its results
into the same slideshow. It is passed to
Immich's [`search/metadata`](https://api.immich.app/endpoints/search/searchAssets)
endpoint (with `type` forced to images). Examples:

```json
{ "city": "Paris", "isFavorite": true }
```
```json
{ "country": "Japan", "takenAfter": "2023-01-01T00:00:00Z" }
```

### Changing an Existing Source

Open **Settings > Devices & services > Album Slideshow** and use the Immich
entry's **Configure** button to change its albums, people, favorites, search
filter, name, or image quality. The reverse-geocoding privacy toggle remains
available. Saving reloads that slideshow without changing its entry ID,
entities, runtime settings, or hidden-photo list.

Saved albums and people that cannot currently be listed remain selected and
are marked **unavailable**. They are not silently removed. Deselecting all
sources and clearing the filter deliberately selects the entire library.
If the stored connection fails, the flow first asks for a working URL and key.

### Image Quality

- **Preview** (default) - a downscaled preview; smoothest slideshow.
- **Full size** - the large rendered version.
- **Original** - the untouched original file (largest, slowest).

### Notes

- The API key is sent **only as a server-side request header**, so it never
  appears in the camera's `current_url` attribute or reaches the browser. Home
  Assistant fetches and re-serves the images; your Immich server is never
  exposed to the dashboard client.
- Capture dates come from the asset list up front, so date filters and date
  ordering work immediately. Location and description are filled in by a
  background pass, so they appear
  shortly after the first load, the same way local-folder EXIF does.

### Face-aware Cropping

In **Cover** fill mode, the integration uses Immich's existing face coordinates
for albums, people, favorites, all-photo selections, and custom searches.
Each half of a paired slide gets its own crop. No new face recognition runs
inside Home Assistant.

The crop favors whole faces and adds room around them where space allows.
Explicitly selected people take priority over bystanders regardless of their
relative sizes. Within each priority group, face area determines which group
to retain when everyone cannot fit. Padding is a preference, not a reason to
discard a face that fits. If a face is too large for the crop, the crop retains
visible face area instead of choosing empty background.

- Add the optional `face.read` permission to the Immich API key. For photos
  cropped, rotated, or mirrored inside Immich, also grant `asset.edit.get`
  so the face coordinates can be mapped back to the downloaded image.
- Image quality and download behavior are unchanged, including **Original**.
  Immich's edits are used to interpret face positions, not applied to the
  displayed image or written back to your library.
- Focus arrives through background enrichment. Initial slides may use a
  centered crop until their metadata has been read.
- Missing permissions, unavailable metadata, or no detected faces fall back
  to the usual centered crop. Failed lookups retry on the next album refresh;
  after granting permissions, press **Refresh album** rather than recreating
  the integration. Existing EXIF metadata and offline playlist caches are kept.
- **Contain** and **Blur** keep their existing behavior. Generic Media Source
  cannot provide the required Immich face metadata.
- A fixed-aspect Cover crop cannot guarantee that every face fits when a
  group spans too much of the photo. Use **Contain** or **Blur** when retaining
  the entire photo is more important than filling the frame.

### Crop Debug Overlay

The per-slideshow **Crop debug overlay** switch is off by default. It draws
green boxes for kept faces, red for cut faces, and grey for excluded faces
where their outlines are visible. Thin outlines show preferred padding.
A yellow crosshair marks the original photo's center; an edge arrow points
back to it when it is outside the crop. A label summarizes the detected faces
or shows **no face data** while metadata is unavailable.

The overlay is rendered into the camera image, so all cards using that camera
see it. Turn it off when finished. Its setting survives restarts. Debug logging
also includes per-photo crop summaries. Other providers can show the center
marker but do not gain face recognition.

### Immich Limits

- Requires an Immich server reachable from Home Assistant and an API key
- Videos are skipped
- Home Assistant fetches and re-serves images, so the Immich server does not need to be reachable from the dashboard client (and the API key never leaves the server)
- Location and description are read per photo in the background, so they appear shortly after the first load

## PhotoPrism

The **PhotoPrism** provider connects straight to your
[PhotoPrism](https://www.photoprism.app/) server for **full photo metadata**:
capture date, GPS/location, and description all work, and you can combine
albums, people and favorites into one slideshow. If you have a PhotoPrism
server, prefer this over the Media Source route.

1. Add the integration and choose **PhotoPrism (direct API, full metadata)**.
2. Enter your PhotoPrism URL (e.g. `http://192.168.1.10:2342`).
3. Choose how to authenticate:
   - **App password** (recommended) - in PhotoPrism go to
     **Settings > Account > Apps and Devices** and create one, then paste it here.
   - **Username + password** - your normal PhotoPrism login. The password is
     stored so the integration can refresh its session automatically; it is
     kept on the server side and never reaches the browser.
4. Give it a name, tick what you want to show, and choose the image quality.

### Choosing What to Show

Works exactly like the Immich picker - tick any mix and they are combined into
one slideshow:

| Option | What it adds |
|--------|--------------|
| **Albums** | Photos from the albums you pick (searchable, with **Select all**) |
| **People** | Photos of the people you pick (searchable, with **Select all**) |
| **Include favorites** | Everything you have favorited in PhotoPrism |

PhotoPrism has no "OR" across filters, so the integration queries each album and
each person separately and merges the results, deduplicated - so **People**
gives you every photo that includes any of them, and you can freely mix albums,
people and favorites. **Leave everything empty to show your whole library.**

**Advanced:** you can also add a PhotoPrism
[search query](https://docs.photoprism.app/user-guide/search/filters/) to fold
its results into the same slideshow, for example:

```text
color:red
```
```text
country:jp year:2023
```

### Image Quality

- **Preview** (default, 1280px) - smoothest slideshow.
- **Full size** (1920px) - more detail.
- **High detail** (2560px) - largest, slowest.

### Notes

- PhotoPrism serves thumbnails with a rotatable preview token in the URL (its
  own cookie-free scheme), so no login token is ever placed in the image URL.
  The integration reads the preview token from case-insensitive search response
  headers or, with username/password authentication, from the login response.
- All photo metadata (date, location, description) comes back inline with the
  photo list, so date filters, location and captions work from the first load
  with no background pass.

## iCloud Shared Album

The **iCloud** provider slideshows a **public iCloud Shared Album**. No Apple ID
or password is needed - the album's share link is the only credential, the same
way anyone with the link can view it on the web.

1. In the **Photos** app (iPhone/iPad/Mac), open the shared album, tap the
   people/share icon, and enable **Public Website** (then copy that link). The
   link looks like `https://www.icloud.com/sharedalbum/#B2Xabc...`.
2. Add the integration, choose **iCloud Shared Album**, paste the link, give it
   a name, and pick an image quality.

### Image Quality

- **Full size** (default) - the largest version Apple generated (usually around
  2048px); best for a slideshow.
- **Preview** - a small thumbnail; fastest / least bandwidth.

### Notes

- **Capture date and captions work** (both come inline with the album data), so
  date filters, date ordering, and the caption overlay all apply.
- **No location.** Apple strips GPS from shared-album web data, so the
  `latitude`/`longitude`/`location` attributes stay empty (same as Google
  Photos).
- Contributors can keep adding photos to the album; new ones show up on the next
  refresh.
- The image URLs Apple hands out are signed and expire after about a day, so the
  integration re-fetches them on every album refresh.
- Image downloads marked `application/octet-stream` are accepted only from
  `icloud-content.com` and its subdomains, checked after redirects. Download
  limits and image decoding checks still apply. HEIC/HEIF decoding depends on
  the codecs available in your Home Assistant installation.

### Troubleshooting Slow Legacy Albums

For legacy Shared Albums that fail during setup or the first refresh, v1.10.0
includes request-stage diagnostics and retries. It does not change the CloudKit
backend or the image decoder.

- Each listing or image-URL request has a 15-second connection limit, a
  60-second idle-read limit and a 90-second total limit. These are per-request
  limits, not a deadline for loading the whole album.
- A timeout, connection/payload failure or selected transient HTTP error gets
  one retry after one second. Successful URL batches are not repeated. TLS
  errors, invalid links, rate limits and other non-transient failures are not
  retried within the request.
- Failures identify link validation, photo listing, or the image-URL batch
  number, with the host, endpoint, attempt, elapsed time and error type/status.
  These request diagnostics omit the share token and raw response contents.
- Apple's HTTP 330 partition redirects are accepted from either the JSON body
  or response headers, limited to one redirect to an Apple shared-streams host.

To test: HACS > Album Slideshow > three-dot menu > **Redownload** > **v1.10.0** (or newer),
then restart Home Assistant and retry the entry. If it still fails, share the
new `Error querying iCloud album` or `iCloud validation failed` message after
checking it for personal information. No album share link is needed for this
diagnostic test.

## Synology Photos

The **Synology** provider connects straight to the **Photos** package on your
Synology NAS for **full photo metadata** (capture date, GPS location and
captions). Like the Immich and PhotoPrism providers, you can combine any mix of
**favorites, albums (including albums shared with you), people, places, tags and
subjects** into one slideshow, from either the **personal** ("My Photos") or
**shared** ("Shared Space") library.

1. Add the integration and choose **Synology Photos (direct API, full
   metadata)**.
2. Enter your DSM address (e.g. `http://192.168.1.10:5000`, or your HTTPS /
   QuickConnect URL) and an account username and password.
3. Choose the **Personal** or **Shared** library.
4. If the account has **two-factor authentication**, also enter a current
   6-digit code. This is only needed once - a trusted-device token is stored so
   later refreshes never prompt for a code again.
5. Tick what you want to show (favorites, albums, people, places, tags,
   subjects) - or leave everything unticked for **all photos** - then name it
  and choose an image quality.

> **Combining sources.** Synology has no "OR" across categories, so the
> integration queries each ticked album/person/place/tag/subject separately and
> merges the results (duplicates removed). Favorites and subjects are a Personal
> library feature.

### Image Quality

Synology serves pre-generated thumbnails:

- **Large** (default) - the biggest thumbnail; best for a slideshow.
- **Medium** - a good balance of detail and bandwidth.
- **Small** - a small thumbnail; fastest / least bandwidth.

### Notes

- **Date, location and captions all work** - Synology returns capture date, GPS
  coordinates and a reverse-geocoded place name inline, so date filters, date
  ordering, the location attribute, and the caption overlay all apply.
- The password is stored so the integration can re-authenticate when its session
  expires. The session id is sent only server-side (it never appears in the
  camera's image URL or the browser).
- New photos added to the album or library show up on the next refresh.
- Albums that another user shared with your account appear under **Albums**
  tagged "(shared)"; they are fetched by their share passphrase.

> **Use a dedicated account.** Create a normal (non-admin) DSM user, give it
> access only to the Photos content you want to show, and use that here rather
> than your admin login.

## Nextcloud

The **Nextcloud** provider has two connection modes, both with **full photo
metadata** (capture date, GPS location and captions): an authenticated
**WebDAV folder**, or a public **Nextcloud Photos album link** that needs no
login. Add the integration, choose **Nextcloud**, then pick a connection type.

### Authenticated WebDAV Folder

Slideshows **any folder in your Nextcloud files** over WebDAV. Works on any
Nextcloud server - no Photos or Memories app is required.

1. In Nextcloud, create an **app password**: **Settings -> Security ->
   Devices & sessions -> Create new app password**. Copy the generated
   password (it is shown only once).
2. Choose **Authenticated WebDAV folder**.
3. Enter your server URL (e.g. `https://cloud.example.com`), your username, and
   the app password.
4. Point it at a **folder path** (e.g. `Photos/Family`), or leave it blank for
   your whole files root. Tick **Include subfolders** to recurse.
5. Name it and pick an image quality.

### Public Album Link

Slideshows a **public Nextcloud Photos album share**, with no Nextcloud
login stored or required.

1. In the Nextcloud **Photos** app, open the album you want to share and
   create a **public link share** (or use an existing one).
2. Choose **Public album link**.
3. Paste the share link (e.g.
   `https://cloud.example.com/apps/photos/public/AbC123`).
4. Name it and pick an image quality.

### Image Quality

- **Preview** (default) - a resized thumbnail; smoothest slideshow.
- **Original** - the untouched original file (largest, slowest).

### Notes

- **Date, location and captions all work in both modes.** Nextcloud has no
  metadata-only API, so the integration reads EXIF the same way the **Local
  Folder** provider does: it downloads each original photo once in the
  background and reads its EXIF/IPTC/XMP. Progress is tracked by the
  **Enrichment progress** diagnostic sensor, and the same reverse-geocoding
  opt-out applies in the integration's **Configure** dialog.
- **Folder mode:** the app password is stored so the integration can re-list
  the folder on each refresh. It is sent to Nextcloud server-side only (HTTP
  Basic auth) and never appears in the camera's image URL or the browser.
- **Public link mode:** no credentials are stored - the share token embedded
  in the link is the only thing the server checks. Anyone with the link (or
  the camera's image URL) can view the photos, same as opening the share
  page directly.
- New photos dropped into the folder or added to the shared album show up on
  the next refresh.
- Videos and non-image files are skipped.

## Ente Photos

The **Ente** provider slideshows a **public Ente album link**, with **full
photo metadata** (capture date, GPS location and captions). No Ente account,
password or API key is involved.

Ente is **end-to-end encrypted**, so this provider works differently from the
others: the album's decryption key travels in the link itself and never
reaches Ente's servers. Home Assistant downloads the encrypted bytes and
decrypts them locally, then serves the decrypted image to your dashboard.

1. In Ente (mobile or web), open the album, tap **Share** and create a
   **public link**.
2. Copy the link. It looks like
   `https://albums.ente.io/?t=TOKEN#KEY`.
3. Add the integration and choose **Ente Photos (public album link)**.
4. Paste the link, name the album and pick an image quality.

> [!IMPORTANT]
> **Copy the whole link, including everything after the `#`.** That fragment
> is the album's decryption key. Without it the photos cannot be decrypted,
> and some apps truncate links at the `#` when sharing them. If setup fails
> with "that does not look like an Ente public album link", a missing
> fragment is the usual cause.

### Image Quality

- **Full quality** (default) - the original file, decrypted locally.
- **Preview** - Ente's smaller pre-generated thumbnail; much faster to load
  and lighter on CPU, noticeably softer on a large display.

### Self-hosted Ente

Leave **API endpoint** blank to use Ente's hosted service. If you run your own
Ente (museum) server, enter its API URL there, for example
`https://api.photos.example.com`.

### Notes

- **Date, location and captions all work**, and unlike the folder-style
  providers they cost nothing extra: Ente returns metadata alongside the file
  list, so it is decrypted up front rather than by downloading every photo.
  Reverse-geocoding into a `location` label still applies, with the same
  opt-out in the integration's **Configure** dialog.
- **Decryption happens in Home Assistant.** Because there is no URL that
  serves a decrypted image, the camera's `current_url` attribute shows an
  internal `ente://<id>` reference instead of a real link. The access token
  and decryption key are never placed in an image URL or exposed to the
  browser.
- The link's access token and collection key are stored in the config entry so
  the integration can re-list and decrypt on each refresh.
- Full-quality mode decrypts each original in Home Assistant. On a low-powered
  host (a Pi, say) with very large photos, **Preview** gives a smoother
  slideshow.
- Password-protected album links are not supported yet.
- Videos and live photos are skipped.
- Photos added to the album show up on the next refresh.

## Local Folder or NAS

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

### EXIF Capture Date and Location

For local-folder entries the integration reads EXIF metadata in the
background after every refresh:

- **Capture date:** `DateTimeOriginal` is preferred; if missing, the file's
  modification time is used so date-based ordering still works for
  screenshots and scans. When `OffsetTimeOriginal` is present (most modern
  cameras and phones) it is honoured; otherwise the timestamp is interpreted
  as the host's local time.
- **GPS coordinates:** `GPSLatitude`/`GPSLongitude` are exposed as the
  `latitude` / `longitude` camera attributes. `(0, 0)` "null island" stamps
  are ignored.
- **Reverse-geocoded location:** by default the integration calls the
  public [Nominatim](https://nominatim.openstreetmap.org/) (OpenStreetMap)
  service to translate coordinates into a human-readable label such as
  `"Lisbon, Portugal"`, exposed as the `location` attribute. Coordinates
  are rounded to **~100 m** before lookup and the answer is cached on disk,
  so the same neighbourhood is only ever fetched once. Nominatim's
  free-tier policy (1 req/sec, identifying User-Agent) is respected.

**Privacy / opt-out:** if you'd rather not send any coordinates to
OpenStreetMap, open *Settings > Devices & Services > Album Slideshow > your
album > Configure* and turn off **Reverse-geocode EXIF GPS coordinates**.
The `latitude`/`longitude` attributes still work; only the `location`
label is suppressed. The opt-out is per-album.

Progress for both phases is exposed as the **Enrichment progress**
diagnostic sensor (percent complete, with `phase`, `exif_done`,
`geocode_done` etc. as attributes).

## Media Source

The **Media Source** provider points the slideshow at any Home Assistant
[Media Source](https://www.home-assistant.io/integrations/media_source/)
folder. It works with local media, Jellyfin, Immich, and any other integration
that exposes a media source. Prefer a direct provider such as
[Immich](#immich) when you need full photo metadata.

1. Add the integration and choose **Media Source (Immich, local media, ...)**.
2. Give the album a **name**.
3. Paste the **Media Source id** of the folder you want (it starts with
   `media-source://`). See below for how to find it.

The integration walks that folder (and its subfolders) collecting images,
skipping videos, system folders (e.g. Synology `@eaDir`), and non-web
formats (`.psd`, `.tiff`, `.heic`, RAW). It re-reads the folder on every
album refresh, so photos you add later show up automatically.

### How to Find the Media Source ID

**Immich**

1. Open the sidebar **Media** browser (or a Media card).
2. Browse into **Immich > Albums / People / Tags > your album**.
3. The folder's id looks like
   `media-source://immich/<config-entry-id>|albums|<album-id>` (people and
   tags use `|people|` / `|tags|`). To copy the exact value, open your
   browser's developer tools > **Network** tab, filter for `media_source`,
   click into the folder, and read the folder's `media_content_id` from the
   `media_source/browse_media` response.

**Local media (e.g. a NAS folder under `/media`)**

You can build the id from the path. Take whatever comes after `/media/` and
prefix it with `media-source://media_source/local/`:

| Media path | Media Source id |
|------------|-----------------|
| `/media/local/Pictures/Family` | `media-source://media_source/local/Pictures/Family` |
| `/media/Photos/2024` | `media-source://media_source/local/Photos/2024` |

> Point the id at a **folder**, not a single file. Don't URL-encode spaces
> in the config field, type them normally.

### Metadata Limitation

Media Source hands the slideshow **URLs**, not files, so there is **no EXIF
to read**. For Media Source albums this means:

- **no date filter / date ordering** (no capture or upload date)
- **no GPS `latitude` / `longitude` / `location`**
- **no description caption**

If your photos are local files (for example a NAS folder mounted under `/media`),
use the **Local Folder** provider instead of Media Source to get full EXIF-based
dates, location, and description captions. Google Photos also lacks location
and descriptions, but does provide dates as shown in the provider table.