"""Download policy regressions; mock transport, but use the real image decoder."""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from aiohttp import ClientResponseError, RequestInfo
from multidict import CIMultiDict
from PIL import Image, UnidentifiedImageError
import pytest
from yarl import URL

from custom_components.album_slideshow import camera


_ICLOUD_URL = "https://cvws.icloud-content.com/asset/IMG_5513.HEIC"


class _Content:
    def __init__(self, chunks):
        self.chunks = chunks
        self.reads = 0

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            self.reads += 1
            yield chunk


class _Response:
    def __init__(self, url, headers, chunks, status):
        self.url = URL(url)
        self.headers = CIMultiDict(headers)
        self.content = _Content(chunks)
        self.status = status
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _Hass:
    def __init__(self):
        self.data = {}
        self.verify_ssl = None

    async def async_add_executor_job(self, func, *args):
        return func(*args)


@pytest.fixture
def make_camera(monkeypatch):
    def make(
        response_url=_ICLOUD_URL, *, headers=None, chunks=(b"image-bytes",), status=200,
    ):
        response = _Response(
            response_url,
            headers if headers is not None else {"Content-Type": "application/octet-stream"},
            chunks,
            status,
        )
        requests = []

        def get(url, **kwargs):
            requests.append((url, kwargs))
            return response

        session = SimpleNamespace(get=get)

        def get_session(hass, verify_ssl=True):
            hass.verify_ssl = verify_ssl
            return session

        monkeypatch.setattr(camera, "async_get_clientsession", get_session)
        cam = camera.AlbumSlideshowCamera.__new__(camera.AlbumSlideshowCamera)
        cam.hass = _Hass()
        cam.coordinator = SimpleNamespace(image_request_headers=None)
        cam._download_cache = camera._DownloadCache(1024 * 1024)
        return cam, response, requests

    return make


@pytest.mark.parametrize("response_url", [
    "https://icloud-content.com/IMG_5513.HEIC",
    _ICLOUD_URL,
    "https://p123.sub.icloud-content.com/IMG_5513.HEIC",
    "https://CVWS.ICLOUD-CONTENT.COM/IMG_5513.HEIC",
])
def test_accepts_binary_images_from_icloud_hosts(make_camera, response_url):
    cam, response, _requests = make_camera(response_url)
    assert asyncio.run(cam._http_get(response_url)) == b"image-bytes"
    assert response.content.reads == 1
    assert response.closed


@pytest.mark.parametrize("response_url", [
    "https://unrelated.example/IMG_5513.HEIC",
    "https://noticloud-content.com/IMG_5513.HEIC",
    "https://icloud-content.com.attacker.example/IMG_5513.HEIC",
    "https://attacker.example/icloud-content.com/IMG_5513.HEIC",
    "https://attacker.example/?source=https://icloud-content.com/IMG_5513.HEIC",
    "https://icloud-content.com@attacker.example/IMG_5513.HEIC",
    "https://127.0.0.1/icloud-content.com/IMG_5513.HEIC",
])
def test_rejects_binary_images_from_other_hosts(make_camera, response_url):
    cam, response, _requests = make_camera(response_url)
    assert asyncio.run(cam._http_get(response_url)) is None
    assert response.content.reads == 0
    assert response.closed


def test_redirect_into_icloud_uses_final_response_hostname(make_camera):
    cam, _response, requests = make_camera()
    source_url = "https://www.icloud.com/image-redirect"
    assert asyncio.run(cam._http_get(source_url)) == b"image-bytes"
    assert requests == [(source_url, {"headers": None, "params": None})]


def test_tls_verification_follows_the_coordinator(make_camera):
    cam, _response, _requests = make_camera()
    asyncio.run(cam._http_get(_ICLOUD_URL))
    assert cam.hass.verify_ssl is True
    cam.coordinator = SimpleNamespace(
        image_request_headers=None, image_request_verify_ssl=False,
    )
    asyncio.run(cam._http_get(_ICLOUD_URL))
    assert cam.hass.verify_ssl is False


def test_fetch_time_params_are_sent_but_kept_out_of_error_logs(make_camera, caplog):
    source_url = "https://nas.example/ugreen/v5/photo/picture/stream?id=2"
    cam, response, requests = make_camera(source_url, headers={"Content-Type": "image/jpeg"})
    cam.coordinator = SimpleNamespace(
        image_request_headers=None, image_request_params={"ugk": "SECRET"},
    )
    assert asyncio.run(cam._http_get(source_url)) == b"image-bytes"
    assert requests[0][1]["params"] == {"ugk": "SECRET"}

    real_url = URL(source_url).update_query(ugk="SECRET")

    def raise_for_status():
        raise ClientResponseError(
            RequestInfo(real_url, "GET", CIMultiDict(), real_url), (), status=401,
        )

    response.raise_for_status = raise_for_status
    assert asyncio.run(cam._http_get(source_url)) is None
    assert "HTTP 401" in caplog.text
    assert "SECRET" not in caplog.text


