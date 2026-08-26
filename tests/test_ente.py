from __future__ import annotations

import base64
import json

import pytest

from custom_components.album_slideshow import ente as en

nacl = pytest.importorskip("nacl.bindings")


# ── base58 ─────────────────────────────────────────────────────────────────

def test_b58decode_known_vector():
    # The canonical Bitcoin base58 test vector.
    assert en.b58decode("StV1DL6CwTryKyV") == b"hello world"


def test_b58decode_preserves_leading_zero_bytes():
    assert en.b58decode("1112") == b"\x00\x00\x00\x01"


def test_b58decode_rejects_invalid_alphabet():
    # '0', 'O', 'I' and 'l' are deliberately absent from base58.
    for bad in ("0", "O", "I", "l", "abc!"):
        with pytest.raises(ValueError):
            en.b58decode(bad)


def test_b58decode_rejects_empty():
    with pytest.raises(ValueError):
        en.b58decode("")


# ── parse_share_link ───────────────────────────────────────────────────────

def _b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = ""
    while num:
        num, rem = divmod(num, 58)
        out = en._B58_ALPHABET[rem] + out
    pad = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * pad + out


_KEY = bytes(range(32))
_KEY_B58 = _b58encode(_KEY)


def test_parse_share_link_hosted():
    share = en.parse_share_link(f"https://albums.ente.io/?t=TOKEN123#{_KEY_B58}")
    assert share is not None
    assert share.access_token == "TOKEN123"
    assert share.collection_key == _KEY
    assert share.album_origin == "https://albums.ente.io"


def test_parse_share_link_self_hosted_origin_is_kept():
    share = en.parse_share_link(f"https://photos.example.com/?t=abc#{_KEY_B58}")
    assert share is not None
    assert share.album_origin == "https://photos.example.com"


def test_parse_share_link_rejects_missing_fragment():
    # The fragment is the decryption key; without it the album is unreadable.
    assert en.parse_share_link("https://albums.ente.io/?t=TOKEN123") is None


def test_parse_share_link_rejects_missing_token():
    assert en.parse_share_link(f"https://albums.ente.io/#{_KEY_B58}") is None


def test_parse_share_link_rejects_wrong_key_length():
    short = _b58encode(b"\x01" * 16)
    assert en.parse_share_link(f"https://albums.ente.io/?t=T#{short}") is None


def test_parse_share_link_rejects_garbage():
    assert en.parse_share_link("not a url") is None
    assert en.parse_share_link("") is None
    assert en.parse_share_link(None) is None
    assert en.parse_share_link("ftp://albums.ente.io/?t=a#b") is None


# ── crypto round trips against real libsodium ──────────────────────────────

def _secretbox(plain: bytes, key: bytes) -> tuple[str, str]:
    nonce = b"\x07" * 24
    cipher = nacl.crypto_secretbox(plain, nonce, key)
    return base64.b64encode(cipher).decode(), base64.b64encode(nonce).decode()


def _secretstream(plain: bytes, key: bytes, chunk: int = 4 * 1024 * 1024) -> tuple[bytes, bytes]:
    state = nacl.crypto_secretstream_xchacha20poly1305_state()
    header = nacl.crypto_secretstream_xchacha20poly1305_init_push(state, key)
    parts = []
    offset = 0
    while offset < len(plain):
        block = plain[offset : offset + chunk]
        offset += chunk
        tag = (
            nacl.crypto_secretstream_xchacha20poly1305_TAG_FINAL
            if offset >= len(plain)
            else nacl.crypto_secretstream_xchacha20poly1305_TAG_MESSAGE
        )
        parts.append(
            nacl.crypto_secretstream_xchacha20poly1305_push(state, block, tag=tag)
        )
    return b"".join(parts), header


def test_decrypt_secretbox_round_trip():
    key = b"\x02" * 32
    cipher, nonce = _secretbox(b"a file key", key)
    assert en.decrypt_secretbox(cipher, nonce, key) == b"a file key"


def test_decrypt_secretstream_round_trip():
    key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    cipher, header = _secretstream(b"some jpeg bytes", key)
    assert en.decrypt_secretstream(cipher, header, key) == b"some jpeg bytes"


