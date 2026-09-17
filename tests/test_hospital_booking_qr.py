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
        self.location_requests: list[str] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_text_direct(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, button_label, rows))

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))

    async def send_location_request(self, client, to, body):
        self.location_requests.append(body)

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


def test_hospital_doctor_fetch_failure_sends_generic_retry_not_a_doctor_not_found_message():
    print("\n--- Live-reported bug: a 503 fetching this hospital's doctors sent 'we couldn't find a doctor matching Star Hospital' -- wrong message for a backend failure ---")
    # _resolve_hospital_search_match's except-branch used t("search_doctor_not_found",
    # query=query) -- but query here is the HOSPITAL's own name (this function only ever
    # runs once the hospital is already resolved), not something the patient typed. A
    # transient 1HMS 503 on /public/doctors made the bot tell the patient their search
    # term was bad, instead of "something went wrong, try again" -- misleading, and gives
    # no indication a retry might just work.
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(side_effect=Exception("503 Service Unavailable"))), \
             patch.object(conversation.hms_client, "record_lead", AsyncMock()):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_book", "msg14")

    run(_run())
    check(len(wa_mock.texts) == 1, f"sends exactly one message, got {wa_mock.texts!r}")
    check(
        not any("couldn't find a doctor matching" in t.lower() for t in wa_mock.texts),
        f"must not claim the search term (the hospital's own name) didn't match anything, got texts={wa_mock.texts!r}",
    )
    check(
        any("something went wrong" in t.lower() for t in wa_mock.texts),
        f"must send the generic HMS-retry message instead, got texts={wa_mock.texts!r}",
    )


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
    scanned_at = conversation._clinic_now().isoformat()
    db_mock = _RecordingDb(
        initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL, "qr_scanned_at": scanned_at}},
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
    check(
        db_mock._state == {"current_step": "post_no_active_appointment", "context": {"lang": "en", "qr_hospital": HOSPITAL, "qr_scanned_at": scanned_at}},
        f"stale booking state is cleared but lang AND the hospital-QR scoping are kept so a Book Appointment tap can skip straight to this hospital's doctors, got {db_mock._state!r}",
    )


def test_book_appointment_after_no_appointment_found_skips_straight_to_hospital_doctors():
    print("\n--- Live-reported bug: QR scan -> Check Status -> no appointment -> Book Appointment asked for LOCATION instead of showing this hospital's doctors ---")
    scanned_at = conversation._clinic_now().isoformat()
    db_mock = _RecordingDb(
        initial_state={"current_step": "choosing_hospital_action", "context": {"lang": "en", "qr_hospital": HOSPITAL, "qr_scanned_at": scanned_at}},
        booked_appointments=[],
    )
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
                # Turn 1: Check Status finds nothing at this hospital.
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_status", "msg9")
                check(
                    db_mock._state["context"].get("qr_hospital") == HOSPITAL,
                    f"hospital-QR scoping must survive the no-active-appointment state clear, got {db_mock._state!r}",
                )
                wa_mock.buttons.clear()

                # Turn 2: tap the Book Appointment button THAT message offered.
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "start_booking", "msg10")

    run(_run())
    check(len(wa_mock.location_requests) == 0, f"must NOT ask for location -- the hospital is already known, got location_requests={wa_mock.location_requests!r}")
    check(len(wa_mock.lists) == 1, "goes straight to a doctor-choice list, same as tapping Book Appointment from the original welcome menu")
    check(len(wa_mock.lists[0][2]) == 2, "list carries this hospital's own doctors")
    check(lead_calls and lead_calls[0]["lead_type"] == "HospitalQRScan", f"still attributes the lead to the QR scan, got {lead_calls!r}")


