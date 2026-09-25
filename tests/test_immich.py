from __future__ import annotations

import pytest

from custom_components.album_slideshow import immich


# ── normalize_base_url ─────────────────────────────────────────────────────

def test_normalize_strips_trailing_slash_and_api():
    assert immich.normalize_base_url("http://x:2283/") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283/api") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283/api/") == "http://x:2283"
    assert immich.normalize_base_url("http://x:2283") == "http://x:2283"


# ── build_image_url ────────────────────────────────────────────────────────

def test_build_image_url_preview():
    u = immich.build_image_url("http://x:2283", "abc", "preview")
    assert u == "http://x:2283/api/assets/abc/thumbnail?size=preview"


def test_build_image_url_fullsize():
    u = immich.build_image_url("http://x:2283", "abc", "fullsize")
    assert u == "http://x:2283/api/assets/abc/thumbnail?size=fullsize"


def test_build_image_url_original():
    u = immich.build_image_url("http://x:2283/api", "abc", "original")
    assert u == "http://x:2283/api/assets/abc/original"


def test_build_image_url_never_includes_key():
    u = immich.build_image_url("http://x:2283", "abc", "preview")
    assert "apiKey" not in u and "x-api-key" not in u


# ── _to_epoch_ms ───────────────────────────────────────────────────────────

def test_to_epoch_ms_parses_z():
    assert immich._to_epoch_ms("2022-02-06T21:51:51.000Z") == 1644184311000


def test_to_epoch_ms_parses_offset():
    assert immich._to_epoch_ms("2022-02-06T21:51:51+00:00") == 1644184311000


def test_to_epoch_ms_bad_input():
    assert immich._to_epoch_ms(None) is None
    assert immich._to_epoch_ms("") is None
    assert immich._to_epoch_ms("not a date") is None


# ── location_label ─────────────────────────────────────────────────────────

def test_location_label_city_country():
    assert immich.location_label("Lisbon", None, "Portugal") == "Lisbon, Portugal"


def test_location_label_falls_back_to_state():
    assert immich.location_label(None, "California", "USA") == "California, USA"


def test_location_label_country_only():
    assert immich.location_label(None, None, "France") == "France"


def test_location_label_none():
    assert immich.location_label(None, None, None) is None
    assert immich.location_label("", "  ", "") is None


# ── parse_search_page ──────────────────────────────────────────────────────

def test_parse_search_page_filters_and_paginates():
    payload = {
        "assets": {
            "items": [
                {"id": "1", "type": "IMAGE"},
                {"id": "2", "type": "VIDEO"},
                {"id": "3", "type": "IMAGE", "isTrashed": True},
                {"id": "4", "type": "IMAGE", "isArchived": True},
                {"id": "5", "type": "IMAGE"},
                {"type": "IMAGE"},  # no id
            ],
            "nextPage": "2",
        }
    }
    items, nxt = immich.parse_search_page(payload)
    assert [i["id"] for i in items] == ["1", "5"]
    assert nxt == 2


def test_parse_search_page_no_next():
    payload = {"assets": {"items": [{"id": "1", "type": "IMAGE"}], "nextPage": None}}
    items, nxt = immich.parse_search_page(payload)
    assert len(items) == 1 and nxt is None


def test_parse_search_page_empty():
    assert immich.parse_search_page({}) == ([], None)
    assert immich.parse_search_page(None) == ([], None)


# ── parse_asset_exif ───────────────────────────────────────────────────────

def test_parse_asset_exif_full():
    asset = {
        "localDateTime": "2022-02-06T13:51:51.000Z",
        "exifInfo": {
            "dateTimeOriginal": "2022-02-06T21:51:51+00:00",
            "latitude": 38.7,
            "longitude": -9.1,
            "city": "Lisbon",
            "country": "Portugal",
            "description": "  Sunset  ",
        },
    }
    out = immich.parse_asset_exif(asset)
    assert out["captured_at"] == 1644184311000
    assert out["latitude"] == 38.7
    assert out["longitude"] == -9.1
    assert out["location"] == "Lisbon, Portugal"
    assert out["description"] == "Sunset"


def test_parse_asset_exif_null_island_dropped():
    asset = {"exifInfo": {"latitude": 0, "longitude": 0}}
    out = immich.parse_asset_exif(asset)
    assert "latitude" not in out


def test_parse_asset_exif_empty_description_ignored():
    asset = {"exifInfo": {"description": "   "}}
    out = immich.parse_asset_exif(asset)
    assert "description" not in out


def test_parse_asset_exif_no_exif():
    assert immich.parse_asset_exif({}) == {}
    assert immich.parse_asset_exif(None) == {}


