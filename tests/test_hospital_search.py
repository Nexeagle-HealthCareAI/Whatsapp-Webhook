"""
Sanity checks for the new hospital-name search capability (Lead Generation Requirement 01):
resolver.match_hospital_by_query, and the doctor-search-miss fallback in conversation.py
(_search_hospitals_flow / _resolve_hospital_search_match / _handle_choosing_hospital_from_search).
Same style as test_doctor_booking_qr.py -- stubs aioodbc/redis before importing app modules,
mocks hms_client/city_index/db/whatsapp_client calls directly, no pytest. Run directly:
python3 test_hospital_search.py
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import httpx

# Stub ODBC database before importing app modules
_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object


async def _create_pool(*a, **k):
    raise NotImplementedError


_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

# Stub Redis before importing app modules
_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")


class MockRedis:
    def __init__(self):
        self.data = {}

    @classmethod
    def from_url(cls, *args, **kwargs):
        return _mock_redis_instance

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        return True


_mock_redis_instance = MockRedis()
_fake_redis_mod.asyncio.Redis = MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402
from app.decision_maker.resolver import match_hospital_by_query  # noqa: E402

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
        self.lists: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, button_label, rows))

    async def send_location_request(self, client, to, body_text):
        pass

    async def send_typing_indicator(self, client, message_id):
        pass


HOSPITAL_A = {"hospitalId": "hosp-a", "name": "Apollo Hospital", "city": "Kolkata"}
HOSPITAL_B = {"hospitalId": "hosp-b", "name": "Apollo Nursing Home", "city": "Mumbai"}
UNRELATED_HOSPITAL = {"hospitalId": "hosp-c", "name": "Fortis Clinic", "city": "Delhi"}

# Two doctors so _render_doctor_list takes the "send a list" branch, not the single-match
# auto-select-and-advance-booking branch (which needs a lot more mocking, already covered by
# test_doctor_booking_qr.py) -- keeps these tests focused on the hospital-search logic itself.
HOSPITAL_A_DOCTORS = [
    {"doctorId": "doc-1", "fullName": "Dr. Rina Sen", "hospitalId": "hosp-a"},
    {"doctorId": "doc-2", "fullName": "Dr. Amit Roy", "hospitalId": "hosp-a"},
]


def test_match_hospital_by_query_matches_and_ignores():
    print("\n--- match_hospital_by_query ---")
    matches = match_hospital_by_query("apollo hospital", [HOSPITAL_A, UNRELATED_HOSPITAL])
    check(len(matches) == 1 and matches[0]["hospitalId"] == "hosp-a", "matches the hospital by name")
    check(match_hospital_by_query("fortis", [HOSPITAL_A, UNRELATED_HOSPITAL])[0]["hospitalId"] == "hosp-c",
          "matches a different hospital by partial name")
    check(match_hospital_by_query("xyz nonexistent place", [HOSPITAL_A, UNRELATED_HOSPITAL]) == [],
          "no match for unrelated text")
    check(match_hospital_by_query("hospital near me", [HOSPITAL_A]) == [],
          "pure filler words alone match nothing")


async def _run_doctor_search_miss(context, query="Apollo Hospital"):
    # _handle_doctor_search_miss is only ever called AFTER a doctor-name search already
    # returned zero matches -- it has no doctor-searching logic of its own, so there's nothing
    # to mock there (city_index/resolve_doctor are exercised by test_resolver.py instead).
    db_mock = AsyncMock()
    wa_mock = _RecordingWhatsApp()
    record_lead_mock = AsyncMock()

    with patch.object(conversation, "db", db_mock), \
         patch.object(conversation, "whatsapp_client", wa_mock), \
         patch.object(conversation.hms_client, "record_lead", record_lead_mock):
        async with httpx.AsyncClient() as client:
            await conversation._handle_doctor_search_miss(client, "919876543210", context, query)
    return wa_mock, record_lead_mock, context


def test_hospital_miss_single_match_shows_doctors_and_records_lead():
    print("\n--- Doctor-search miss, single hospital match ---")
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation.hms_client, "list_hospitals", AsyncMock(return_value=[HOSPITAL_A, UNRELATED_HOSPITAL])), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(return_value=HOSPITAL_A_DOCTORS)):
            return await _run_doctor_search_miss(context, query="Apollo Hospital")

    wa_mock, record_lead_mock, context = run(_run())
    check(record_lead_mock.await_count == 1, "records exactly one lead")
    _, kwargs = record_lead_mock.call_args
    check(kwargs.get("hospital_id") == "hosp-a", "lead is attributed to the matched hospital")
    check(kwargs.get("lead_type") == "HospitalNameSearch", "lead is tagged HospitalNameSearch")
    check(len(wa_mock.lists) == 1, "sends the hospital's doctor list")
    check(context.get("doctor_options") is not None, "doctor_options is populated for the follow-up pick")


def test_hospital_miss_many_matches_sends_disambiguation():
    print("\n--- Doctor-search miss, ambiguous hospital match ---")
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation.hms_client, "list_hospitals", AsyncMock(return_value=[HOSPITAL_A, HOSPITAL_B])):
            return await _run_doctor_search_miss(context, query="Apollo")

    wa_mock, record_lead_mock, context = run(_run())
    check(record_lead_mock.await_count == 0, "does not record a lead until a specific hospital is chosen")
    check(len(wa_mock.lists) == 1, "sends a hospital disambiguation list")
    check(set(context.get("hospital_options", {}).keys()) == {"hosp-a", "hosp-b"}, "both candidates are offered")


def test_no_hospital_match_falls_through_to_not_found():
    print("\n--- Doctor-search miss, no hospital match either ---")
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation.hms_client, "list_hospitals", AsyncMock(return_value=[HOSPITAL_A])):
            return await _run_doctor_search_miss(context, query="Someone Nobody Knows")

    wa_mock, record_lead_mock, context = run(_run())
    check(record_lead_mock.await_count == 0, "no lead recorded for an unresolvable query")
    check(any("Someone Nobody Knows" in t for t in wa_mock.texts), "sends the original 'not found' message")
    check(len(wa_mock.lists) == 0, "no hospital list sent")


def test_choosing_hospital_from_search_resolves_and_records_lead():
    print("\n--- Picking a hospital from the disambiguation list ---")
    context = {
        "lang": "en",
        "hospital_options": {"hosp-a": HOSPITAL_A, "hosp-b": HOSPITAL_B},
        "hospital_search_query": "Apollo",
    }
    db_mock = AsyncMock()
    wa_mock = _RecordingWhatsApp()
    record_lead_mock = AsyncMock()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "record_lead", record_lead_mock), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(return_value=HOSPITAL_A_DOCTORS)):
            async with httpx.AsyncClient() as client:
                await conversation._handle_choosing_hospital_from_search(client, "919876543210", "list_reply", "hosp-a", context)

    run(_run())
    check(record_lead_mock.await_count == 1, "records the lead once a specific hospital is chosen")
    _, kwargs = record_lead_mock.call_args
    check(kwargs.get("hospital_id") == "hosp-a", "lead is attributed to the chosen hospital")
    check(len(wa_mock.lists) == 1, "shows that hospital's doctors")
    check("hospital_options" not in context, "hospital_options is cleared after use")


if __name__ == "__main__":
    test_match_hospital_by_query_matches_and_ignores()
    test_hospital_miss_single_match_shows_doctors_and_records_lead()
    test_hospital_miss_many_matches_sends_disambiguation()
    test_no_hospital_match_falls_through_to_not_found()
    test_choosing_hospital_from_search_resolves_and_records_lead()

    print("\n" + "=" * 50)
    if failures:
        print(f"HOSPITAL SEARCH TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL HOSPITAL SEARCH TESTS PASSED")