def test_stale_hospital_menu_button_still_works_after_moving_to_a_later_step():
    print("\n--- Live-reported bug: tapping a STALE hospital-menu button while mid-way through a later step did nothing useful ---")
    # WhatsApp keeps every past interactive message tappable forever. A patient who tapped
    # Book Appointment, got the doctor-choice list, but then goes BACK and taps "My
    # Appointment" on the ORIGINAL welcome menu message was getting routed to whatever step's
    # handler is current (choosing_doctor here) -- which doesn't recognize hospbook_status as
    # valid input for ITS OWN step, and just replied "please choose a doctor from the list
    # above", silently swallowing the request entirely.
    db_mock = _RecordingDb(
        initial_state={
            "current_step": "choosing_doctor",
            "context": {"lang": "en", "qr_hospital": HOSPITAL, "qr_scanned_at": conversation._clinic_now().isoformat(), "doctor_options": {"d1": TWO_DOCTORS[0]}},
        },
        booked_appointments=[LOCAL_ROW_HOSP1],
    )
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=APPT_AT_HOSP1)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_status", "msg12")

    run(_run())
    check(len(wa_mock.buttons) == 1, f"shows the real appointment-status card, not a generic reprompt, got buttons={wa_mock.buttons!r} texts={wa_mock.texts!r}")
    status_text = wa_mock.buttons[0][0] if wa_mock.buttons else ""
    check("Dr. A" in status_text, f"resolves the actual appointment status, got {status_text!r}")
    check(not any("choose a doctor" in t.lower() for t in wa_mock.texts), f"must not fall through to the choosing_doctor step's generic reprompt, got texts={wa_mock.texts!r}")


def test_hospital_menu_button_still_works_days_after_the_scan_unlike_the_search_lock():
    print("\n--- The global hospbook_* button dispatch is intentionally NOT gated by the 15-minute search lock ---")
    # Peer-review finding: the global dispatch's design deliberately does not check
    # _qr_locked_hospital_id -- an explicit button tap naming its own hospital should always
    # work, unlike the 15-minute IMPLICIT search-scoping lock. That distinction was never
    # actually exercised with a genuinely stale (multi-day) qr_scanned_at -- every other test
    # for this button used a fresh timestamp, so a future change accidentally gating this on
    # the lock would have gone unnoticed.
    days_old_scan = (conversation._clinic_now() - timedelta(days=3)).isoformat()
    db_mock = _RecordingDb(
        initial_state={
            "current_step": "choosing_doctor",
            "context": {"lang": "en", "qr_hospital": HOSPITAL, "qr_scanned_at": days_old_scan, "doctor_options": {"d1": TWO_DOCTORS[0]}},
        },
        booked_appointments=[],
    )
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors_at_hospital", AsyncMock(return_value=TWO_DOCTORS)), \
             patch.object(conversation.hms_client, "record_lead", AsyncMock()):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "button_reply", "hospbook_book", "msg13")

    run(_run())
    check(len(wa_mock.lists) == 1, f"still shows this hospital's doctor list even 3 days after the scan, got lists={wa_mock.lists!r}")
    check(len(wa_mock.lists[0][2]) == 2, "list still carries this hospital's own doctors")


def test_doctor_name_via_nlu_global_intent_within_qr_lock_skips_location_too():
    print("\n--- Live-reported bug: a doctor name resolved via NLU (not the STEP_REGISTRY hot-swap) still asked for location during an active hospital-QR lock ---")
    # Distinct code path from test_typed_doctor_name_within_15_min_of_qr_scan_is_scoped_to_
    # that_hospital above: THAT one goes through _handle_awaiting_doctor_name (a step
    # handler). This one reproduces "Dr Payal Anand" typed as a free-form reply while
    # current_step is something else entirely (awaiting_patient_details, matching the live
    # report) -- NLU resolves it to a doctor_name entity, and handle_message's "Prioritize
    # NLU global intents" block (book_appointment/check_availability) used to check ONLY
    # city/patient_lat for whether it's safe to search, never the hospital-QR lock -- so it
    # asked for location even though the hospital was already fixed.
    from app.referee.intent_router import RoutedResult

    scanned_at = conversation._clinic_now().isoformat()
    db_mock = _RecordingDb(initial_state={
        "current_step": "awaiting_patient_details",
        "context": {"lang": "hg", "qr_hospital": HOSPITAL, "qr_scanned_at": scanned_at},
    })
    wa_mock = _RecordingWhatsApp()
    doctors_at_hospital = [{"doctorId": "d9", "fullName": "Dr. Payal Anand", "hospitalId": "hosp-1", "hospitalName": "Purnea General Hospital", "city": "Purnea"}]

    routed = RoutedResult(action="proceed_to_business_logic", intent="book_appointment", entities={"doctor_name": "Payal Anand"}, confidence=0.9)

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value={"intent": "book_appointment", "entities": {"doctor_name": "Payal Anand"}, "confidence": "high", "detected_language": None, "language_confidence": None})), \
             patch.object(conversation.intent_router, "route_intent", AsyncMock(return_value=routed)), \
             patch.object(conversation.city_index, "get_all_doctors", AsyncMock(return_value=doctors_at_hospital)), \
             patch.object(conversation.city_index, "get_index", AsyncMock(return_value={})), \
             patch.object(conversation.hms_client, "record_lead", AsyncMock()):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "Dr Payal Anand", "msg11")

    run(_run())
    check(len(wa_mock.location_requests) == 0, f"must NOT ask for location -- the hospital-QR lock already fixes the hospital, got location_requests={wa_mock.location_requests!r}")
    check(len(wa_mock.texts) >= 1, f"resolves the doctor directly instead, got texts={wa_mock.texts!r}")


