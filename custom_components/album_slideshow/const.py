DOMAIN = "album_slideshow"

CONF_PROVIDER = "provider"
CONF_ALBUM_URL = "album_url"
CONF_ALBUM_NAME = "album_name"
CONF_LOCAL_PATH = "local_path"
CONF_RECURSIVE = "recursive"
CONF_IMAGE_CACHE_MB = "image_cache_mb"
# Media Source provider: a ``media-source://...`` content id pointing at a
# folder-like node (e.g. an Immich people/album view, or local media). The
# coordinator browses it, collects the image children, and resolves each to
# a playable URL.
CONF_MEDIA_CONTENT_ID = "media_content_id"
# Immich (direct API) provider.
CONF_IMMICH_URL = "immich_url"
CONF_IMMICH_API_KEY = "immich_api_key"
CONF_IMMICH_SELECTION_TYPE = "immich_selection_type"
CONF_IMMICH_SELECTION_ID = "immich_selection_id"
CONF_IMMICH_IMAGE_SIZE = "immich_image_size"
CONF_IMMICH_FILTER = "immich_filter"

IMMICH_SELECTION_ALBUM = "album"
IMMICH_SELECTION_ALBUMS = "albums"
IMMICH_SELECTION_PERSON = "person"
IMMICH_SELECTION_PEOPLE = "people"
IMMICH_SELECTION_FAVORITES = "favorites"
IMMICH_SELECTION_ALL = "all"
IMMICH_SELECTION_RANDOM = "random"
IMMICH_SELECTION_SEARCH = "search"
# Composite: a client-side union of any mix of albums, people, favorites and a
# custom filter. Immich has no OR operator, so each member is queried on its
# own and the results are merged (see #19). The selection id is a JSON object
# ``{"albums": [...], "people": [...], "favorites": bool}``; an empty composite
# means "all photos".
IMMICH_SELECTION_COMPOSITE = "composite"

IMMICH_IMAGE_PREVIEW = "preview"
IMMICH_IMAGE_FULLSIZE = "fullsize"
IMMICH_IMAGE_ORIGINAL = "original"
IMMICH_IMAGE_SIZE_OPTIONS = [
    IMMICH_IMAGE_PREVIEW,
    IMMICH_IMAGE_FULLSIZE,
    IMMICH_IMAGE_ORIGINAL,
]
DEFAULT_IMMICH_IMAGE_SIZE = IMMICH_IMAGE_PREVIEW
# Local-folder option: when True (default) the coordinator does best-effort
# reverse geocoding of EXIF GPS coordinates via the public Nominatim
# (OpenStreetMap) endpoint and exposes a human-readable ``location``
# attribute. Users with privacy concerns can disable this from the
# integration's options dialog.
CONF_REVERSE_GEOCODE = "reverse_geocode"
DEFAULT_REVERSE_GEOCODE = True

PROVIDER_GOOGLE_SHARED = "google_shared"
PROVIDER_LOCAL_FOLDER = "local_folder"
PROVIDER_MEDIA_SOURCE = "media_source"
PROVIDER_IMMICH = "immich"
PROVIDER_PHOTOPRISM = "photoprism"
PROVIDER_ICLOUD = "icloud"
PROVIDER_SYNOLOGY = "synology"
PROVIDER_NEXTCLOUD = "nextcloud"

# Nextcloud (authenticated WebDAV folder) provider. Points at any folder in a
# user's files and lists it over WebDAV. Auth is HTTP Basic with a username +
# app password (Settings > Security > Devices & sessions); the app password is
# stored so the coordinator can re-list on each refresh and is sent server-side
# only, never reaching the browser.
CONF_NEXTCLOUD_URL = "nextcloud_url"
CONF_NEXTCLOUD_USERNAME = "nextcloud_username"
CONF_NEXTCLOUD_PASSWORD = "nextcloud_password"
CONF_NEXTCLOUD_FOLDER = "nextcloud_folder"
CONF_NEXTCLOUD_RECURSIVE = "nextcloud_recursive"
CONF_NEXTCLOUD_IMAGE_SIZE = "nextcloud_image_size"

# ``preview`` uses the core/preview thumbnail endpoint (smoother, smaller);
# ``original`` fetches the real file straight off the WebDAV collection.
NEXTCLOUD_IMAGE_PREVIEW = "preview"
NEXTCLOUD_IMAGE_ORIGINAL = "original"
NEXTCLOUD_IMAGE_SIZE_OPTIONS = [NEXTCLOUD_IMAGE_PREVIEW, NEXTCLOUD_IMAGE_ORIGINAL]
DEFAULT_NEXTCLOUD_IMAGE_SIZE = NEXTCLOUD_IMAGE_PREVIEW
# Long edge (px) requested from the core/preview endpoint for preview quality.
NEXTCLOUD_PREVIEW_PX = 1920