def test_decrypt_secretstream_spans_multiple_chunks():
    # Ente encrypts in 4 MiB chunks; the pull state must carry across them.
    key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    payload = bytes(range(256)) * 40_000  # >4 MiB
    cipher, header = _secretstream(payload, key)
    assert en.decrypt_secretstream(cipher, header, key) == payload


def _make_file(file_key: bytes, collection_key: bytes, metadata: dict, **extra):
    enc_key, nonce = _secretbox(file_key, collection_key)
    meta_cipher, meta_header = _secretstream(
        json.dumps(metadata).encode(), file_key
    )
    item = {
        "id": 42,
        "encryptedKey": enc_key,
        "keyDecryptionNonce": nonce,
        "metadata": {
            "encryptedData": base64.b64encode(meta_cipher).decode(),
            "decryptionHeader": base64.b64encode(meta_header).decode(),
        },
        "file": {"decryptionHeader": base64.b64encode(b"f" * 24).decode()},
        "thumbnail": {"decryptionHeader": base64.b64encode(b"t" * 24).decode()},
    }
    item.update(extra)
    return item


def test_decrypt_file_key_and_metadata():
    collection_key = b"\x03" * 32
    file_key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    raw = _make_file(file_key, collection_key, {"title": "a.jpg", "fileType": 0})

    assert en.decrypt_file_key(raw, collection_key) == file_key
    meta = en.decrypt_metadata(raw, file_key)
    assert meta["title"] == "a.jpg"
    assert en.is_image(meta) is True


def test_decrypt_metadata_missing_blob_returns_empty():
    assert en.decrypt_metadata({}, b"\x00" * 32) == {}
    assert en.decrypt_metadata({"metadata": {}}, b"\x00" * 32) == {}


def test_decrypt_magic_metadata_is_optional_and_never_raises():
    # Undecryptable magic metadata must not sink the whole photo.
    bad = {"pubMagicMetadata": {"data": "!!!", "header": "!!!"}}
    assert en.decrypt_magic_metadata(bad, b"\x00" * 32) == {}


def test_decrypt_album_name_from_encrypted_name():
    key = b"\x04" * 32
    cipher, nonce = _secretbox("Summer 2026".encode(), key)
    collection = {"encryptedName": cipher, "nameDecryptionNonce": nonce}
    assert en.decrypt_album_name(collection, key) == "Summer 2026"


def test_decrypt_album_name_prefers_plaintext_and_tolerates_junk():
    assert en.decrypt_album_name({"name": " Trip "}, b"\x00" * 32) == "Trip"
    assert en.decrypt_album_name(None, b"\x00" * 32) is None
    assert en.decrypt_album_name({}, b"\x00" * 32) is None


# ── diff parsing ───────────────────────────────────────────────────────────

def test_parse_diff_page_filters_deleted_and_tracks_updation_time():
    payload = {
        "diff": [
            {"id": 1, "encryptedKey": "k", "updationTime": 100},
            {"id": 2, "encryptedKey": "k", "updationTime": 300, "isDeleted": True},
            {"id": 3, "encryptedKey": "k", "updationTime": 200},
        ],
        "hasMore": True,
    }
    files, has_more, latest = en.parse_diff_page(payload)
    assert [f["id"] for f in files] == [1, 3]
    assert has_more is True
    # Deleted rows still advance the cursor, or pagination would loop.
    assert latest == 300


def test_parse_diff_page_skips_entries_without_key():
    payload = {"diff": [{"id": 1}, {"encryptedKey": "k"}], "hasMore": False}
    files, has_more, _ = en.parse_diff_page(payload)
    assert files == []
    assert has_more is False


def test_parse_diff_page_handles_garbage():
    assert en.parse_diff_page(None) == ([], False, 0)
    assert en.parse_diff_page({"diff": "nope"}) == ([], False, 0)


# ── media field mapping ────────────────────────────────────────────────────

def test_build_media_fields_converts_microseconds_to_milliseconds():
    fields = en.build_media_fields(
        {"title": "a.jpg", "creationTime": 1_700_000_000_000_000}, {}
    )
    assert fields["captured_at"] == 1_700_000_000_000
    assert fields["filename"] == "a.jpg"