# ── selected-person smart crop ─────────────────────────────────────────────

def test_selected_person_ids_supports_current_composite_entries():
    selected = immich.selected_person_ids(
        "composite",
        '{"albums": ["a1"], "people": ["p1", "p2"], "favorites": true}',
    )
    assert selected == {"p1", "p2"}


def test_selected_person_ids_supports_legacy_entries():
    assert immich.selected_person_ids("person", "p1") == {"p1"}
    assert immich.selected_person_ids("people", "p1,p2") == {"p1", "p2"}
    assert immich.selected_person_ids("album", "a1") == set()


def _face(person_id, x1, y1, x2, y2, width=1000, height=2000):
    return {
        "imageWidth": width,
        "imageHeight": height,
        "boundingBoxX1": x1,
        "boundingBoxY1": y1,
        "boundingBoxX2": x2,
        "boundingBoxY2": y2,
        "person": {"id": person_id, "name": person_id},
    }


def test_parse_face_boxes_marks_selected_people():
    faces = [
        _face("selected", 100, 200, 300, 600),
        _face("bystander", 500, 1000, 900, 1800),
    ]
    boxes = immich.parse_face_boxes(faces, {"selected"})
    assert boxes[0] == pytest.approx([0.1, 0.1, 0.3, 0.3, 0.04, True])
    assert boxes[1] == pytest.approx([0.5, 0.5, 0.9, 0.9, 0.16, False])
    assert boxes[0][5] is True
    assert boxes[1][5] is False


def test_parse_face_boxes_includes_unnamed_faces():
    unnamed = _face("x", 100, 200, 300, 600)
    unnamed["person"] = None
    assert immich.parse_face_boxes([unnamed]) == [pytest.approx([0.1, 0.1, 0.3, 0.3, 0.04, False])]


def test_parse_face_boxes_ignores_invalid_faces():
    invalid = _face("p1", 300, 600, 100, 200)
    assert immich.parse_face_boxes([invalid, "junk"], {"p1"}) == []
    assert immich.parse_face_boxes(None) == []


@pytest.mark.parametrize(("edits", "box", "dimensions"), [
    ([{"action": "rotate", "parameters": {"angle": 90}}], (210, 280, 270, 320), (300, 400)),
    ([{"action": "rotate", "parameters": {"angle": 180}}], (80, 210, 120, 270), (400, 300)),
    ([{"action": "rotate", "parameters": {"angle": 270}}], (30, 80, 90, 120), (300, 400)),
    ([{"action": "mirror", "parameters": {"axis": "horizontal"}}], (280, 210, 320, 270), (400, 300)),
    ([{"action": "mirror", "parameters": {"axis": "vertical"}}], (80, 30, 120, 90), (400, 300)),
    ([{"action": "crop", "parameters": {"x": 200, "y": 0, "width": 200, "height": 150}}],
     (80, 30, 120, 90), (200, 150)),
    ([{"action": "crop", "parameters": {"x": 200, "y": 0, "width": 200, "height": 150}},
      {"action": "rotate", "parameters": {"angle": 90}}], (60, 80, 120, 120), (150, 200)),
    ([{"action": "crop", "parameters": {"x": 200, "y": 0, "width": 200, "height": 150}},
      {"action": "rotate", "parameters": {"angle": 90}},
      {"action": "mirror", "parameters": {"axis": "vertical"}}], (30, 80, 90, 120), (150, 200)),
])
def test_parse_face_focus_inverts_immich_edits(edits, box, dimensions):
    face = _face("p1", *box, width=dimensions[0], height=dimensions[1])
    focus = immich.parse_face_focus([face], {"p1"}, edits=edits, original_size=(400, 300))
    assert focus == pytest.approx((0.75, 0.2))


def test_parse_face_focus_unedits_before_clamping():
    face = _face("p1", -100, 30, -60, 90, width=200, height=150)
    edits = [{"action": "crop", "parameters": {"x": 200, "y": 0, "width": 200, "height": 150}}]
    assert immich.parse_face_focus(
        [face], {"p1"}, edits=edits, original_size=(400, 300)
    ) == pytest.approx((0.3, 0.2))


