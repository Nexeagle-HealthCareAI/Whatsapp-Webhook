"""
Sanity checks for the per-hospital booking QR flow: the early HOSPBOOK trigger in
app.conversation.handle_message (_handle_hospital_booking_trigger), the
Book Appointment / Check Appointment Status welcome menu it leads into
(_handle_choosing_hospital_action in app/conversation/checkin.py), and the
_advance_or_run_pending_action / pending_hospital machinery in app/conversation/language.py
that lets the menu get shown once a language resolves. Also covers hospital-scoped status
checks (_start_appointment_action_flow's `hospital` filter in appointment_actions.py). Same
style as test_doctor_booking_qr.py -- stubs aioodbc/redis before importing app modules,
mocks hms_client/db/whatsapp_client calls directly, no pytest.
Run directly: python3 test_hospital_booking_qr.py
"""

import asyncio
import os
import sys
import types
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

import httpx

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

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402
from app.messengers.hms_client import HmsApiError, AppointmentDetail  # noqa: E402

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

    async def send_typing_indicator(self, client, message_id):
        pass


class _RecordingDb:
    def __init__(self, initial_state: dict | None = None, booked_appointments: list[dict] | None = None):
        self._state = initial_state
        self._booked_appointments = booked_appointments or []

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self._state = None

    async def get_booked_appointments_for_phone(self, phone):
        return self._booked_appointments

    async def mark_appointment_cancelled_locally(self, hms_appointment_id):
        pass

    async def mark_appointment_rescheduled_locally(self, hms_appointment_id, new_date):
        pass


HOSPITAL = {"hospitalId": "hosp-1", "name": "Purnea General Hospital"}
TWO_DOCTORS = [
    {"doctorId": "d1", "fullName": "Dr. A", "hospitalName": "Purnea General Hospital", "city": "Purnea"},
    {"doctorId": "d2", "fullName": "Dr. B", "hospitalName": "Purnea General Hospital", "city": "Purnea"},
]

_FUTURE_1 = (date.today() + timedelta(days=2)).isoformat()
_FUTURE_2 = (date.today() + timedelta(days=3)).isoformat()

APPT_AT_HOSP1 = AppointmentDetail(appointmentId="appt-1", doctorName="Dr. A", apptDate=_FUTURE_1, statusCode="BOOKED")
APPT_AT_HOSP2 = AppointmentDetail(appointmentId="appt-2", doctorName="Dr. C", apptDate=_FUTURE_2, statusCode="BOOKED")

LOCAL_ROW_HOSP1 = {
    "id": "row-1", "hms_appointment_id": "appt-1", "preferred_date": _FUTURE_1,
    "patient_display_name": "Aquib", "patient_age": None, "patient_gender": None, "patient_guardian": None,
    "hospital_id": "hosp-1",
}
LOCAL_ROW_HOSP2 = {
    "id": "row-2", "hms_appointment_id": "appt-2", "preferred_date": _FUTURE_2,
    "patient_display_name": "Aquib", "patient_age": None, "patient_gender": None, "patient_guardian": None,
    "hospital_id": "hosp-2",
}


def test_trigger_pattern_matches_and_ignores():
    print("\n--- Hospital-QR trigger pattern ---")
    m = conversation._HOSPITAL_BOOKING_TRIGGER_PATTERN.search("QR ID of this hospital is hosp-1")
    check(m is not None and m.group(1) == "hosp-1", "bare 'QR ID of this hospital is <code>' matches")
    m2 = conversation._HOSPITAL_BOOKING_TRIGGER_PATTERN.search("qr id of this hospital is hosp-1")
    check(m2 is not None, "lowercase matches case-insensitively")
    # The real pre-filled QR text (HospitalBookingRedirectHandler.build_wa_payload) is a
    # human-readable sentence with "QR ID of this hospital is <code>" as a trailing phrase,
    # not the whole message -- this is exactly why the pattern uses search(), not match()/^.
    m3 = conversation._HOSPITAL_BOOKING_TRIGGER_PATTERN.search(
        "Hey, I've scanned the QR code for Star Hospital! QR ID of this hospital is hosp-1"
    )
    check(m3 is not None and m3.group(1) == "hosp-1", "human-readable prefixed text still extracts the trailing code")
    check(conversation._HOSPITAL_BOOKING_TRIGGER_PATTERN.search("book appointment") is None,
          "organic 'book appointment' text does not collide with the pattern")
    check(conversation._HOSPITAL_BOOKING_TRIGGER_PATTERN.search("CHECKIN hosp-1") is None,
          "CHECKIN's own trigger text does not collide with the hospital-booking pattern")