def test_media_links_get_a_fresh_signature_for_each_download(make_camera):
    source_url = "http://ha.test:8123/immich/entry/asset/fullsize/image/jpeg"
    cam, _response, requests = make_camera(source_url, headers={"Content-Type": "image/jpeg"})
    signatures = iter(["FIRST", "SECOND"])
    cam.coordinator = SimpleNamespace(
        image_request_headers=None, media_link_signature=lambda url: next(signatures),
    )
    asyncio.run(cam._http_get(source_url))
    asyncio.run(cam._http_get(source_url))
    assert [request[0] for request in requests] == [source_url, source_url]
    assert [request[1]["params"] for request in requests] == [
        {"authSig": "FIRST"}, {"authSig": "SECOND"},
    ]


def test_redirect_away_from_icloud_does_not_inherit_binary_exception(make_camera):
    cam, response, _requests = make_camera("https://other.example/IMG_5513.HEIC")
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) is None
    assert response.content.reads == 0


def test_binary_content_type_is_case_insensitive_and_allows_parameters(make_camera):
    cam, _response, _requests = make_camera(
        headers={"content-type": " Application/Octet-Stream ; charset=binary"}
    )
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) == b"image-bytes"


@pytest.mark.parametrize("content_type", [
    "text/html", "application/json", "application/xml", "video/mp4",
    "application/octet-stream-extra",
])
def test_icloud_still_rejects_other_non_image_types(make_camera, content_type):
    cam, response, _requests = make_camera(headers={"Content-Type": content_type})
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) is None
    assert response.content.reads == 0


@pytest.mark.parametrize("headers", [
    {}, {"Content-Type": ""}, {"Content-Type": "image/jpeg"},
    {"Content-Type": "Image/PNG; charset=binary"},
])
def test_other_providers_keep_existing_image_header_policy(make_camera, headers):
    url = "https://photos.example/image"
    cam, _response, _requests = make_camera(url, headers=headers)
    assert asyncio.run(cam._http_get(url)) == b"image-bytes"


def test_binary_image_with_oversized_declared_length_is_not_read(monkeypatch, make_camera):
    monkeypatch.setattr(camera, "_MAX_DOWNLOAD_BYTES", 8)
    cam, response, _requests = make_camera(headers={
        "Content-Type": "application/octet-stream", "Content-Length": "9",
    })
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) is None
    assert response.content.reads == 0
    assert response.closed


@pytest.mark.parametrize("length", [None, "invalid", "5"])
def test_binary_image_stream_limit_stops_download(monkeypatch, make_camera, length):
    monkeypatch.setattr(camera, "_MAX_DOWNLOAD_BYTES", 8)
    headers = {"Content-Type": "application/octet-stream"}
    if length is not None:
        headers["Content-Length"] = length
    cam, response, _requests = make_camera(
        headers=headers, chunks=(b"12345", b"67890", b"not-read"),
    )
    assert asyncio.run(cam._fetch_bytes(_ICLOUD_URL)) is None
    assert response.content.reads == 2
    assert cam._download_cache.get(_ICLOUD_URL) is None
    assert response.closed


def test_binary_image_at_download_limit_is_accepted(monkeypatch, make_camera):
    monkeypatch.setattr(camera, "_MAX_DOWNLOAD_BYTES", 8)
    cam, _response, _requests = make_camera(
        headers={"Content-Type": "application/octet-stream", "Content-Length": "8"},
        chunks=(b"1234", b"5678"),
    )
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) == b"12345678"


def test_binary_image_http_errors_are_still_rejected(make_camera):
    cam, response, _requests = make_camera(status=403)
    assert asyncio.run(cam._http_get(_ICLOUD_URL)) is None
    assert response.content.reads == 0
    assert response.closed


def test_binary_image_reaches_real_decoder_and_renderer(make_camera):
    # The filename is opaque: JPEG bytes behind a HEIC URL still decode as JPEG.
    # This verifies MIME handling, not whether a HEIC codec is installed.
    buffer = io.BytesIO()
    with Image.new("RGB", (8, 4), "red") as source:
        source.save(buffer, format="JPEG")
    cam, _response, _requests = make_camera(chunks=(buffer.getvalue(),))
    item = camera.MediaItem(
        url=_ICLOUD_URL, width=8, height=4, mime_type=None, filename="IMG_5513.HEIC",
    )
    rendered, meta = asyncio.run(cam._compose_single(item, 16, 8, "contain"))
    assert rendered is not None
    try:
        assert rendered.size == (16, 8)
        assert camera.ip.encode_image(rendered).startswith(b"\xff\xd8")
        assert meta["captured_at_pair"] is None
    finally:
        rendered.close()


def test_binary_exception_does_not_bypass_image_decoding(make_camera):
    cam, _response, _requests = make_camera(chunks=(b"<html>not an image</html>",))
    item = camera.MediaItem(
        url=_ICLOUD_URL, width=None, height=None, mime_type=None, filename=None,
    )
    with pytest.raises(UnidentifiedImageError):
        asyncio.run(cam._compose_single(item, 16, 8, "contain"))