"""
tests/test_location_disambiguation.py
----------------------------------------
Covers app.conversation.location._resolve_city and _handle_choosing_location's new
API-backed typed-location resolution (app/messengers/location_client.py) -- 0/1/N match
handling, the local city_index fallback when the API call itself fails, and picking from
the disambiguation list via a list_reply. Run directly: python3 tests/test_location_disambiguation.py
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object
async def _create_pool(*a, **k):
    raise NotImplementedError
_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")
class _StubRedis:
    @classmethod
    def from_url(cls, *a, **k):
        return cls()
_fake_redis_mod.asyncio.Redis = _StubRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402
from app.conversation import location as location_module  # noqa: E402
from app.messengers import location_client  # noqa: E402
from app.decision_maker import booking_slots  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


MUMBAI = {"name": "Mumbai", "type": "city", "state": "Maharashtra", "coordinates": {"latitude": 18.98, "longitude": 72.83}}
MUMBAI_SUBURBAN = {"name": "Mumbai Suburban", "type": "district", "state": "Maharashtra", "coordinates": {"latitude": 19.06, "longitude": 72.87}}
MUMBAI_NO_COORDS = {"name": "Greater Mumbai", "type": "town", "state": "Maharashtra", "coordinates": None}


def test_single_match_resolves_directly_with_coordinates():
    original = location_client.search_locations
    location_client.search_locations = AsyncMock(return_value=[MUMBAI])
    try:
        result = run(location_module._resolve_city({"location_text": "mumbai"}))
        check(result.get("city") == "Mumbai", f"single match sets city, got {result.get('city')!r}")
        check(result.get("patient_lat") == 18.98, "single match's coordinates are used")
        check("location_options" not in result, "a single match is never treated as ambiguous")
    finally:
        location_client.search_locations = original


def test_single_match_without_coordinates_still_sets_city():
    original = location_client.search_locations
    location_client.search_locations = AsyncMock(return_value=[MUMBAI_NO_COORDS])
    try:
        result = run(location_module._resolve_city({"location_text": "greater mumbai"}))
        check(result.get("city") == "Greater Mumbai", "city is still set even without coordinates")
        check(result.get("patient_lat") is None, "no coordinates means no lat/lng, not a guess")
    finally:
        location_client.search_locations = original


def test_multiple_matches_return_as_ambiguous_options():
    original = location_client.search_locations
    location_client.search_locations = AsyncMock(return_value=[MUMBAI, MUMBAI_SUBURBAN])
    try:
        result = run(location_module._resolve_city({"location_text": "mumbai"}))
        check("location_options" in result, "2+ matches are surfaced as candidates, not auto-resolved")
        check(len(result["location_options"]) == 2, f"both candidates are kept, got {len(result.get('location_options', {}))}")
        check("city" not in result, "an ambiguous result must not also claim a resolved city")
    finally:
        location_client.search_locations = original


def test_zero_matches_from_a_working_api_means_not_found():
    original = location_client.search_locations
    location_client.search_locations = AsyncMock(return_value=[])
    try:
        result = run(location_module._resolve_city({"location_text": "nowhere12345"}))
        check(result.get("city") is None, "an empty result from a working API resolves to nothing")
        check("location_options" not in result, "zero matches is not the same as ambiguous")
    finally:
        location_client.search_locations = original


def test_api_failure_falls_back_to_local_city_index():
    original = location_client.search_locations
    location_client.search_locations = AsyncMock(side_effect=ConnectionError("api is down"))
    original_safe_index = conversation._safe_city_index
    async def mock_safe_index():
        return {"Kishanganj": [[26.10, 87.95]]}
    conversation._safe_city_index = mock_safe_index
    try:
        result = run(location_module._resolve_city({"location_text": "kishanganj"}))
        check(result.get("city") == "Kishanganj", f"API failure falls back to the local index match, got {result.get('city')!r}")
        check(result.get("patient_lat") == 26.10, "the local index's own coordinates are used on fallback")
    finally:
        location_client.search_locations = original
        conversation._safe_city_index = original_safe_index


def test_picking_from_the_disambiguation_list_resolves_and_advances():
    original_db = conversation.db
    original_wa_send_list = conversation.whatsapp_client.send_list
    original_advance = conversation._advance_booking_flow

    class _RecordingDb:
        def __init__(self):
            self.saved = None
        async def save_conversation_state(self, phone, step, context):
            self.saved = (step, context)

    db_mock = _RecordingDb()
    advanced_with = []
    async def mock_advance(client, phone, context, booking):
        advanced_with.append((context, booking))

    conversation.db = db_mock
    conversation._advance_booking_flow = mock_advance
    try:
        booking = booking_slots.empty()
        booking_slots.mark_ambiguous(booking, "location", ["0", "1"], raw="mumbai")
        context = {
            "lang": "en", "booking": booking,
            "location_options": {"0": MUMBAI, "1": MUMBAI_SUBURBAN},
        }
        run(location_module._handle_choosing_location(None, "919876543210", "list_reply", "1", context))

        check(len(advanced_with) == 1, "picking a candidate proceeds to advance the booking flow")
        advanced_context, advanced_booking = advanced_with[0]
        check(advanced_context.get("city") == "Mumbai Suburban", f"the CHOSEN candidate is resolved, not the first one, got {advanced_context.get('city')!r}")
        check("location_options" not in advanced_context, "location_options is cleared once resolved")
        check(advanced_booking["location"]["status"] == "filled", "the location slot is filled after picking")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_list = original_wa_send_list
        conversation._advance_booking_flow = original_advance


def test_stale_or_unknown_list_reply_id_reprompts_the_same_list():
    sent_lists = []
    original_send_list = conversation.whatsapp_client.send_list
    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append(rows)
    conversation.whatsapp_client.send_list = mock_send_list
    try:
        booking = booking_slots.empty()
        booking_slots.mark_ambiguous(booking, "location", ["0", "1"], raw="mumbai")
        context = {
            "lang": "en", "booking": booking,
            "location_options": {"0": MUMBAI, "1": MUMBAI_SUBURBAN},
        }
        run(location_module._handle_choosing_location(None, "919876543210", "list_reply", "stale-id-99", context))
        check(len(sent_lists) == 1, "an id that doesn't match any current candidate re-shows the list instead of crashing")
    finally:
        conversation.whatsapp_client.send_list = original_send_list


def test_list_reply_with_no_active_location_list_reprompts_instead_of_sending_an_empty_list():
    """Live-reported: a patient re-tapped a STALE interactive message (WhatsApp never
    disables past interactive messages) after the conversation had already moved on to
    choosing_location, delivering a list_reply id that isn't a language code and doesn't
    match anything -- location_options is empty, so there's no disambiguation list active
    at all. The old code treated any unmatched list_reply as "re-show the current list,"
    which built a location list with ZERO rows; WhatsApp Cloud API rejects an empty list,
    so the send silently failed and the patient got no reply whatsoever. Must re-issue the
    real location prompt instead."""
    sent_lists = []
    sent_location_prompts = []
    sent_texts = []

    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append(rows)

    async def mock_send_location_request(client, to, text):
        sent_location_prompts.append(text)

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    original_send_list = conversation.whatsapp_client.send_list
    original_send_location_request = conversation.whatsapp_client.send_location_request
    original_send_text = conversation.whatsapp_client.send_text
    conversation.whatsapp_client.send_list = mock_send_list
    conversation.whatsapp_client.send_location_request = mock_send_location_request
    conversation.whatsapp_client.send_text = mock_send_text
    try:
        booking = booking_slots.empty()
        context = {"lang": "en", "booking": booking}  # no location_options set
        run(location_module._handle_choosing_location(None, "919876543210", "list_reply", "stale-id-99", context))
        check(len(sent_lists) == 0, f"must never build a location list with no options to show, got {len(sent_lists)} list send(s)")
        check(len(sent_location_prompts) == 1, "must re-issue the real location prompt instead of going silent")
        check(len(sent_texts) == 1, "must re-send the manual-entry hint alongside the location prompt")
    finally:
        conversation.whatsapp_client.send_list = original_send_list
        conversation.whatsapp_client.send_location_request = original_send_location_request
        conversation.whatsapp_client.send_text = original_send_text


def test_stale_language_pick_during_choosing_location_switches_language_and_reasks():
    """Product expectation: WhatsApp keeps the ORIGINAL welcome/language list tappable
    forever, so a patient can pick a DIFFERENT language from it even after choosing_location
    has already started (e.g. picked English, then goes back and taps Hindi instead). That's
    an unambiguous, deliberate signal -- must switch the active language and re-ask for
    location in the NEW language, not silently re-prompt in whatever language was set
    before."""
    sent_texts = []
    sent_location_prompts = []
    saved_states = []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_location_request(client, to, text):
        sent_location_prompts.append(text)

    class _RecordingDb:
        async def save_conversation_state(self, phone, step, context):
            saved_states.append((step, context))

    original_send_text = conversation.whatsapp_client.send_text
    original_send_location_request = conversation.whatsapp_client.send_location_request
    original_db = conversation.db
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_location_request = mock_send_location_request
    conversation.db = _RecordingDb()
    try:
        booking = booking_slots.empty()
        booking_slots.fill(booking, "lang", "en", source="user")
        context = {"lang": "en", "booking": booking}
        run(location_module._handle_choosing_location(None, "919876543210", "list_reply", "hi", context))

        check(context.get("lang") == "hi", f"the tapped language must actually take effect, got lang={context.get('lang')!r}")
        check(context["booking"]["lang"]["value"] == "hi", "the booking clipboard's lang slot must be updated too")
        check(len(sent_location_prompts) == 1, "must re-ask for location once the language switches")
        check(any("हिंदी" in t or "हिन्दी" in t for t in sent_texts), f"the greeting must actually be sent in the NEW language, got sent_texts={sent_texts!r}")
        check(len(saved_states) == 1 and saved_states[0][0] == "choosing_location", f"the language switch must be persisted, staying on choosing_location, got {saved_states!r}")
    finally:
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_location_request = original_send_location_request
        conversation.db = original_db


if __name__ == "__main__":
    test_single_match_resolves_directly_with_coordinates()
    test_single_match_without_coordinates_still_sets_city()
    test_multiple_matches_return_as_ambiguous_options()
    test_zero_matches_from_a_working_api_means_not_found()
    test_api_failure_falls_back_to_local_city_index()
    test_picking_from_the_disambiguation_list_resolves_and_advances()
    test_stale_or_unknown_list_reply_id_reprompts_the_same_list()
    test_list_reply_with_no_active_location_list_reprompts_instead_of_sending_an_empty_list()
    test_stale_language_pick_during_choosing_location_switches_language_and_reasks()

    print("\n" + "=" * 50)
    if failures:
        print(f"LOCATION DISAMBIGUATION TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL LOCATION DISAMBIGUATION TESTS PASSED")
