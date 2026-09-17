"""
Regression tests for a real, live-reported dead end: after a hospital-QR "Book Appointment"
picked a doctor with no slots left today, tapping "Different doctor" did nothing -- the
patient was stuck. Root cause was two compounding bugs in
app.conversation._send_patient_details_flow (the actual live code path for date/shift +
patient-details collection, via a WhatsApp Flow -- app.conversation.slot_selection's
_send_slot_options/"choosing_slot" step is never reached for this transition, see
_step_for_action):

1. A genuine 1HMS fetch FAILURE (get_doctor_availability -- confirmed live to intermittently
   503, same backend flakiness as list_doctors_at_hospital) was silently swallowed and
   treated identically to a confirmed empty calendar, sending the misleading
   "Today's timings are already over" instead of a generic retry message.
2. Even on a genuine empty-calendar result, the "Different doctor" button transitioned to
   "choosing_doctor" -- whose handler only accepts list_reply input, so the button tap
   (button_reply) was silently rejected with a generic hint that never offered a new list.

Same style as test_doctor_booking_qr.py -- stubs aioodbc/redis before importing app modules,
no pytest. Run directly: python3 tests/test_patient_details_flow.py
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object


async def _create_pool(*a, **k):
    raise NotImplementedError


_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

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

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402

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
        self.buttons: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, button_label, rows))

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))

    async def send_flow(self, client, to, body_text, flow_id, flow_cta, screen_id, flow_token, initial_data=None):
        return False

    async def send_text_direct(self, client, to, body):
        self.texts.append(body)


class _RecordingDb:
    def __init__(self, initial_state: dict | None = None):
        self._state = initial_state

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self._state = None


QR_HOSPITAL = {"hospitalId": "hosp-star", "name": "Star Hospital"}
DOCTOR_A = {"doctorId": "doc-a", "fullName": "accountant", "hospitalId": "hosp-star", "hospitalName": "Star Hospital"}
DOCTOR_B = {"doctorId": "doc-b", "fullName": "Dr Nizami", "hospitalId": "hosp-star", "hospitalName": "Star Hospital"}


def _hospital_qr_context():
    return {
        "lang": "en",
        "qr_hospital": QR_HOSPITAL,
        "qr_scanned_at": conversation._clinic_now().isoformat(),
        "doctor_id": "doc-a",
        "doctor_name": "accountant",
        "current_step": "awaiting_patient_details",
    }


def test_availability_fetch_failure_sends_generic_retry_not_shifts_over():
    print("\n--- Live-reported: get_doctor_availability 503s -- must send error_hms, not the misleading 'timings over' message ---")
    context = _hospital_qr_context()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        import httpx
        with patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_get_offered_slots", AsyncMock(side_effect=httpx.HTTPStatusError(
                 "503", request=None, response=httpx.Response(503)))):
            async with httpx.AsyncClient() as client:
                await conversation._send_patient_details_flow(client, "919876543210", context)

    run(_run())
    check(len(wa_mock.texts) == 1, f"sends exactly one text, got {wa_mock.texts!r}")
    check(wa_mock.texts and "something went wrong" in wa_mock.texts[0].lower(),
          f"sends the generic HMS-retry message, got {wa_mock.texts!r}")
    check(len(wa_mock.buttons) == 0, "does NOT send the 'Different doctor' button -- we don't actually know the calendar is empty")


def test_genuine_no_slots_transitions_to_choosing_slot_not_choosing_doctor():
    print("\n--- A genuinely empty calendar still sends 'Different doctor', but must land on a step that honours the tap ---")
    context = _hospital_qr_context()
    db_mock = _RecordingDb(initial_state={"current_step": "awaiting_patient_details", "context": context})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        import httpx
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_get_offered_slots", AsyncMock(return_value=[])):
            async with httpx.AsyncClient() as client:
                await conversation._send_patient_details_flow(client, "919876543210", context)

    run(_run())
    check(len(wa_mock.buttons) == 1, f"sends the 'Different doctor' button, got {wa_mock.buttons!r}")
    check(db_mock._state["current_step"] == "choosing_slot",
          f"lands on choosing_slot (whose handler wires 'change_doctor'), not choosing_doctor (which silently ignores it), got {db_mock._state['current_step']!r}")


def test_tapping_different_doctor_after_hospital_qr_no_slots_sends_a_fresh_list():
    print("\n--- Tapping 'Different doctor' from that state must actually re-list the QR-locked hospital's doctors, not crash or dead-end ---")
    context = _hospital_qr_context()
    context.pop("current_step", None)
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_slot", "context": context})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        import httpx
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(return_value=[DOCTOR_A, DOCTOR_B])):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "change_doctor", "msg1")

    run(_run())
    check(len(wa_mock.lists) == 1, f"sends a fresh doctor list instead of dead-ending, got lists={wa_mock.lists!r} texts={wa_mock.texts!r}")
    check(len(wa_mock.texts) == 0, f"no 'please choose from the list' hint fired -- the tap was actually handled, got {wa_mock.texts!r}")


if __name__ == "__main__":
    test_availability_fetch_failure_sends_generic_retry_not_shifts_over()
    test_genuine_no_slots_transitions_to_choosing_slot_not_choosing_doctor()
    test_tapping_different_doctor_after_hospital_qr_no_slots_sends_a_fresh_list()

    print("\n" + "=" * 50)
    if failures:
        print(f"PATIENT DETAILS FLOW TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL PATIENT DETAILS FLOW TESTS PASSED")
