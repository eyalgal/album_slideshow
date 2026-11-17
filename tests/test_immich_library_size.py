"""A whole Immich library larger than 20,000 photos is read in full."""
from __future__ import annotations

import asyncio

from custom_components.album_slideshow import immich


def _client(total: int, page_size: int = 1000) -> tuple[immich.ImmichClient, list[int]]:
    client = immich.ImmichClient(object(), "http://immich.test", "secret")
    pages: list[int] = []

    async def fake_post(_path, body):
        page = body["page"]
        pages.append(page)
        start = (page - 1) * page_size
        items = [{"id": str(i), "type": "IMAGE"} for i in range(start, min(start + page_size, total))]
        more = start + page_size < total
        return {"assets": {"items": items, "nextPage": str(page + 1) if more else None}}

    client._post = fake_post
    return client, pages


def test_reads_a_library_of_36000_photos_in_full():
    client, _ = _client(36_000)

    items = asyncio.run(client._collect_metadata({}))

    assert len(items) == 36_000


def test_still_stops_at_the_safety_ceiling():
    client, pages = _client(10**7)

    items = asyncio.run(client._collect_metadata({}))

    assert len(items) == immich._MAX_ASSETS
    assert len(pages) == immich._MAX_ASSETS // 1000