# Synology Photos (direct API) provider. Talks to a DSM Photos package over its
# entry.cgi web API. The account password is stored so the coordinator can
# re-authenticate when the session id expires; accounts with 2FA are handled by
# capturing a trusted-device token during setup (see synology.py).
CONF_SYNOLOGY_URL = "synology_url"
CONF_SYNOLOGY_USERNAME = "synology_username"
CONF_SYNOLOGY_PASSWORD = "synology_password"
CONF_SYNOLOGY_DEVICE_ID = "synology_device_id"
CONF_SYNOLOGY_SPACE = "synology_space"
CONF_SYNOLOGY_ALBUM_ID = "synology_album_id"
CONF_SYNOLOGY_IMAGE_SIZE = "synology_image_size"
# Passphrase for an album that was shared with the configured account. Present
# only when the chosen source is a shared-with-me album; such albums are
# reachable by passphrase rather than by album id.
CONF_SYNOLOGY_PASSPHRASE = "synology_passphrase"
# When True, the source is the account's Favorites (favorited photos) rather
# than the whole space or a specific album.
CONF_SYNOLOGY_FAVORITE = "synology_favorite"
# Composite selection: a client-side union of any mix of albums, people,
# places, tags, subjects and favorites. Synology has no OR across categories,
# so each selected member is queried on its own and the results are merged
# (see the Immich/PhotoPrism composite). Stored as a JSON object:
# ``{"favorites": bool, "album_ids": [...], "passphrases": [...],
#    "person_ids": [...], "geocoding_ids": [...], "tag_ids": [...],
#    "concept_ids": [...]}``; an empty composite means the whole space.
CONF_SYNOLOGY_SELECTION = "synology_selection"

# Personal ("My Photos") vs shared ("Shared Space") library.
SYNOLOGY_SPACE_PERSONAL = "personal"
SYNOLOGY_SPACE_SHARED = "shared"

# Native Synology thumbnail sizes. ``xl`` is the largest (best for a slideshow).
SYNOLOGY_IMAGE_SMALL = "sm"
SYNOLOGY_IMAGE_MEDIUM = "m"
SYNOLOGY_IMAGE_LARGE = "xl"
SYNOLOGY_IMAGE_SIZE_OPTIONS = [
    SYNOLOGY_IMAGE_SMALL,
    SYNOLOGY_IMAGE_MEDIUM,
    SYNOLOGY_IMAGE_LARGE,
]
DEFAULT_SYNOLOGY_IMAGE_SIZE = SYNOLOGY_IMAGE_LARGE

# iCloud Shared Album provider. The share token in the pasted link is the only
# credential; no account or password is involved.
CONF_ICLOUD_URL = "icloud_url"
CONF_ICLOUD_TOKEN = "icloud_token"
CONF_ICLOUD_IMAGE_SIZE = "icloud_image_size"

# ``full`` picks the largest derivative Apple generated (best for a slideshow,
# usually ~2048px); ``preview`` picks the smallest (a thumbnail; fastest).
ICLOUD_IMAGE_FULL = "full"
ICLOUD_IMAGE_PREVIEW = "preview"
ICLOUD_IMAGE_SIZE_OPTIONS = [ICLOUD_IMAGE_FULL, ICLOUD_IMAGE_PREVIEW]
DEFAULT_ICLOUD_IMAGE_SIZE = ICLOUD_IMAGE_FULL


# PhotoPrism (direct API) provider.
CONF_PHOTOPRISM_URL = "photoprism_url"
CONF_PHOTOPRISM_AUTH_METHOD = "photoprism_auth_method"
CONF_PHOTOPRISM_TOKEN = "photoprism_token"
CONF_PHOTOPRISM_USERNAME = "photoprism_username"
CONF_PHOTOPRISM_PASSWORD = "photoprism_password"
CONF_PHOTOPRISM_SELECTION_TYPE = "photoprism_selection_type"
CONF_PHOTOPRISM_SELECTION_ID = "photoprism_selection_id"
CONF_PHOTOPRISM_IMAGE_SIZE = "photoprism_image_size"
CONF_PHOTOPRISM_FILTER = "photoprism_filter"

PHOTOPRISM_AUTH_APP_PASSWORD = "app_password"
PHOTOPRISM_AUTH_USER_PASSWORD = "user_password"

# PhotoPrism thumbnail sizes (from its Thumbnail Image API). ``preview`` is a
# good slideshow default; the larger sizes trade bandwidth for detail.
PHOTOPRISM_IMAGE_PREVIEW = "fit_1280"
PHOTOPRISM_IMAGE_FULLSIZE = "fit_1920"
PHOTOPRISM_IMAGE_ORIGINAL = "fit_2560"
PHOTOPRISM_IMAGE_SIZE_OPTIONS = [
    PHOTOPRISM_IMAGE_PREVIEW,
    PHOTOPRISM_IMAGE_FULLSIZE,
    PHOTOPRISM_IMAGE_ORIGINAL,
]
DEFAULT_PHOTOPRISM_IMAGE_SIZE = PHOTOPRISM_IMAGE_PREVIEW

