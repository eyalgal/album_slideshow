from __future__ import annotations

from custom_components.album_slideshow.coordinator import (
    MediaItem,
    _enrich_missing_dates,
    _photo_base_key,
)


# -- _photo_base_key --------------------------------------------------------

def test_base_key_strips_size_suffix():
    a = _photo_base_key("https://lh3.googleusercontent.com/abc123=w1920-h1080")
    b = _photo_base_key("https://lh3.googleusercontent.com/abc123=w640-h480-no")
    assert a == b == "https://lh3.googleusercontent.com/abc123"


def test_base_key_strips_query_string():
    a = _photo_base_key("https://lh3.googleusercontent.com/abc123=w1920-h1080")
    b = _photo_base_key("https://lh3.googleusercontent.com/abc123?authuser=0")
    assert a == b == "https://lh3.googleusercontent.com/abc123"


def test_base_key_handles_none_and_empty():
    assert _photo_base_key(None) is None
    assert _photo_base_key("") is None


# -- _enrich_missing_dates --------------------------------------------------

def _item(base: str, captured=None, uploaded=None, size="=w1920-h1080"):
    return MediaItem(
        url=f"{base}{size}",
        width=None,
        height=None,
        mime_type=None,
        filename=None,
        captured_at=captured,
        uploaded_at=uploaded,
    )


def test_enrich_backfills_dates_from_scraped_twin():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, size="=w640-h480")]  # publicalbum: no dates, different size
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 1
    assert api[0].captured_at == 1000
    assert api[0].uploaded_at == 2000


def test_enrich_does_not_overwrite_existing_dates():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, captured=111, uploaded=222)]
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 0
    assert api[0].captured_at == 111
    assert api[0].uploaded_at == 222


def test_enrich_fills_only_missing_field():
    base = "https://lh3.googleusercontent.com/photo1"
    api = [_item(base, captured=111, uploaded=None)]
    scraped = [_item(base, captured=1000, uploaded=2000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 1
    # captured_at is kept, only the missing uploaded_at is filled.
    assert api[0].captured_at == 111
    assert api[0].uploaded_at == 2000


def test_enrich_leaves_unmatched_items_untouched():
    api = [_item("https://lh3.googleusercontent.com/only_in_api")]
    scraped = [_item("https://lh3.googleusercontent.com/only_in_scrape", captured=1000)]

    n = _enrich_missing_dates(api, scraped)

    assert n == 0
    assert api[0].captured_at is None
    assert api[0].uploaded_at is None


def test_enrich_noop_when_a_source_empty():
    scraped = [_item("https://lh3.googleusercontent.com/photo1", captured=1000)]
    assert _enrich_missing_dates([], scraped) == 0
    assert _enrich_missing_dates(scraped, []) == 0


# -- ENRICHING_PROVIDERS ----------------------------------------------------
# The Enrichment progress sensor is created from this tuple, so it has to stay
# in step with the providers the coordinator actually schedules work for.

def test_enriching_providers_matches_documented_set():
    from custom_components.album_slideshow.const import (
        ENRICHING_PROVIDERS,
        PROVIDER_ENTE,
        PROVIDER_GOOGLE_SHARED,
        PROVIDER_IMMICH,
        PROVIDER_LOCAL_FOLDER,
        PROVIDER_MEDIA_SOURCE,
        PROVIDER_NEXTCLOUD,
    )

    assert set(ENRICHING_PROVIDERS) == {
        PROVIDER_LOCAL_FOLDER,
        PROVIDER_IMMICH,
        PROVIDER_NEXTCLOUD,
        PROVIDER_ENTE,
    }
    # Providers with no metadata to enrich must stay out, or they'd get a
    # progress sensor that never moves.
    assert PROVIDER_GOOGLE_SHARED not in ENRICHING_PROVIDERS
    assert PROVIDER_MEDIA_SOURCE not in ENRICHING_PROVIDERS


def test_sensor_platform_uses_the_shared_enrichment_tuple():
    import pathlib

    src = pathlib.Path(
        "custom_components/album_slideshow/sensor.py"
    ).read_text()
    assert "coordinator.provider in ENRICHING_PROVIDERS" in src


# -- publicalbum.org video filtering (#26) ----------------------------------
# publicalbum.org returns mimetype/mediaMetadata as null, verified against a
# real shared album, so it can only drop videos by reusing the scraper's keys.

def test_looks_like_video_accepts_lowercase_mimetype():
    from custom_components.album_slideshow.coordinator import _looks_like_video

    # publicalbum.org spells it lowercase; Google's own shapes use camelCase.
    assert _looks_like_video({"mimetype": "video/mp4"}) is True
    assert _looks_like_video({"mimeType": "video/mp4"}) is True
    assert _looks_like_video({"mimetype": "image/jpeg"}) is False


def test_looks_like_video_cannot_detect_a_publicalbum_video():
    from custom_components.album_slideshow.coordinator import _looks_like_video

    # This is the exact shape a real album returned for a video: no signal at
    # all. It documents why the media-key fallback exists.
    raw = {
        "id": "AF1QipOVuW0YZ1YRJ-dHFC8FZ1wp0JEjwAHdYIG2CF_v",
        "description": None,
        "url": "https://lh3.googleusercontent.com/pw/AP1GczPO7xue=w1920-h1080",
        "mimetype": None,
        "mediaMetadata": None,
    }
    assert _looks_like_video(raw) is False
