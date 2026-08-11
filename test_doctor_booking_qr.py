"""
Sanity checks for the Doctor Dekho per-doctor booking QR flow: the early DRBOOK trigger in
app.conversation.handle_message (_handle_doctor_booking_trigger). Same style as
test_checkin.py/test_document_qr.py -- stubs aioodbc/redis before importing app modules,
mocks hms_client/db/whatsapp_client calls directly, no pytest. Run directly:
python3 test_doctor_booking_qr.py
"""

import asyncio
import os
import sys
import types
from datetime import date
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

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402
from app.hms_client import HmsApiError  # noqa: E402

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
    """Records every send_* call instead of hitting the real Graph API."""

    def __init__(self):
        self.texts: list[str] = []
        self.lists: list[tuple] = []
        self.buttons: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, button_label, rows))

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))

    async def send_flow(self, client, to, body_text, flow_id, flow_cta, screen_id, flow_token, initial_data=None):
        return False

    async def send_typing_indicator(self, client, message_id):
        pass


class _RecordingDb:
    """In-memory stand-in for app.db's conversation_state calls."""

    def __init__(self, initial_state: dict | None = None):
        self._state = initial_state

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self._state = None


DOCTOR = {"doctorId": "doc-1", "fullName": "Dr. Priya Sharma"}


def test_trigger_pattern_matches_and_ignores():
    print("\n--- DRBOOK trigger pattern ---")
    m = conversation._DOCTOR_BOOKING_TRIGGER_PATTERN.match("DRBOOK doc-1")
    check(m is not None and m.group(1) == "doc-1", "DRBOOK <id> matches")
    m2 = conversation._DOCTOR_BOOKING_TRIGGER_PATTERN.match("drbook doc-1")
    check(m2 is not None, "lowercase drbook matches case-insensitively")
    check(conversation._DOCTOR_BOOKING_TRIGGER_PATTERN.match("doctor sharma") is None,
          "organic 'doctor sharma' text does not collide with the DRBOOK pattern")
    check(conversation._DOCTOR_BOOKING_TRIGGER_PATTERN.match("book appointment") is None,
          "organic 'book appointment' text does not collide with the DRBOOK pattern")


def test_unresolvable_doctor_sends_not_available_and_preserves_state():
    print("\n--- Unresolvable doctor id ---")
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_doctor", "context": {"lang": "en", "doctor_id": "d1"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_doctor_by_id", AsyncMock(side_effect=HmsApiError("not found"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DRBOOK badid", "msg1")

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one 'not available' message")
    check(db_mock._state["current_step"] == "choosing_doctor", "existing in-progress state is left untouched")


def test_valid_doctor_no_lang_yet_asks_language_with_doctor_prefilled():
    print("\n--- Valid doctor, language not yet known ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_doctor_by_id", AsyncMock(return_value=DOCTOR)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DRBOOK doc-1", "msg2")

    run(_run())
    check(len(wa_mock.lists) == 1, "sends the language-choice list (same first step as any fresh booking)")
    check(db_mock._state is not None and db_mock._state["current_step"] == "choosing_language", "transitions to choosing_language")
    booking = db_mock._state["context"].get("booking")
    check(booking is not None and booking["doctor"]["status"] == "filled", "doctor slot is already filled before language is even chosen")
    check(booking["doctor"]["value"]["id"] == "doc-1", "doctor slot carries the resolved doctor id")
    check(booking["doctor"]["value"]["fullName"] == "Dr. Priya Sharma", "doctor slot carries the resolved doctor name")


FAKE_SLOT = {"button_id": "slot1", "shift_name": "Morning", "date": date(2026, 8, 12), "is_today": True, "label": "Today Morning"}


def test_valid_doctor_lang_known_skips_straight_to_date():
    print("\n--- Valid doctor, language already known -- skips search entirely ---")
    db_mock = _RecordingDb(initial_state={"current_step": "some_prior_step", "context": {"lang": "hi"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_get_offered_slots", AsyncMock(return_value=[FAKE_SLOT])), \
             patch.object(conversation.hms_client, "get_doctor_by_id", AsyncMock(return_value=DOCTOR)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DRBOOK doc-1", "msg3")

    run(_run())
    booking = db_mock._state["context"]["booking"]
    check(booking["doctor"]["status"] == "filled" and booking["doctor"]["value"]["id"] == "doc-1", "doctor slot filled with the resolved doctor")
    check(booking["location"]["status"] == "filled" and booking["location"]["source"] == "inferred",
          "location is auto-inferred (skipped), not asked for")
    check(db_mock._state["current_step"] == "awaiting_patient_details", "jumps straight to the date/slot-offer step, skipping specialty/doctor search")
    check(len(wa_mock.texts) >= 1, "sends the welcome banner and/or slot-offer text")
    check(len(wa_mock.lists) == 0, "never sends a doctor-search list (specialty/sort/doctor choices)")


if __name__ == "__main__":
    test_trigger_pattern_matches_and_ignores()
    test_unresolvable_doctor_sends_not_available_and_preserves_state()
    test_valid_doctor_no_lang_yet_asks_language_with_doctor_prefilled()
    test_valid_doctor_lang_known_skips_straight_to_date()

    print("\n" + "=" * 50)
    if failures:
        print(f"DOCTOR BOOKING QR TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL DOCTOR BOOKING QR TESTS PASSED")
