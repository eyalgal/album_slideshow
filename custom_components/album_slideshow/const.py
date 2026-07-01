DOMAIN = "album_slideshow"

CONF_PROVIDER = "provider"
CONF_ALBUM_URL = "album_url"
CONF_ALBUM_NAME = "album_name"
CONF_LOCAL_PATH = "local_path"
CONF_RECURSIVE = "recursive"
CONF_IMAGE_CACHE_MB = "image_cache_mb"
# Local-folder option: when True (default) the coordinator does best-effort
# reverse geocoding of EXIF GPS coordinates via the public Nominatim
# (OpenStreetMap) endpoint and exposes a human-readable ``location``
# attribute. Users with privacy concerns can disable this from the
# integration's options dialog.
CONF_REVERSE_GEOCODE = "reverse_geocode"
DEFAULT_REVERSE_GEOCODE = True

PROVIDER_GOOGLE_SHARED = "google_shared"
PROVIDER_LOCAL_FOLDER = "local_folder"

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