def test_typed_doctor_name_within_15_min_of_qr_scan_is_scoped_to_that_hospital():
    print("\n--- Typing a doctor's name within 15 min of the QR scan is silently scoped to that hospital ---")
    # Real scenario the 15-minute hospital-QR search lock exists for: the patient doesn't
    # tap "Book Appointment" -- they navigate to "search by doctor name" and just type a
    # name. qr_hospital/qr_scanned_at survive that navigation (context is always spread
    # forward, never rebuilt from scratch on this path -- see _transition_to), so the lock
    # should still apply here exactly as it does from the menu button.
    same_name_at_two_hospitals = [
        {"doctorId": "d1", "fullName": "Dr. A. Sharma", "hospitalId": "hosp-1", "hospitalName": "Purnea General Hospital", "city": "Purnea"},
        {"doctorId": "d2", "fullName": "Dr. A. Sharma", "hospitalId": "hosp-2", "hospitalName": "Star Hospital", "city": "Purnea"},
    ]
    db_mock = _RecordingDb(initial_state={
        "current_step": "awaiting_doctor_name",
        "context": {
            "lang": "en", "qr_hospital": HOSPITAL,
            "qr_scanned_at": conversation._clinic_now().isoformat(),
        },
    })
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.city_index, "get_all_doctors", AsyncMock(return_value=same_name_at_two_hospitals)), \
             patch.object(conversation.city_index, "get_index", AsyncMock(return_value={})), \
             patch.object(conversation.hms_client, "record_lead", AsyncMock()):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "Dr Sharma", "msg8")

    run(_run())
    # Unscoped, "Sharma" would match BOTH doctors (one at each hospital) -> ambiguous "many".
    # Locked to hosp-1, only ITS Sharma is even in the candidate pool -> resolves straight to
    # a single match, no disambiguation needed and no leak of the hosp-2 doctor.
    check(len(wa_mock.texts) >= 1, f"resolves directly to a single-match message, got texts={wa_mock.texts!r}")
    check(
        any("hosp-2" not in t and "Star Hospital" not in t for t in wa_mock.texts),
        f"must never surface the same-named doctor at the OTHER hospital, got texts={wa_mock.texts!r}",
    )
    check(len(wa_mock.lists) == 0, "must not show a disambiguation list -- the lock already narrowed it to one")


if __name__ == "__main__":
    test_trigger_pattern_matches_and_ignores()
    test_invalid_hospital_code_sends_error_and_preserves_state()
    test_valid_hospital_lang_known_shows_welcome_menu()
    test_tapping_book_from_menu_shows_hospital_doctor_list_and_records_qr_lead()
    test_hospital_doctor_fetch_failure_sends_generic_retry_not_a_doctor_not_found_message()
    test_valid_hospital_no_lang_yet_asks_language_then_resumes_to_the_menu()
    test_tapping_check_status_shows_only_this_hospitals_appointment()
    test_check_status_with_no_appointment_at_this_hospital_is_scoped_not_global()
    test_book_appointment_after_no_appointment_found_skips_straight_to_hospital_doctors()
    test_stale_hospital_menu_button_still_works_after_moving_to_a_later_step()
    test_hospital_menu_button_still_works_days_after_the_scan_unlike_the_search_lock()
    test_doctor_name_via_nlu_global_intent_within_qr_lock_skips_location_too()
    test_typed_doctor_name_within_15_min_of_qr_scan_is_scoped_to_that_hospital()

    print("\n" + "=" * 50)
    if failures:
        print(f"HOSPITAL BOOKING QR TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL HOSPITAL BOOKING QR TESTS PASSED")
