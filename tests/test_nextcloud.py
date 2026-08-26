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


# ── parse_share_link (public album link) ───────────────────────────────────

def test_parse_share_link_pretty_url():
    out = nc.parse_share_link("https://cloud.example.com/apps/photos/public/AbC123")
    assert out == ("https://cloud.example.com", "AbC123")


def test_parse_share_link_trailing_slash():
    out = nc.parse_share_link("https://cloud.example.com/apps/photos/public/AbC123/")
    assert out == ("https://cloud.example.com", "AbC123")


def test_parse_share_link_index_php_form():
    out = nc.parse_share_link(
        "https://cloud.example.com/index.php/apps/photos/public/AbC123"
    )
    assert out == ("https://cloud.example.com", "AbC123")


def test_parse_share_link_subdirectory_install():
    out = nc.parse_share_link(
        "https://example.com/nextcloud/apps/photos/public/AbC123"
    )
    assert out == ("https://example.com/nextcloud", "AbC123")


def test_parse_share_link_with_query_string():
    out = nc.parse_share_link(
        "https://cloud.example.com/apps/photos/public/AbC123?foo=bar"
    )
    assert out == ("https://cloud.example.com", "AbC123")


def test_parse_share_link_rejects_non_matching_url():
    assert nc.parse_share_link("https://cloud.example.com/s/AbC123") is None
    assert nc.parse_share_link("not a url") is None
    assert nc.parse_share_link("") is None
    assert nc.parse_share_link(None) is None


# ── dav_root_public / build_image_url_public / build_preview_url_public ────

def test_dav_root_public():
    assert nc.dav_root_public("https://cloud.example.com", "AbC123") == (
        "https://cloud.example.com/remote.php/dav/photospublic/AbC123/"
    )


def test_build_image_url_public():
    url = nc.build_image_url_public(
        "https://cloud.example.com", "AbC123", "photo one.jpg"
    )
    assert url == (
        "https://cloud.example.com/remote.php/dav/photospublic/AbC123/photo%20one.jpg"
    )


def test_build_preview_url_public_default_size():
    url = nc.build_preview_url_public("https://cloud.example.com", "AbC123", "456")
    assert url == (
        "https://cloud.example.com/index.php/apps/photos/api/v1/publicPreview/456"
        "?token=AbC123&x=1024&y=1024"
    )


def test_build_preview_url_public_custom_size():
    url = nc.build_preview_url_public(
        "https://cloud.example.com", "AbC123", "456", px=256
    )
    assert "x=256&y=256" in url


# ── parse_propfind_response: real-server multi-propstat root regression ────
# Shape verified against a real Nextcloud server (28.x-era): the root
# collection's <d:response> carries *two* propstats - one 200 with just
# resourcetype, and a second 404 Not Found for props a folder can't answer
# (getcontenttype/getcontentlength/getlastmodified/oc:fileid). A parser that
# naively grabs the first <d:prop> it finds per response (instead of picking
# the 200 propstat) would silently work for this shape too, but one that
# grabs the *last* prop block, or merges both, would not - this pins the real
# multi-propstat structure so a regression stays caught. Exercises the shared
# parse_propfind_response, so it protects both auth modes.

_REAL_SHAPE_MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns"><d:response><d:href>/remote.php/dav/photospublic/AbC123/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat><d:propstat><d:prop><d:getcontenttype/><d:getcontentlength/><d:getlastmodified/><oc:fileid/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response><d:response><d:href>/remote.php/dav/photospublic/AbC123/12345-20260518_190350.jpg</d:href><d:propstat><d:prop><d:getcontenttype>image/jpeg</d:getcontenttype><d:getcontentlength>4424803</d:getcontentlength><d:getlastmodified>Mon, 18 May 2026 17:03:51 GMT</d:getlastmodified><d:resourcetype/><oc:fileid>12345</oc:fileid></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>
"""


def test_parse_propfind_real_server_shape_multi_propstat_root():
    root = "https://cloud.example.com/remote.php/dav/photospublic/AbC123/"
    items = nc.parse_propfind_response(_REAL_SHAPE_MULTISTATUS, root)
    assert len(items) == 1
    photo = items[0]
    assert photo["filename"] == "12345-20260518_190350.jpg"
    assert photo["content_type"] == "image/jpeg"
    assert photo["size"] == 4424803
    assert photo["file_id"] == "12345"


# ── nc:original-name (Photos albums prefix DAV names with "{fileid}-") ─────
# CollectionPhoto::getName() in nextcloud/photos returns "{fileId}-{name}", so
# the DAV path segment is not presentable. nc:original-name carries the real
# filename; the href-derived name stays as-is because URLs are built from it.

_ORIGINAL_NAME_MULTISTATUS = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns"><d:response><d:href>/remote.php/dav/photospublic/AbC123/12345-20260518_190350.jpg</d:href><d:propstat><d:prop><d:getcontenttype>image/jpeg</d:getcontenttype><d:getcontentlength>42</d:getcontentlength><d:resourcetype/><oc:fileid>12345</oc:fileid><nc:original-name>Beach day.jpg</nc:original-name></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>
"""


def test_parse_propfind_prefers_original_name_for_display():
    root = "https://cloud.example.com/remote.php/dav/photospublic/AbC123/"
    items = nc.parse_propfind_response(_ORIGINAL_NAME_MULTISTATUS, root)
    assert len(items) == 1
    photo = items[0]
    assert photo["display_name"] == "Beach day.jpg"
    # The DAV segment is untouched so href/URL building still works.
    assert photo["filename"] == "12345-20260518_190350.jpg"


def test_parse_propfind_display_name_falls_back_to_filename():
    root = "https://cloud.example.com/remote.php/dav/photospublic/AbC123/"
    items = nc.parse_propfind_response(_REAL_SHAPE_MULTISTATUS, root)
    assert items[0]["display_name"] == "12345-20260518_190350.jpg"


def test_propfind_body_requests_original_name():
    assert b"<nc:original-name/>" in nc._PROPFIND_BODY
    assert b'xmlns:nc="http://nextcloud.org/ns"' in nc._PROPFIND_BODY