def test_invalid_hospital_code_sends_error_and_preserves_state():
    print("\n--- Invalid/unresolvable hospital code ---")
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_doctor", "context": {"lang": "en"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_hospital_by_code", AsyncMock(side_effect=HmsApiError("not found"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "QR ID of this hospital is badcode", "msg1")

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one 'not available' message")
    check(db_mock._state["current_step"] == "choosing_doctor", "existing in-progress state is left untouched on an invalid code")


def test_valid_hospital_lang_known_shows_welcome_menu():
    print("\n--- Valid hospital, language already known -- shows the Book/Check-Status welcome menu, not the doctor list directly ---")
    db_mock = _RecordingDb(initial_state={"current_step": "some_prior_step", "context": {"lang": "en"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_hospital_by_code", AsyncMock(return_value=HOSPITAL)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "QR ID of this hospital is hosp-1", "msg2")

    run(_run())
    check(len(wa_mock.buttons) == 1, "sends exactly one welcome-menu button message, not a doctor list")
    check(len(wa_mock.lists) == 0, "does not jump straight into the doctor list -- the menu comes first")
    button_ids = [bid for bid, _ in wa_mock.buttons[0][1]] if wa_mock.buttons else []
    check(button_ids == ["hospbook_book", "hospbook_status"], f"offers Book Appointment and Check Status buttons, got {button_ids!r}")
    check(db_mock._state["current_step"] == "choosing_hospital_action", "transitions into the new menu step")
    check(db_mock._state["context"].get("qr_hospital") == HOSPITAL, "carries the resolved hospital into the menu step's context")


def test_tapping_book_from_menu_shows_hospital_doctor_list_and_records_qr_lead():
    print("\n--- Tapping Book Appointment on the menu -- shows only this hospital's doctors, records a QR-attributed lead ---")
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL}})
    wa_mock = _RecordingWhatsApp()
    lead_calls = []

    async def _record_lead(**kwargs):
        lead_calls.append(kwargs)

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(return_value=TWO_DOCTORS)), \
             patch.object(conversation.hms_client, "record_lead", AsyncMock(side_effect=_record_lead)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_book", "msg5")

    run(_run())
    check(len(wa_mock.lists) == 1, "sends a doctor-choice list for the hospital's two doctors")
    check(len(wa_mock.lists[0][2]) == 2, "list carries both of the hospital's doctors")
    check(len(lead_calls) == 1, "records exactly one lead")
    check(lead_calls[0]["lead_type"] == "HospitalQRScan", f"attributes the lead to the QR scan, not a typed search, got {lead_calls[0]!r}")
    check(lead_calls[0]["hospital_id"] == "hosp-1", "lead is attributed to the scanned hospital")


def test_valid_hospital_no_lang_yet_asks_language_then_resumes_to_the_menu():
    print("\n--- Valid hospital, language not yet known -- asks first, then resumes into the SAME hospital's welcome menu once picked ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_hospital_by_code", AsyncMock(return_value=HOSPITAL)):
            async with httpx.AsyncClient() as client:
                # Turn 1: scan the QR before any language is known.
                await conversation.handle_message(client, "919876543210", "Test", "text", "QR ID of this hospital is hosp-1", "msg3")
                check(
                    db_mock._state is not None and db_mock._state["current_step"] == "choosing_language",
                    f"asks for a language first, same as any other fresh entry point, got {db_mock._state!r}",
                )
                check(
                    db_mock._state["context"].get("pending_hospital") == HOSPITAL,
                    "carries the resolved hospital forward as a pending action, not losing it while asking for language",
                )

                # Turn 2: patient picks a language from the list _start() sent.
                lang_code = next(iter(conversation.LANGUAGE_LABELS.keys()))
                await conversation.handle_message(client, "919876543210", "Test", "list_reply", lang_code, "msg4")

    run(_run())
    check(len(wa_mock.buttons) == 1, "once language is picked, resumes straight into the hospital's welcome menu -- pending_hospital wasn't dropped")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "choosing_hospital_action",
        f"lands on the menu step, not straight into the doctor list, got {db_mock._state!r}",
    )
    check(db_mock._state["context"].get("qr_hospital") == HOSPITAL, "the resumed menu step still carries the right hospital")