# Composite: client-side union of albums + people + favorites (+ optional
# search query). PhotoPrism has no OR across filters, so each member is
# queried separately and merged. Selection id is a JSON object
# ``{"albums": [...], "people": [...], "favorites": bool}``; empty means all.
PHOTOPRISM_SELECTION_COMPOSITE = "composite"


FILL_COVER = "cover"
FILL_CONTAIN = "contain"
FILL_BLUR = "blur"

ORIENTATION_MISMATCH_PAIR = "pair"
ORIENTATION_MISMATCH_SINGLE = "single"
ORIENTATION_MISMATCH_AVOID = "avoid"

ORDER_RANDOM = "random"
ORDER_ALBUM = "album_order"
ORDER_NEWEST_TAKEN = "newest_taken"
ORDER_OLDEST_TAKEN = "oldest_taken"
ORDER_NEWEST_ADDED = "newest_added"
ORDER_OLDEST_ADDED = "oldest_added"

ORDER_OPTIONS = [
    ORDER_RANDOM,
    ORDER_ALBUM,
    ORDER_NEWEST_TAKEN,
    ORDER_OLDEST_TAKEN,
    ORDER_NEWEST_ADDED,
    ORDER_OLDEST_ADDED,
]

DATE_FILTER_OFF = "off"
DATE_FILTER_LAST_7 = "last_7_days"
DATE_FILTER_LAST_30 = "last_30_days"
DATE_FILTER_LAST_365 = "last_365_days"
DATE_FILTER_THIS_MONTH = "this_month"
DATE_FILTER_THIS_YEAR = "this_year"
DATE_FILTER_ON_THIS_DAY = "on_this_day"

DATE_FILTER_OPTIONS = [
    DATE_FILTER_OFF,
    DATE_FILTER_LAST_7,
    DATE_FILTER_LAST_30,
    DATE_FILTER_LAST_365,
    DATE_FILTER_THIS_MONTH,
    DATE_FILTER_THIS_YEAR,
    DATE_FILTER_ON_THIS_DAY,
]
DEFAULT_DATE_FILTER = DATE_FILTER_OFF

# How the date filter treats photos that have no EXIF capture date.
#   use_uploaded_at - fall back to the upload date (keeps filters meaningful)
#   include         - keep undated photos (legacy behaviour for windows)
#   exclude         - drop undated photos entirely
MISSING_DATE_USE_UPLOADED = "use_uploaded_at"
MISSING_DATE_INCLUDE = "include"
MISSING_DATE_EXCLUDE = "exclude"

MISSING_DATE_OPTIONS = [
    MISSING_DATE_USE_UPLOADED,
    MISSING_DATE_INCLUDE,
    MISSING_DATE_EXCLUDE,
]
DEFAULT_MISSING_DATE_MODE = MISSING_DATE_USE_UPLOADED

DEFAULT_SLIDE_INTERVAL = 60
DEFAULT_REFRESH_HOURS = 24
DEFAULT_FILL_MODE = FILL_BLUR
DEFAULT_ORIENTATION_MISMATCH_MODE = ORIENTATION_MISMATCH_PAIR
DEFAULT_ORDER_MODE = ORDER_RANDOM
DEFAULT_ASPECT_RATIO = "16:9"
DEFAULT_PAIR_DIVIDER_PX = 8
DEFAULT_PAIR_DIVIDER_COLOR = "#FFFFFF"
DEFAULT_RECURSIVE = True
# Per-album download cache. Multiple albums add up: 4 × 150 MB = 600 MB
# of just-in-case downloaded JPEGs. 75 MB caches roughly 10-20 photos
# at typical Google Photos resolutions, which is enough for the preload
# path. Users with one album and lots of RAM can bump this via the
# Image cache size number entity.
DEFAULT_IMAGE_CACHE_MB = 75

MAX_RESOLUTION_OPTIONS = ["480p", "720p", "1080p", "1440p", "4K (2160p)", "original"]
DEFAULT_MAX_RESOLUTION = "1080p"
MAX_RESOLUTION_SHORT_EDGE: dict[str, int | None] = {
    "480p": 480,
    "720p": 720,
    "1080p": 1080,
    "1440p": 1440,
    "4K (2160p)": 2160,
    "original": None,
}

PUBLICALBUM_ENDPOINT = "https://www.publicalbum.org/api/v2/webapp/embed-player/jsonrpc"

SERVICE_NEXT_SLIDE = "next_slide"
SERVICE_REFRESH_ALBUM = "refresh_album"
ATTR_ENTRY_ID = "entry_id"
