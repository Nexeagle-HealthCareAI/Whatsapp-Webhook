"""
Sanity checks for the OPD QR check-in flow: the early CHECKIN trigger in
app.conversation.handle_message, and the two new steps (checkin_awaiting_location,
checkin_choosing_appointment). Same style as test_nlu_integration.py -- stubs
aioodbc/redis before importing app modules, mocks hms_client/db/whatsapp_client calls
directly, no pytest. Run directly: python3 test_checkin.py
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
from app.messengers.hms_client import HmsApiError  # noqa: E402

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
        self.location_requests: list[str] = []
        self.lists: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_location_request(self, client, to, body_text):
        self.location_requests.append(body_text)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, button_label, rows))

    async def send_typing_indicator(self, client, message_id):
        pass


class _RecordingDb:
    """In-memory stand-in for app.db's conversation_state + checkin-notification calls."""

    def __init__(self, initial_state: dict | None = None):
        self._state = initial_state
        self.upserts: list[tuple] = []
        self.cleared = False

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self.cleared = True
        self._state = None

    async def upsert_checkin_notification(self, hms_appointment_id, phone_number, preferred_language, patient_display_name=None):
        self.upserts.append((hms_appointment_id, phone_number, preferred_language))


def test_checkin_trigger_matches_and_ignores():
    print("\n--- CHECKIN trigger pattern ---")
    m = conversation._CHECKIN_TRIGGER_PATTERN.match("CHECKIN APLO4F")
    check(m is not None and m.group(1) == "APLO4F", "uppercase CHECKIN with code matches")
    m2 = conversation._CHECKIN_TRIGGER_PATTERN.match("checkin aplo4f")
    check(m2 is not None and m2.group(1) == "aplo4f", "lowercase checkin matches case-insensitively")
    check(conversation._CHECKIN_TRIGGER_PATTERN.match("I want to checkin now") is None, "embedded 'checkin' in a sentence does not match")
    check(conversation._CHECKIN_TRIGGER_PATTERN.match("checkin") is None, "bare 'checkin' with no code does not match")