@pytest.mark.parametrize("edit", [
    {"action": "rotate", "parameters": {"angle": 45}},
    {"action": "mirror", "parameters": {"axis": "invalid"}},
    {"action": "future-edit", "parameters": {}},
    {"action": "crop", "parameters": {"x": -1, "y": 0, "width": 100, "height": 100}},
    {"action": "crop", "parameters": {"x": 0, "y": 0, "width": 900, "height": 100}},
    {"action": "crop", "parameters": {"x": 0, "y": 0, "width": float("nan"), "height": 100}},
    {"action": "crop", "parameters": None},
])
def test_parse_face_focus_rejects_unknown_or_invalid_edits(edit):
    with pytest.raises(ValueError):
        immich.parse_face_focus([_face("p1", 10, 10, 20, 20)], {"p1"}, edits=[edit], original_size=(400, 300))


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), True, "100"])
def test_parse_face_focus_rejects_invalid_numbers(invalid):
    face = _face("p1", invalid, 10, 20, 20)
    assert immich.parse_face_focus([face], {"p1"}) is None


@pytest.mark.parametrize("orientation", ["5", "6", "7", "8", "-90", "90", 6])
def test_original_image_size_honors_exif_orientation(orientation):
    asset = {"exifInfo": {"exifImageWidth": 400, "exifImageHeight": 300, "orientation": orientation}}
    assert immich.original_image_size(asset) == (300, 400)


def test_original_image_size_fallback():
    assert immich.original_image_size({"exifInfo": {"exifImageWidth": 400, "exifImageHeight": 300}}) == (400, 300)
    assert immich.original_image_size({}) is None
    assert immich.original_image_size({"exifInfo": {"exifImageWidth": 0, "exifImageHeight": 300}}) is None


# ── parse_random ───────────────────────────────────────────────────────────

def test_parse_random_plain_list():
    payload = [
        {"id": "1", "type": "IMAGE"},
        {"id": "2", "type": "VIDEO"},
        {"id": "3", "type": "IMAGE", "isTrashed": True},
        {"id": "4", "type": "IMAGE"},
    ]
    out = immich.parse_random(payload)
    assert [i["id"] for i in out] == ["1", "4"]


def test_parse_random_wrapped_dict():
    payload = {"assets": {"items": [{"id": "1", "type": "IMAGE"}]}}
    assert [i["id"] for i in immich.parse_random(payload)] == ["1"]


def test_parse_random_empty():
    assert immich.parse_random(None) == []
    assert immich.parse_random({}) == []


# ── build_search_body ──────────────────────────────────────────────────────

def test_build_search_body_album():
    assert immich.build_search_body("album", "aid", None) == {
        "type": "IMAGE",
        "albumIds": ["aid"],
    }


def test_build_search_body_person():
    assert immich.build_search_body("person", "pid", None) == {
        "type": "IMAGE",
        "personIds": ["pid"],
    }


def test_build_search_body_favorites():
    assert immich.build_search_body("favorites", None, None) == {
        "type": "IMAGE",
        "isFavorite": True,
    }


def test_build_search_body_all():
    assert immich.build_search_body("all", None, None) == {"type": "IMAGE"}


def test_build_search_body_search_forces_image_type():
    out = immich.build_search_body(
        "search", None, {"city": "Paris", "type": "VIDEO", "isFavorite": True}
    )
    assert out == {"city": "Paris", "type": "IMAGE", "isFavorite": True}



# ── multi-person union (people source, OR) ─────────────────────────────────

import asyncio


class _FakeClient(immich.ImmichClient):
    """ImmichClient whose _post is stubbed with canned per-id pages.

    ``id_field`` is ``personIds`` or ``albumIds`` - whichever the union
    collector queries with.
    """

    def __init__(self, pages_by_id, id_field="personIds"):
        # Skip the real __init__ (no hass/session needed for these tests).
        self._pages_by_id = pages_by_id
        self._id_field = id_field
        self.calls = []

    async def _post(self, path, body):
        self.calls.append((path, body))
        one = body[self._id_field][0]
        page = body.get("page", 1)
        pages = self._pages_by_id.get(one, [])
        idx = page - 1
        if idx >= len(pages):
            return {"assets": {"items": [], "total": 0, "nextPage": None}}
        items = pages[idx]
        next_page = page + 1 if idx + 1 < len(pages) else None
        return {"assets": {"items": items, "total": len(items), "nextPage": next_page}}


def _asset(aid):
    return {"id": aid, "type": "IMAGE"}


def test_people_union_ors_across_people():
    client = _FakeClient(
        {
            "p1": [[_asset("a"), _asset("b")]],
            "p2": [[_asset("c")]],
        }
    )
    out = asyncio.run(client.async_collect_assets("people", "p1,p2"))
    assert [a["id"] for a in out] == ["a", "b", "c"]


def test_people_union_dedupes_shared_assets():
    client = _FakeClient(
        {
            "p1": [[_asset("a"), _asset("shared")]],
            "p2": [[_asset("shared"), _asset("b")]],
        }
    )
    out = asyncio.run(client.async_collect_assets("people", "p1,p2"))
    assert [a["id"] for a in out] == ["a", "shared", "b"]


