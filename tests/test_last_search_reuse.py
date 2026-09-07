"""
Covers the "reuse last location + specialty" flow (app/conversation/last_search.py,
app/db/patient_last_search.py): the pure freshness check, and the three confirm-button
branches (Yes / Change Location / Change Specialty). Same style as
test_appointment_cancel_reschedule.py -- stubs aioodbc/redis before importing app modules,
no pytest. Run directly: python3 tests/test_last_search_reuse.py
"""

import asyncio
import os
import sys
import types
from datetime import datetime, timedelta, timezone

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
from app.conversation import last_search as last_search_module  # noqa: E402
from app.db.patient_last_search import is_last_search_fresh  # noqa: E402
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


class _RecordingWhatsApp:
    def __init__(self):
        self.texts: list[str] = []
        self.buttons: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))


NOW = datetime.now(timezone.utc)
FRESH_ROW = {
    "last_city": "Kishanganj", "last_location_text": "kishanganj",
    "last_patient_lat": 26.1, "last_patient_lng": 87.9,
    "location_updated_at": NOW - timedelta(hours=1),
    "last_specialty_category": "Cardiologist (Heart)",
    "specialty_updated_at": NOW - timedelta(hours=2),
}


def test_is_last_search_fresh_truth_table():
    print("\n--- is_last_search_fresh: both fresh / one missing / one stale / both missing ---")
    check(is_last_search_fresh(FRESH_ROW, now=NOW) is True, "both present and both under 24h -> fresh")

    no_specialty = {**FRESH_ROW, "last_specialty_category": None, "specialty_updated_at": None}
    check(is_last_search_fresh(no_specialty, now=NOW) is False, "missing specialty -> not fresh")

    no_location = {**FRESH_ROW, "last_city": None, "last_patient_lat": None, "location_updated_at": None}
    check(is_last_search_fresh(no_location, now=NOW) is False, "missing location -> not fresh")

    stale_specialty = {**FRESH_ROW, "specialty_updated_at": NOW - timedelta(hours=25)}
    check(is_last_search_fresh(stale_specialty, now=NOW) is False, "specialty older than 24h -> not fresh even though location is fresh")

    stale_location = {**FRESH_ROW, "location_updated_at": NOW - timedelta(hours=25)}
    check(is_last_search_fresh(stale_location, now=NOW) is False, "location older than 24h -> not fresh even though specialty is fresh")

    check(is_last_search_fresh(None, now=NOW) is False, "no row at all -> not fresh")


def test_reuse_yes_fetches_doctors_live_without_asking_sort():
    print("\n--- Tapping Yes skips choosing_location AND choosing_sort, fetches doctors live ---")
    calls = []

    async def mock_send_doctor_list(client, phone, context):
        calls.append(context)

    original_send_doctor_list = conversation._send_doctor_list
    conversation._send_doctor_list = mock_send_doctor_list
    try:
        context = {
            "lang": "en", "session_id": "sid-1",
            "last_search_specialty": "Cardiologist (Heart)",
            "last_search_city": "Kishanganj", "last_search_location_text": "kishanganj",
            "last_search_lat": 26.1, "last_search_lng": 87.9,
        }
        run(last_search_module._handle_confirming_last_search(None, "919876543210", "button_reply", "reuse_yes", context))

        check(len(calls) == 1, f"must call _send_doctor_list exactly once, got {len(calls)}")
        sent = calls[0]
        check(sent.get("specialty_category") == "Cardiologist (Heart)", f"carries the stored specialty forward, got {sent.get('specialty_category')!r}")
        check(sent.get("city") == "Kishanganj", f"carries the stored city forward, got {sent.get('city')!r}")
        check(sent.get("patient_lat") == 26.1, f"carries the stored coordinates forward, got {sent.get('patient_lat')!r}")
        check(sent.get("sort_key") == "nearest", f"defaults to nearest sort when coordinates are known, got {sent.get('sort_key')!r}")
    finally:
        conversation._send_doctor_list = original_send_doctor_list


