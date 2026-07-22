from __future__ import annotations

import base64

from custom_components.album_slideshow import nextcloud as nc


# ── normalize_base_url ─────────────────────────────────────────────────────

def test_normalize_base_url_strips_trailing_slash():
    assert nc.normalize_base_url("https://cloud.example.com/") == "https://cloud.example.com"


def test_normalize_base_url_strips_dav_suffix():
    assert nc.normalize_base_url("https://cloud.example.com/remote.php/dav") == "https://cloud.example.com"
    assert nc.normalize_base_url("https://cloud.example.com/remote.php/webdav/") == "https://cloud.example.com"


def test_normalize_base_url_keeps_subdirectory_install():
    assert nc.normalize_base_url("https://example.com/nextcloud/") == "https://example.com/nextcloud"


# ── normalize_folder ───────────────────────────────────────────────────────

def test_normalize_folder():
    assert nc.normalize_folder("/Photos/Family/") == "Photos/Family"
    assert nc.normalize_folder("Photos//Family") == "Photos/Family"
    assert nc.normalize_folder("") == ""
    assert nc.normalize_folder(None) == ""


# ── dav_root ───────────────────────────────────────────────────────────────

def test_dav_root_with_folder():
    assert nc.dav_root("https://cloud.example.com", "alice", "Photos/Family") == (
        "https://cloud.example.com/remote.php/dav/files/alice/Photos/Family/"
    )


def test_dav_root_root_folder():
    assert nc.dav_root("https://cloud.example.com", "alice", "") == (
        "https://cloud.example.com/remote.php/dav/files/alice/"
    )


def test_dav_root_encodes_spaces_but_keeps_slashes():
    root = nc.dav_root("https://cloud.example.com", "alice", "My Photos/2026 Trip")
    assert root == (
        "https://cloud.example.com/remote.php/dav/files/alice/My%20Photos/2026%20Trip/"
    )


# ── build_preview_url ──────────────────────────────────────────────────────

def test_build_preview_url():
    url = nc.build_preview_url("https://cloud.example.com", "12345", 1920)
    assert url == (
        "https://cloud.example.com/index.php/core/preview?fileId=12345&x=1920&y=1920&a=1"
    )


# ── basic_auth_header ──────────────────────────────────────────────────────

def test_basic_auth_header():
    header = nc.basic_auth_header("alice", "app-pass")
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header[len("Basic ") :]).decode()
    assert decoded == "alice:app-pass"


# ── _looks_like_image / _mtime_to_epoch_ms ─────────────────────────────────

def test_looks_like_image_by_content_type():
    assert nc._looks_like_image("image/jpeg", "x") is True
    assert nc._looks_like_image("text/html", "x.jpg") is False


def test_looks_like_image_by_extension_when_no_content_type():
    assert nc._looks_like_image(None, "photo.HEIC") is True
    assert nc._looks_like_image(None, "notes.txt") is False


def test_mtime_to_epoch_ms():
    # Fri, 09 Jul 2026 13:34:25 GMT
    ms = nc._mtime_to_epoch_ms("Thu, 01 Jan 1970 00:00:01 GMT")
    assert ms == 1000
    assert nc._mtime_to_epoch_ms(None) is None
    assert nc._mtime_to_epoch_ms("garbage") is None


# ── parse_propfind_response ─────────────────────────────────────────────────

_ROOT = "https://cloud.example.com/remote.php/dav/files/alice/Photos/"

_MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/files/alice/Photos/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/Photos/beach%20day.jpg</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontenttype>image/jpeg</d:getcontenttype>
        <d:getcontentlength>2048</d:getcontentlength>
        <d:getlastmodified>Thu, 01 Jan 1970 00:00:01 GMT</d:getlastmodified>
        <d:resourcetype/>
        <oc:fileid>9001</oc:fileid>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/Photos/Subfolder/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/alice/Photos/notes.txt</d:href>
    <d:propstat>
      <d:prop>
        <d:getcontenttype>text/plain</d:getcontenttype>
        <d:resourcetype/>
        <oc:fileid>9002</oc:fileid>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""


def test_parse_propfind_skips_root_folders_and_non_images():
    photos = nc.parse_propfind_response(_MULTISTATUS, _ROOT)
    assert len(photos) == 1
    p = photos[0]
    assert p["filename"] == "beach day.jpg"
    assert p["content_type"] == "image/jpeg"
    assert p["size"] == 2048
    assert p["mtime_ms"] == 1000
    assert p["file_id"] == "9001"
    assert p["href"] == (
        "https://cloud.example.com/remote.php/dav/files/alice/Photos/beach%20day.jpg"
    )


def test_parse_propfind_bad_xml_returns_empty():
    assert nc.parse_propfind_response("not xml", _ROOT) == []


def test_parse_propfind_empty_returns_empty():
    empty = (
        '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"></d:multistatus>'
    )
    assert nc.parse_propfind_response(empty, _ROOT) == []
