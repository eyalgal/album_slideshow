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


# ── new-format parsing / backend detection ─────────────────────────────────

def test_parse_share_link_new_photos_url():
    assert ic.parse_share_link(
        "https://photos.icloud.com/shared/album/0c8RHcRtNHGlSsmr5YxG3JhqQ"
    ) == "0c8RHcRtNHGlSsmr5YxG3JhqQ"


def test_parse_share_link_new_url_with_query():
    assert ic.parse_share_link(
        "https://photos.icloud.com/shared/album/0c8RHcRtNHGlSsmr5YxG3JhqQ?foo=bar"
    ) == "0c8RHcRtNHGlSsmr5YxG3JhqQ"


def test_parse_share_link_new_token_with_dash_underscore():
    # CloudKit short GUIDs use a URL-safe alphabet and may contain - and _.
    assert ic.parse_share_link(
        "https://photos.icloud.com/shared/album/045YeI20-8u3X31bBPD5z9B_A/"
    ) == "045YeI20-8u3X31bBPD5z9B_A"


def test_detect_backend_new_vs_legacy():
    assert ic.detect_backend(
        "https://photos.icloud.com/shared/album/0c8RHcRtNHGlSsmr5YxG3JhqQ"
    ) == ic.BACKEND_CLOUDKIT
    assert ic.detect_backend(
        "https://www.icloud.com/sharedalbum/#B2XJtdOXmGiafRQ"
    ) == ic.BACKEND_SHAREDSTREAMS
    # Bare tokens default to the legacy backend.
    assert ic.detect_backend("B2XJtdOXmGiafRQ") == ic.BACKEND_SHAREDSTREAMS


def test_parse_share_returns_token_and_backend():
    assert ic.parse_share(
        "https://photos.icloud.com/shared/album/0c8RHcRtNHGlSsmr5YxG3JhqQ"
    ) == ("0c8RHcRtNHGlSsmr5YxG3JhqQ", ic.BACKEND_CLOUDKIT)
    assert ic.parse_share("not a link!!") is None


# ── CloudKit helpers ───────────────────────────────────────────────────────

def _ck_field(value, ftype="STRING"):
    return {"value": value, "type": ftype}


def _ck_res(url):
    return _ck_field({"downloadURL": url, "fileChecksum": "x"}, "ASSETID")


def _ck_master(record_name, fields):
    return {"recordType": "CPLMaster", "recordName": record_name, "fields": fields}


def _ck_asset(record_name, master_name, extra=None):
    fields = {
        "masterRef": _ck_field({"recordName": master_name}, "REFERENCE"),
        "assetDate": _ck_field(1470291599000, "TIMESTAMP"),
    }
    fields.update(extra or {})
    return {"recordType": "CPLAsset", "recordName": record_name, "fields": fields}


def test_ck_decode_filename():
    # base64 of "S8iqQQl.jpg"
    fields = {"filenameEnc": _ck_field("UzhpcVFRbC5qcGc=", "ENCRYPTED_BYTES")}
    assert ic._ck_decode_filename(fields) == "S8iqQQl.jpg"
    assert ic._ck_decode_filename({}) is None


def test_build_ck_image_url_substitutes_filename():
    url = "https://cvws-h2.icloud-content.com/B/abc/${f}?o=Av"
    assert ic.build_ck_image_url(url, "My Photo.jpg") == (
        "https://cvws-h2.icloud-content.com/B/abc/My%20Photo.jpg?o=Av"
    )
    assert ic.build_ck_image_url(url, None).endswith("/image?o=Av")
    assert ic.build_ck_image_url(None) is None


def test_pick_ck_resource_full_and_preview():
    fields = {
        "itemType": _ck_field("public.jpeg"),
        "resJPEGThumbRes": _ck_res("https://x/thumb/${f}"),
        "resJPEGThumbWidth": _ck_field(500, "INT64"),
        "resJPEGMedRes": _ck_res("https://x/med/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
    }
    full = ic.pick_ck_resource(fields, "full")
    preview = ic.pick_ck_resource(fields, "preview")
    assert full[0] == "https://x/med/${f}"
    assert preview[0] == "https://x/thumb/${f}"


def test_pick_ck_resource_skips_unsafe_original():
    # HEIC original is not browser-safe, so only the JPEG derivative is used.
    fields = {
        "itemType": _ck_field("public.heic"),
        "resJPEGMedRes": _ck_res("https://x/med/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
        "resOriginalRes": _ck_res("https://x/orig/${f}"),
        "resOriginalWidth": _ck_field(4000, "INT64"),
    }
    assert ic.pick_ck_resource(fields, "full")[0] == "https://x/med/${f}"


def test_pick_ck_resource_none_when_no_resources():
    assert ic.pick_ck_resource({"itemType": _ck_field("public.jpeg")}, "full") is None


def test_parse_ck_records_joins_master_and_asset():
    master = _ck_master("M1", {
        "itemType": _ck_field("public.jpeg"),
        "filenameEnc": _ck_field("UzhpcVFRbC5qcGc=", "ENCRYPTED_BYTES"),
        "resJPEGMedRes": _ck_res("https://x/med/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
        "resJPEGMedHeight": _ck_field(1500, "INT64"),
    })
    asset = _ck_asset("A1", "M1")
    out = ic.parse_ck_records([master, asset], "full")
    assert len(out) == 1
    item = out[0]
    assert item["source_id"] == "M1"
    assert item["url"] == "https://x/med/S8iqQQl.jpg"
    assert item["width"] == 2000 and item["height"] == 1500
    assert item["captured_at"] == 1470291599000


def test_parse_ck_records_skips_hidden_and_video():
    img_master = _ck_master("M1", {
        "itemType": _ck_field("public.jpeg"),
        "resJPEGMedRes": _ck_res("https://x/med/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
    })
    vid_master = _ck_master("M2", {
        "itemType": _ck_field("com.apple.quicktime-movie"),
        "resJPEGMedRes": _ck_res("https://x/vid/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
    })
    hidden = _ck_asset("A0", "M1", {"isHidden": _ck_field(1, "INT64")})
    good = _ck_asset("A1", "M1")
    video = _ck_asset("A2", "M2")
    out = ic.parse_ck_records([img_master, vid_master, hidden, good, video], "full")
    assert [i["source_id"] for i in out] == ["M1"]


def test_parse_ck_records_dedupes_by_master():
    master = _ck_master("M1", {
        "itemType": _ck_field("public.jpeg"),
        "resJPEGMedRes": _ck_res("https://x/med/${f}"),
        "resJPEGMedWidth": _ck_field(2000, "INT64"),
    })
    out = ic.parse_ck_records(
        [master, _ck_asset("A1", "M1"), _ck_asset("A2", "M1")], "full"
    )
    assert len(out) == 1
