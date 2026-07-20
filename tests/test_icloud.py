from __future__ import annotations

from custom_components.album_slideshow import icloud as ic


# ── parse_share_link ───────────────────────────────────────────────────────

def test_parse_share_link_full_url():
    assert ic.parse_share_link(
        "https://www.icloud.com/sharedalbum/#B2XJtdOXmGiafRQ"
    ) == "B2XJtdOXmGiafRQ"


def test_parse_share_link_bare_token():
    assert ic.parse_share_link("B2XJtdOXmGiafRQ") == "B2XJtdOXmGiafRQ"


def test_parse_share_link_trailing_slash_form():
    assert ic.parse_share_link(
        "https://www.icloud.com/sharedalbum/B2XJtdOXmGiafRQ/"
    ) == "B2XJtdOXmGiafRQ"


def test_parse_share_link_rejects_garbage():
    assert ic.parse_share_link("not a link!!") is None
    assert ic.parse_share_link("") is None
    assert ic.parse_share_link(None) is None


# ── partition_host / base_url ──────────────────────────────────────────────

def test_partition_host_b_token():
    # "2X" in base62 -> 62*... ; matches the live-verified p157 host.
    assert ic.partition_host("B2XJtdOXmGiafRQ") == "p157-sharedstreams.icloud.com"


def test_partition_host_a_token_uses_one_char():
    # A-prefixed tokens use only the single next char.
    host = ic.partition_host("A5abcdef")
    assert host.startswith("p") and host.endswith("-sharedstreams.icloud.com")


def test_base_url_default_and_override():
    assert ic.base_url("B2XJtdOXmGiafRQ") == (
        "https://p157-sharedstreams.icloud.com/B2XJtdOXmGiafRQ/sharedstreams"
    )
    assert ic.base_url("B2XJtdOXmGiafRQ", "p99-sharedstreams.icloud.com") == (
        "https://p99-sharedstreams.icloud.com/B2XJtdOXmGiafRQ/sharedstreams"
    )


# ── _to_epoch_ms ───────────────────────────────────────────────────────────

def test_to_epoch_ms():
    assert ic._to_epoch_ms("2025-02-22T00:52:13Z") == 1740185533000
    assert ic._to_epoch_ms(None) is None
    assert ic._to_epoch_ms("garbage") is None


# ── parse_webstream ────────────────────────────────────────────────────────

def _photo(guid, derivs):
    return {"photoGuid": guid, "derivatives": derivs}


def test_parse_webstream_keeps_valid_photos():
    payload = {
        "photos": [
            _photo("g1", {"342": {"checksum": "a"}}),
            _photo("g2", {}),            # no derivatives -> skipped
            {"derivatives": {"1": {}}},  # no guid -> skipped
            "not a dict",
        ]
    }
    out = ic.parse_webstream(payload)
    assert [p["photoGuid"] for p in out] == ["g1"]


def test_parse_webstream_bad_payload():
    assert ic.parse_webstream(None) == []
    assert ic.parse_webstream({}) == []


# ── pick_checksum ──────────────────────────────────────────────────────────

def test_pick_checksum_full_is_largest():
    photo = _photo("g", {
        "342": {"checksum": "small"},
        "2049": {"checksum": "big"},
    })
    assert ic.pick_checksum(photo, "full") == "big"
    assert ic.pick_checksum(photo, "preview") == "small"


def test_pick_checksum_single_derivative():
    photo = _photo("g", {"1024": {"checksum": "only"}})
    assert ic.pick_checksum(photo, "full") == "only"
    assert ic.pick_checksum(photo, "preview") == "only"


def test_pick_checksum_none_when_empty():
    assert ic.pick_checksum({"derivatives": {}}, "full") is None
    assert ic.pick_checksum({}, "full") is None


# ── build_image_url ────────────────────────────────────────────────────────

def test_build_image_url():
    item = {"url_location": "cvws.icloud-content.com", "url_path": "/S/x/IMG.JPG?o=Av"}
    assert ic.build_image_url(item) == (
        "https://cvws.icloud-content.com/S/x/IMG.JPG?o=Av"
    )


def test_build_image_url_missing_parts():
    assert ic.build_image_url({"url_location": "x"}) is None
    assert ic.build_image_url(None) is None


# ── parse_photo_meta ───────────────────────────────────────────────────────

def test_parse_photo_meta_date_and_caption():
    meta = ic.parse_photo_meta({
        "dateCreated": "2025-02-22T00:52:13Z",
        "caption": "Beach day",
    })
    assert meta["captured_at"] == 1740185533000
    assert meta["description"] == "Beach day"


def test_parse_photo_meta_blank_caption_omitted():
    meta = ic.parse_photo_meta({"dateCreated": "2025-02-22T00:52:13Z", "caption": "  "})
    assert "description" not in meta


def test_parse_photo_meta_no_location_ever():
    # Sanity: iCloud shared albums never carry GPS, so meta has no lat/long.
    meta = ic.parse_photo_meta({"dateCreated": "2025-02-22T00:52:13Z"})
    assert "latitude" not in meta and "longitude" not in meta