def test_trigger_invalid_code_sends_message_and_preserves_state():
    print("\n--- Invalid check-in code ---")
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_doctor", "context": {"lang": "en", "doctor_id": "d1"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_hospital_by_code", AsyncMock(side_effect=HmsApiError("not found"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "CHECKIN BADCODE", "msg1")

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one message for an invalid code")
    check(db_mock._state["current_step"] == "choosing_doctor", "existing in-progress state is left untouched on an invalid code")


def test_trigger_valid_code_starts_location_prompt():
    print("\n--- Valid check-in code starts the flow ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()
    hospital = {"success": True, "hospitalId": "hosp-1", "name": "Apollo Test", "city": "Kolkata"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_hospital_by_code", AsyncMock(return_value=hospital)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "CHECKIN APLO4F", "msg2")

    run(_run())
    check(db_mock._state is not None and db_mock._state["current_step"] == "checkin_awaiting_location", "transitions to checkin_awaiting_location")
    check(db_mock._state["context"]["checkin_hospital_id"] == "hosp-1", "hospital id is carried into context")
    check(len(wa_mock.location_requests) == 1, "sends exactly one location request")


def test_awaiting_location_single_match_checks_in():
    print("\n--- Single appointment match checks in ---")
    context = {"lang": "en", "checkin_hospital_id": "hosp-1", "checkin_hospital_name": "Apollo Test"}
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_awaiting_location", "context": context})
    wa_mock = _RecordingWhatsApp()
    resolve_result = {"success": True, "appointmentId": "appt-1", "tokenNo": 7, "status": "WAITING"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "resolve_checkin", AsyncMock(return_value=resolve_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "location", "22.5726,88.3639", "msg3")

    run(_run())
    check(len(db_mock.upserts) == 1 and db_mock.upserts[0][0] == "appt-1", "registers the appointment for future queue-update pushes")
    check(db_mock.cleared is True, "conversation state is cleared after a successful check-in")
    check(any("7" in t for t in wa_mock.texts), "confirmation message mentions the token number")


def test_awaiting_location_multiple_matches_offers_choice():
    print("\n--- Multiple appointment matches offer a list ---")
    context = {"lang": "en", "checkin_hospital_id": "hosp-1", "checkin_hospital_name": "Apollo Test"}
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_awaiting_location", "context": context})
    wa_mock = _RecordingWhatsApp()
    resolve_result = {
        "success": False,
        "message": "Multiple appointments found for today. Please choose one.",
        "candidates": [
            {"appointmentId": "appt-1", "doctorName": "Dr. A", "startAt": "2026-08-11T10:00:00"},
            {"appointmentId": "appt-2", "doctorName": "Dr. B", "startAt": "2026-08-11T14:00:00"},
        ],
    }

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "resolve_checkin", AsyncMock(return_value=resolve_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "location", "22.5726,88.3639", "msg4")

    run(_run())
    check(db_mock._state["current_step"] == "checkin_choosing_appointment", "transitions to checkin_choosing_appointment")
    check(len(wa_mock.lists) == 1 and len(wa_mock.lists[0][2]) == 2, "sends a list with both candidates")
    check(len(db_mock.upserts) == 0, "does not register any appointment yet -- nothing was actually checked in")


def test_awaiting_location_too_far_reprompts():
    print("\n--- Geofence rejection re-prompts for location ---")
    context = {"lang": "en", "checkin_hospital_id": "hosp-1", "checkin_hospital_name": "Apollo Test"}
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_awaiting_location", "context": context})
    wa_mock = _RecordingWhatsApp()
    resolve_result = {"success": False, "message": "You don't appear to be at the hospital yet."}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "resolve_checkin", AsyncMock(return_value=resolve_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "location", "28.6139,77.2090", "msg5")

    run(_run())
    check(db_mock._state["current_step"] == "checkin_awaiting_location", "stays on checkin_awaiting_location so the patient can retry")
    check(db_mock.cleared is False, "state is not cleared on a recoverable 'too far' rejection")


def test_awaiting_location_no_appointment_clears_state():
    print("\n--- No appointment found clears state ---")
    context = {"lang": "en", "checkin_hospital_id": "hosp-1", "checkin_hospital_name": "Apollo Test"}
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_awaiting_location", "context": context})
    wa_mock = _RecordingWhatsApp()
    resolve_result = {"success": False, "message": "No appointment found for today."}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "resolve_checkin", AsyncMock(return_value=resolve_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "location", "22.5726,88.3639", "msg6")

    run(_run())
    check(db_mock.cleared is True, "state is cleared -- nothing more this flow can do without a matching appointment")


def test_choosing_appointment_stale_selection_is_ignored():
    print("\n--- Stale list selection is ignored ---")
    context = {
        "lang": "en", "checkin_latitude": 22.5726, "checkin_longitude": 88.3639,
        "checkin_options": {"appt-1": {"appointmentId": "appt-1", "doctorName": "Dr. A"}},
    }
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_choosing_appointment", "context": context})
    wa_mock = _RecordingWhatsApp()
    issue_token_mock = AsyncMock()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "issue_queue_token", issue_token_mock):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "list_reply", "appt-stale", "msg7")

    run(_run())
    check(issue_token_mock.await_count == 0, "never calls issue_queue_token for an id not in checkin_options")
    check(db_mock._state["current_step"] == "checkin_choosing_appointment", "stays on the same step")


def test_choosing_appointment_valid_selection_checks_in():
    print("\n--- Valid list selection checks in ---")
    context = {
        "lang": "en", "checkin_latitude": 22.5726, "checkin_longitude": 88.3639,
        "checkin_options": {"appt-2": {"appointmentId": "appt-2", "doctorName": "Dr. B"}},
    }
    db_mock = _RecordingDb(initial_state={"current_step": "checkin_choosing_appointment", "context": context})
    wa_mock = _RecordingWhatsApp()
    token_result = {"success": True, "tokenNo": 3, "status": "WAITING"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "issue_queue_token", AsyncMock(return_value=token_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "list_reply", "appt-2", "msg8")

    run(_run())
    check(len(db_mock.upserts) == 1 and db_mock.upserts[0][0] == "appt-2", "registers the chosen appointment for future queue-update pushes")
    check(db_mock.cleared is True, "conversation state is cleared after a successful check-in")


if __name__ == "__main__":
    test_checkin_trigger_matches_and_ignores()
    test_trigger_invalid_code_sends_message_and_preserves_state()
    test_trigger_valid_code_starts_location_prompt()
    test_awaiting_location_single_match_checks_in()
    test_awaiting_location_multiple_matches_offers_choice()
    test_awaiting_location_too_far_reprompts()
    test_awaiting_location_no_appointment_clears_state()
    test_choosing_appointment_stale_selection_is_ignored()
    test_choosing_appointment_valid_selection_checks_in()

    print("\n" + "=" * 50)
    if failures:
        print(f"CHECK-IN TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL CHECK-IN TESTS PASSED")