def test_people_union_pages_each_person():
    client = _FakeClient(
        {
            "p1": [[_asset("a")], [_asset("b")]],
        }
    )
    out = asyncio.run(client.async_collect_assets("people", "p1"))
    assert [a["id"] for a in out] == ["a", "b"]
    # One query per page; every body carries exactly one personId.
    assert all(len(c[1]["personIds"]) == 1 for c in client.calls)


def test_people_union_ignores_blank_ids():
    client = _FakeClient({"p1": [[_asset("a")]]})
    out = asyncio.run(client.async_collect_assets("people", "p1,,"))
    assert [a["id"] for a in out] == ["a"]
    assert {c[1]["personIds"][0] for c in client.calls} == {"p1"}


def test_albums_union_ors_and_dedupes():
    client = _FakeClient(
        {
            "al1": [[_asset("a"), _asset("shared")]],
            "al2": [[_asset("shared"), _asset("b")]],
        },
        id_field="albumIds",
    )
    out = asyncio.run(client.async_collect_assets("albums", "al1,al2"))
    assert [a["id"] for a in out] == ["a", "shared", "b"]
    # Each album is queried on its own (OR), never combined into one AND query.
    assert all(len(c[1]["albumIds"]) == 1 for c in client.calls)


def test_albums_union_single_album():
    client = _FakeClient({"al1": [[_asset("a")]]}, id_field="albumIds")
    out = asyncio.run(client.async_collect_assets("albums", "al1"))
    assert [a["id"] for a in out] == ["a"]


# ── composite selection (albums + people + favorites + filter, OR) ─────────

def test_parse_composite_selection_valid():
    sel = immich.parse_composite_selection(
        '{"albums": ["a1"], "people": ["p1", "p2"], "favorites": true}'
    )
    assert sel == {"albums": ["a1"], "people": ["p1", "p2"], "favorites": True}


def test_parse_composite_selection_empty_or_bad():
    assert immich.parse_composite_selection(None) == {
        "albums": [],
        "people": [],
        "favorites": False,
    }
    assert immich.parse_composite_selection("not json") == {
        "albums": [],
        "people": [],
        "favorites": False,
    }


def test_build_composite_bodies_mixes_members():
    bodies = immich.build_composite_bodies(
        '{"albums": ["a1"], "people": ["p1"], "favorites": true}'
    )
    assert {"type": "IMAGE", "albumIds": ["a1"]} in bodies
    assert {"type": "IMAGE", "personIds": ["p1"]} in bodies
    assert {"type": "IMAGE", "isFavorite": True} in bodies
    assert len(bodies) == 3


def test_build_composite_bodies_includes_custom_filter():
    bodies = immich.build_composite_bodies(
        '{"albums": [], "people": [], "favorites": false}',
        {"city": "Paris", "type": "VIDEO"},
    )
    # Custom filter becomes one member, forced to images.
    assert bodies == [{"city": "Paris", "type": "IMAGE"}]


def test_build_composite_bodies_empty_is_all_photos():
    assert immich.build_composite_bodies(None) == [{"type": "IMAGE"}]


class _CompositeFakeClient(immich.ImmichClient):
    """ImmichClient stub keyed by a canonical string for each union body."""

    def __init__(self, items_by_key):
        self._items_by_key = items_by_key
        self.calls = []

    async def _post(self, path, body):
        self.calls.append(dict(body))
        if "albumIds" in body:
            key = "album:" + body["albumIds"][0]
        elif "personIds" in body:
            key = "person:" + body["personIds"][0]
        elif body.get("isFavorite"):
            key = "favorites"
        else:
            key = "all"
        items = self._items_by_key.get(key, [])
        return {"assets": {"items": items, "total": len(items), "nextPage": None}}


def test_composite_union_mixes_and_dedupes():
    client = _CompositeFakeClient(
        {
            "album:al1": [_asset("a"), _asset("shared")],
            "person:p1": [_asset("shared"), _asset("b")],
            "favorites": [_asset("c")],
        }
    )
    sel = '{"albums": ["al1"], "people": ["p1"], "favorites": true}'
    out = asyncio.run(client.async_collect_assets("composite", sel))
    assert [a["id"] for a in out] == ["a", "shared", "b", "c"]


def test_composite_empty_selection_fetches_all():
    client = _CompositeFakeClient({"all": [_asset("a"), _asset("b")]})
    out = asyncio.run(
        client.async_collect_assets(
            "composite", '{"albums": [], "people": [], "favorites": false}'
        )
    )
    assert [a["id"] for a in out] == ["a", "b"]
    assert client.calls == [{"type": "IMAGE", "size": 1000, "page": 1}]
