from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from custom_components.album_slideshow import camera
from custom_components.album_slideshow.const import (
    DEFAULT_PAIR_PARTNER,
    PAIR_PARTNER_DEFAULT,
    PAIR_PARTNER_OPTIONS,
    PAIR_PARTNER_SAME_DAY,
)
from custom_components.album_slideshow.select import PairPartnerSelect
from custom_components.album_slideshow.store import SlideshowStore


# ── Pair partner select ─────────────────────────────────────────────────────


def test_default_pair_partner_keeps_todays_pairing():
    assert DEFAULT_PAIR_PARTNER == PAIR_PARTNER_DEFAULT == "default"
    assert SlideshowStore().pair_partner == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_offers_both_modes():
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), SlideshowStore())
    assert control.options == PAIR_PARTNER_OPTIONS == ["default", "same_day_other_years"]
    assert control.current_option == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_sets_the_store_and_ignores_unknown_options():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    asyncio.run(control.async_select_option(PAIR_PARTNER_SAME_DAY))
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY
    asyncio.run(control.async_select_option("nearest"))
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY


def test_pair_partner_select_shows_default_for_an_unknown_stored_value():
    store = SlideshowStore()
    store.pair_partner = "nearest"
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    assert control.current_option == PAIR_PARTNER_DEFAULT


def test_pair_partner_select_restores_its_value():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state=PAIR_PARTNER_SAME_DAY))
    asyncio.run(control.async_added_to_hass())
    assert store.pair_partner == PAIR_PARTNER_SAME_DAY


def test_pair_partner_select_ignores_an_unknown_restored_value():
    store = SlideshowStore()
    control = PairPartnerSelect(SimpleNamespace(entry_id="album"), store)
    control.async_get_last_state = AsyncMock(return_value=SimpleNamespace(state="nearest"))
    asyncio.run(control.async_added_to_hass())
    assert store.pair_partner == PAIR_PARTNER_DEFAULT


def test_changing_pair_partner_discards_rendered_slides():
    assert "pair_partner" in camera._RENDER_SETTINGS