def test_build_media_fields_prefers_public_magic_metadata():
    metadata = {
        "title": "IMG_1.jpg",
        "creationTime": 1_000_000_000_000_000,
        "latitude": 1.5,
        "longitude": 2.5,
    }
    magic = {
        "editedName": "Sunset.jpg",
        "editedTime": 2_000_000_000_000_000,
        "caption": "Golden hour",
        "w": 4032,
        "h": 3024,
    }
    fields = en.build_media_fields(metadata, magic)
    assert fields["filename"] == "Sunset.jpg"
    assert fields["captured_at"] == 2_000_000_000_000
    assert fields["description"] == "Golden hour"
    assert fields["width"] == 4032
    # Magic metadata carried no coords, so the originals survive.
    assert (fields["latitude"], fields["longitude"]) == (1.5, 2.5)


def test_build_media_fields_drops_null_island_coordinates():
    fields = en.build_media_fields({"latitude": 0, "longitude": 0}, {})
    assert fields["latitude"] is None
    assert fields["longitude"] is None


def test_build_media_fields_ignores_empty_timestamps():
    fields = en.build_media_fields({"creationTime": 0}, {})
    assert fields["captured_at"] is None


def test_is_image_rejects_video_and_live_photo():
    assert en.is_image({"fileType": en.FILE_TYPE_VIDEO}) is False
    assert en.is_image({"fileType": en.FILE_TYPE_LIVE_PHOTO}) is False
    assert en.is_image({}) is False


# ── synthetic urls ─────────────────────────────────────────────────────────

def test_item_url_round_trip():
    url = en.build_item_url(1234)
    assert url == "ente://1234"
    assert en.file_id_from_url(url) == "1234"


def test_file_id_from_url_ignores_other_schemes():
    assert en.file_id_from_url("https://example.com/a.jpg") is None
    assert en.file_id_from_url("ente://") is None


def test_normalize_api_origin_defaults_and_strips():
    assert en.normalize_api_origin("") == en.DEFAULT_API_ORIGIN
    assert en.normalize_api_origin(None) == en.DEFAULT_API_ORIGIN
    assert en.normalize_api_origin("https://api.example.com/") == "https://api.example.com"


# ── coordinator item building (decrypt -> MediaItem) ───────────────────────

from custom_components.album_slideshow.coordinator import _build_ente_item  # noqa: E402


def test_build_ente_item_maps_metadata_and_keeps_secrets_out_of_the_url():
    collection_key = b"\x05" * 32
    file_key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    raw = _make_file(
        file_key,
        collection_key,
        {
            "title": "IMG_9.jpg",
            "fileType": 0,
            "creationTime": 1_700_000_000_000_000,
            "latitude": 38.7,
            "longitude": -9.1,
        },
        info={"fileSize": 5555, "thumbSize": 111},
    )

    item, meta = _build_ente_item(raw, collection_key, want_preview=False)

    assert item.url == "ente://42"
    assert item.filename == "IMG_9.jpg"
    assert item.captured_at == 1_700_000_000_000
    assert (item.latitude, item.longitude) == (38.7, -9.1)
    assert item.byte_size == 5555
    # Metadata arrives complete, so no enrichment download is needed.
    assert item.exif_scanned is True
    # Neither the access token nor any key may appear in a user-visible URL.
    assert "ente://42" == item.url
    assert meta["key"] == file_key
    assert meta["thumbnail"] is False


def test_build_ente_item_preview_mode_uses_thumbnail_header_and_size():
    collection_key = b"\x06" * 32
    file_key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    raw = _make_file(
        file_key,
        collection_key,
        {"title": "a.jpg", "fileType": 0},
        info={"fileSize": 5555, "thumbSize": 111},
    )
    item, meta = _build_ente_item(raw, collection_key, want_preview=True)
    assert item.byte_size == 111
    assert meta["thumbnail"] is True
    assert meta["header"] == raw["thumbnail"]["decryptionHeader"]


def test_build_ente_item_skips_videos():
    collection_key = b"\x07" * 32
    file_key = nacl.crypto_secretstream_xchacha20poly1305_keygen()
    raw = _make_file(file_key, collection_key, {"title": "v.mp4", "fileType": 1})
    assert _build_ente_item(raw, collection_key, want_preview=False) is None