def test_tapping_check_status_shows_only_this_hospitals_appointment():
    print("\n--- Tapping Check Appointment Status -- surfaces only the appointment booked at THIS hospital ---")
    db_mock = _RecordingDb(
        initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL}},
        booked_appointments=[LOCAL_ROW_HOSP1, LOCAL_ROW_HOSP2],
    )
    wa_mock = _RecordingWhatsApp()
    get_appt_calls = []

    async def _get_appointment(appointment_id):
        get_appt_calls.append(appointment_id)
        return {"appt-1": APPT_AT_HOSP1, "appt-2": APPT_AT_HOSP2}.get(appointment_id)

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(side_effect=_get_appointment)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_status", "msg6")

    run(_run())
    check(len(wa_mock.buttons) == 1, "sends exactly one appointment-status message")
    status_text = wa_mock.buttons[0][0] if wa_mock.buttons else ""
    check("Dr. A" in status_text, f"shows the hospital-1 appointment (Dr. A), got: {status_text!r}")
    check("Dr. C" not in status_text, f"does NOT leak the hospital-2 appointment (Dr. C), got: {status_text!r}")
    check(get_appt_calls == ["appt-1"], f"never even looks up the other hospital's appointment via HMS -- filtered locally first, got {get_appt_calls!r}")


def test_check_status_with_no_appointment_at_this_hospital_is_scoped_not_global():
    print("\n--- Check Appointment Status when the patient has an appointment elsewhere but NOT at this hospital ---")
    db_mock = _RecordingDb(
        initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL}},
        booked_appointments=[LOCAL_ROW_HOSP2],
    )
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=APPT_AT_HOSP2)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_status", "msg7")

    run(_run())
    check(len(wa_mock.buttons) == 1, "sends exactly one no-active-appointment message")
    message_text = wa_mock.buttons[0][0] if wa_mock.buttons else ""
    check("Purnea General Hospital" in message_text, f"names the scanned hospital in the empty-state message, got: {message_text!r}")
    button_ids = [bid for bid, _ in wa_mock.buttons[0][1]] if wa_mock.buttons else []
    check(button_ids == ["start_booking"], f"offers the Book Appointment button as the next step, got {button_ids!r}")
    check(db_mock._state is None, "conversation state is cleared, same as the generic no-active-appointment path")


if __name__ == "__main__":
    test_trigger_pattern_matches_and_ignores()
    test_invalid_hospital_code_sends_error_and_preserves_state()
    test_valid_hospital_lang_known_shows_welcome_menu()
    test_tapping_book_from_menu_shows_hospital_doctor_list_and_records_qr_lead()
    test_valid_hospital_no_lang_yet_asks_language_then_resumes_to_the_menu()
    test_tapping_check_status_shows_only_this_hospitals_appointment()
    test_check_status_with_no_appointment_at_this_hospital_is_scoped_not_global()

    print("\n" + "=" * 50)
    if failures:
        print(f"HOSPITAL BOOKING QR TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL HOSPITAL BOOKING QR TESTS PASSED")