def test_reuse_change_location_keeps_specialty_pending_and_asks_for_location():
    print("\n--- Tapping Change Location re-asks for location, keeps the old specialty pending ---")
    calls = []

    async def mock_advance(client, phone, context, booking):
        calls.append((context, booking))

    original_advance = conversation._advance_booking_flow
    conversation._advance_booking_flow = mock_advance
    try:
        context = {
            "lang": "en", "session_id": "sid-1",
            "last_search_specialty": "Cardiologist (Heart)",
            "last_search_city": "Kishanganj", "last_search_location_text": "kishanganj",
            "last_search_lat": 26.1, "last_search_lng": 87.9,
        }
        run(last_search_module._handle_confirming_last_search(None, "919876543210", "button_reply", "reuse_change_location", context))

        check(len(calls) == 1, f"must call _advance_booking_flow exactly once, got {len(calls)}")
        sent_context, sent_booking = calls[0]
        check(sent_context.get("pending_specialty") == "Cardiologist (Heart)", f"carries the OLD specialty forward as pending, got {sent_context.get('pending_specialty')!r}")
        check(sent_booking["lang"]["status"] == "filled", "lang is pre-filled")
        check(sent_booking["location"]["status"] == "blank", "location is left blank so _advance_booking_flow's own pending_specialty branch asks for a new one")
    finally:
        conversation._advance_booking_flow = original_advance


def test_reuse_change_specialty_keeps_location_filled_and_opens_search_mode():
    print("\n--- Tapping Change Specialty keeps the old location, lands on search-mode with no specialty pending ---")
    calls = []

    async def mock_advance(client, phone, context, booking):
        calls.append((context, booking))

    original_advance = conversation._advance_booking_flow
    conversation._advance_booking_flow = mock_advance
    try:
        context = {
            "lang": "en", "session_id": "sid-1",
            "last_search_specialty": "Cardiologist (Heart)",
            "last_search_city": "Kishanganj", "last_search_location_text": "kishanganj",
            "last_search_lat": 26.1, "last_search_lng": 87.9,
        }
        run(last_search_module._handle_confirming_last_search(None, "919876543210", "button_reply", "reuse_change_specialty", context))

        check(len(calls) == 1, f"must call _advance_booking_flow exactly once, got {len(calls)}")
        sent_context, sent_booking = calls[0]
        check("pending_specialty" not in sent_context, "no specialty carried forward -- the whole point is picking a NEW one")
        check(sent_booking["lang"]["status"] == "filled", "lang is pre-filled")
        check(sent_booking["location"]["status"] == "filled", "the OLD location is pre-filled so it doesn't get re-asked")
        check(sent_context.get("city") == "Kishanganj", f"old city carried in context too (read by _send_doctor_list/_sort_doctors later), got {sent_context.get('city')!r}")
    finally:
        conversation._advance_booking_flow = original_advance


def test_confirm_prompt_names_the_specialty_and_city_with_three_buttons():
    print("\n--- The confirm prompt itself names both pieces and offers exactly 3 buttons ---")
    wa_mock = _RecordingWhatsApp()
    original_wa = conversation.whatsapp_client
    conversation.whatsapp_client = wa_mock
    try:
        context = {
            "lang": "en",
            "last_search_specialty": "Cardiologist (Heart)",
            "last_search_city": "Kishanganj",
        }
        run(last_search_module._prompt_confirming_last_search(None, "919876543210", context))

        check(len(wa_mock.buttons) == 1, "sends exactly one button prompt")
        body_text, buttons = wa_mock.buttons[0]
        check("Cardiologist (Heart)" in body_text, f"names the specialty, got {body_text!r}")
        check("Kishanganj" in body_text, f"names the city, got {body_text!r}")
        button_ids = [bid for bid, _ in buttons]
        check(button_ids == ["reuse_yes", "reuse_change_location", "reuse_change_specialty"], f"offers exactly the 3 expected buttons, got {button_ids!r}")
    finally:
        conversation.whatsapp_client = original_wa


def test_unrecognized_reply_sends_a_hint_instead_of_crashing():
    print("\n--- A stray reply that isn't one of the 3 buttons gets a hint, not a crash ---")
    wa_mock = _RecordingWhatsApp()
    original_wa = conversation.whatsapp_client
    conversation.whatsapp_client = wa_mock
    try:
        context = {"lang": "en", "last_search_specialty": "Cardiologist (Heart)", "last_search_city": "Kishanganj"}
        run(last_search_module._handle_confirming_last_search(None, "919876543210", "text", "maybe?", context))
        check(len(wa_mock.texts) == 1, f"sends exactly one hint text, got {wa_mock.texts!r}")
    finally:
        conversation.whatsapp_client = original_wa


if __name__ == "__main__":
    test_is_last_search_fresh_truth_table()
    test_reuse_yes_fetches_doctors_live_without_asking_sort()
    test_reuse_change_location_keeps_specialty_pending_and_asks_for_location()
    test_reuse_change_specialty_keeps_location_filled_and_opens_search_mode()
    test_confirm_prompt_names_the_specialty_and_city_with_three_buttons()
    test_unrecognized_reply_sends_a_hint_instead_of_crashing()

    print("\n" + "=" * 50)
    if failures:
        print(f"LAST SEARCH REUSE TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL LAST SEARCH REUSE TESTS PASSED")
