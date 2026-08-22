"""
Sanity checks for the real appointment cancel/reschedule flow: _start_appointment_action_flow
and the confirm/decline handlers it leads to. Same style as test_checkin.py -- stubs
aioodbc/redis before importing app modules, mocks hms_client/db/whatsapp_client calls directly,
no pytest. Run directly: python test_appointment_cancel_reschedule.py
"""

import asyncio
import os
import sys
import types
from datetime import date, timedelta
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
from app.decision_maker import booking_slots  # noqa: E402
from app.i18n import t  # noqa: E402

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


class _RecordingDb:
    def __init__(self, initial_state: dict | None = None, booked_appointments: list[dict] | None = None):
        self._state = initial_state
        self._booked = booked_appointments or []
        self.cleared = False
        self.cancelled_locally: list[str] = []
        self.rescheduled_locally: list[tuple] = []
        self.get_booked_calls = 0

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self.cleared = True
        self._state = None

    async def get_booked_appointments_for_phone(self, phone):
        self.get_booked_calls += 1
        return self._booked

    async def mark_appointment_cancelled_locally(self, hms_appointment_id):
        self.cancelled_locally.append(hms_appointment_id)

    async def mark_appointment_rescheduled_locally(self, hms_appointment_id, new_date):
        self.rescheduled_locally.append((hms_appointment_id, new_date))


# Dynamic, not hardcoded -- a fixed past date would eventually fall outside
# _is_appointment_stale's 1-day grace window as real calendar time moves past it.
_TOMORROW = (date.today() + timedelta(days=1)).isoformat()
_DAY_AFTER = (date.today() + timedelta(days=2)).isoformat()
_LIVE_APPT = {"success": True, "appointment": {"appointmentId": "appt-1", "doctorName": "Dr. A", "apptDate": _TOMORROW, "statusCode": "FUTURE"}}
_LIVE_APPT_2 = {"success": True, "appointment": {"appointmentId": "appt-2", "doctorName": "Dr. B", "apptDate": _DAY_AFTER, "statusCode": "FUTURE"}}


def test_no_active_appointment_sends_message_and_clears_state():
    print("\n--- No active appointment ---")
    db_mock = _RecordingDb(booked_appointments=[])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation, "db", db_mock), patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(len(wa_mock.buttons) == 1, "sends exactly one 'no active appointment' message with a Book Appointment button")
    check(
        wa_mock.buttons and wa_mock.buttons[0][1] and wa_mock.buttons[0][1][0][0] == "start_booking",
        f"offers a tappable start_booking button, not just the typed fallback, got {wa_mock.buttons!r}",
    )
    check(db_mock.cleared is True, "clears conversation state")


def test_single_live_appointment_goes_straight_to_confirmation():
    print("\n--- Single live appointment -> confirmation prompt ---")
    db_mock = _RecordingDb(booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
          "transitions straight to confirming_appointment_cancel (no disambiguation needed)")
    check(db_mock._state["context"]["appt_action_id"] == "appt-1", "resolved appointment id is stored in context")
    check(len(wa_mock.buttons) == 1, "sends a Yes/No confirmation button prompt")


def test_multiple_live_appointments_offers_disambiguation_list():
    print("\n--- Multiple live appointments -> disambiguation list ---")
    db_mock = _RecordingDb(booked_appointments=[
        {"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"},
        {"id": "row2", "hms_appointment_id": "appt-2", "preferred_date": "2026-08-21"},
    ])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}

    async def _get_appointment(appt_id):
        return _LIVE_APPT if appt_id == "appt-1" else _LIVE_APPT_2

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(side_effect=_get_appointment)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="reschedule", new_date_str="2026-08-25")

    run(_run())
    check(db_mock._state["current_step"] == "choosing_appointment_to_reschedule", "transitions to the disambiguation step")
    check(len(wa_mock.lists) == 1 and len(wa_mock.lists[0][2]) == 2, "sends a list with both live candidates")


def test_multiple_live_appointments_with_status_action_uses_its_own_step_and_stays_read_only():
    print("\n--- Multiple live appointments + check_my_appointment -> its own disambiguation step, then a read-only status ---")
    db_mock = _RecordingDb(booked_appointments=[
        {"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"},
        {"id": "row2", "hms_appointment_id": "appt-2", "preferred_date": "2026-08-21"},
    ])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}

    async def _get_appointment(appt_id):
        return _LIVE_APPT if appt_id == "appt-1" else _LIVE_APPT_2

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(side_effect=_get_appointment)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="status")
                check(
                    db_mock._state["current_step"] == "choosing_appointment_to_view",
                    f"transitions to its own status-disambiguation step, not cancel's or reschedule's, got {db_mock._state['current_step']!r}",
                )
                # Patient picks one from the list.
                step, ctx = db_mock._state["current_step"], db_mock._state["context"]
                await conversation._handle_choosing_appointment_to_view(client, "919876543210", "list_reply", "appt-2", ctx)

    run(_run())
    check(len(wa_mock.buttons) == 0, "picking one from the list still ends in a read-only status, not a Confirm/Cancel prompt")
    check(wa_mock.texts and "Dr. B" in wa_mock.texts[-1], f"shows the CHOSEN appointment's details, got {wa_mock.texts!r}")
    check(db_mock.cleared is True, "clears conversation state once the status is shown")


def test_cancelled_local_candidate_is_filtered_out_via_live_reverification():
    print("\n--- Locally-'booked' but actually-cancelled candidate is excluded ---")
    # Local status says 'booked', but the hospital side already cancelled it -- the live
    # GET /public/appointments/{id} re-check must catch this, not just trust local status.
    db_mock = _RecordingDb(booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}
    stale = {"success": True, "appointment": {"appointmentId": "appt-1", "doctorName": "Dr. A", "apptDate": "2026-08-20", "statusCode": "CANCELLED"}}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=stale)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(len(wa_mock.buttons) == 1, "treats it as having no active appointment")
    check(db_mock.cleared is True, "clears conversation state rather than confirming a cancel on an already-cancelled appointment")


def test_appointment_more_than_a_day_past_its_date_is_excluded_even_if_status_is_stale():
    print("\n--- statusCode stuck on FUTURE for a many-days-old appointment -- date-based safety net catches it ---")
    # 1HMS's statusCode is the primary live/cancellable signal, but if it's ever stale (not
    # flipped to COMPLETED even though the date has clearly passed), this date-level check is
    # the defensive backstop -- a 1-day grace period, not hours, since apptDate is date-only.
    long_past = (date.today() - timedelta(days=5)).isoformat()
    db_mock = _RecordingDb(booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": long_past}])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}
    stuck = {"success": True, "appointment": {"appointmentId": "appt-1", "doctorName": "Dr. A", "apptDate": long_past, "statusCode": "FUTURE"}}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=stuck)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(len(wa_mock.buttons) == 1, "treats a many-days-old appointment as not active, even with statusCode still FUTURE")
    check(db_mock.cleared is True, "clears conversation state")


def test_appointment_from_yesterday_is_still_within_the_grace_window():
    print("\n--- Yesterday's appointment is still offered -- the grace period isn't same-day only ---")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    db_mock = _RecordingDb(booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": yesterday}])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}
    recent = {"success": True, "appointment": {"appointmentId": "appt-1", "doctorName": "Dr. A", "apptDate": yesterday, "statusCode": "FUTURE"}}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=recent)):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
        f"yesterday's appointment is still within the 1-day grace period, got {db_mock._state!r}",
    )


def test_confirming_cancel_confirm_calls_hms_client_and_updates_local_status():
    print("\n--- Confirming cancel: Yes -> real cancel ---")
    context = {"lang": "en", "appt_action_id": "appt-1"}
    db_mock = _RecordingDb(initial_state={"current_step": "confirming_appointment_cancel", "context": context})
    wa_mock = _RecordingWhatsApp()
    cancel_result = {"success": True, "message": "Appointment cancelled successfully."}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "cancel_appointment", AsyncMock(return_value=cancel_result)) as cancel_mock:
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_appointment_cancel(client, "919876543210", "button_reply", "confirm", context)
            cancel_mock.assert_called_once_with("appt-1", mobile="919876543210")

    run(_run())
    check(db_mock.cancelled_locally == ["appt-1"], "marks the appointment cancelled in the bot's own local table")
    check(any("cancelled successfully" in t for t in wa_mock.texts), "relays the API's own success message")
    check(db_mock.cleared is True, "clears conversation state after completing the action")


def test_confirming_cancel_decline_does_not_call_hms_client():
    print("\n--- Confirming cancel: No -> nothing happens ---")
    context = {"lang": "en", "appt_action_id": "appt-1"}
    db_mock = _RecordingDb(initial_state={"current_step": "confirming_appointment_cancel", "context": context})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "cancel_appointment", AsyncMock()) as cancel_mock:
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_appointment_cancel(client, "919876543210", "button_reply", "cancel", context)
            cancel_mock.assert_not_called()

    run(_run())
    check(db_mock.cancelled_locally == [], "does not mark anything cancelled locally")
    check(db_mock.cleared is True, "still clears conversation state on decline")


def test_confirming_cancel_server_rejection_surfaces_message_without_crashing():
    print("\n--- Confirming cancel: server-side rejection (e.g. already cancelled elsewhere) ---")
    context = {"lang": "en", "appt_action_id": "appt-1"}
    db_mock = _RecordingDb(initial_state={"current_step": "confirming_appointment_cancel", "context": context})
    wa_mock = _RecordingWhatsApp()
    reject_result = {"success": False, "message": "This appointment is already cancelled."}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "cancel_appointment", AsyncMock(return_value=reject_result)):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_appointment_cancel(client, "919876543210", "button_reply", "confirm", context)

    run(_run())
    check(db_mock.cancelled_locally == [], "does not mark cancelled locally on a server-side rejection")
    check(any("already cancelled" in t for t in wa_mock.texts), "relays the specific rejection reason to the patient")


def test_confirming_reschedule_confirm_calls_hms_client_with_parsed_date():
    print("\n--- Confirming reschedule: Yes -> real reschedule ---")
    context = {"lang": "en", "appt_action_id": "appt-1", "appt_action_new_date": "2026-08-25"}
    db_mock = _RecordingDb(initial_state={"current_step": "confirming_appointment_reschedule", "context": context})
    wa_mock = _RecordingWhatsApp()
    reschedule_result = {"success": True, "message": "Appointment rescheduled successfully."}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "reschedule_appointment", AsyncMock(return_value=reschedule_result)) as reschedule_mock:
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_appointment_reschedule(client, "919876543210", "button_reply", "confirm", context)
            check(reschedule_mock.call_args.kwargs["mobile"] == "919876543210", "passes the visitor's own phone number as the mobile cross-check")

    run(_run())
    check(len(db_mock.rescheduled_locally) == 1 and db_mock.rescheduled_locally[0][0] == "appt-1", "updates local record with the new date")
    check(any("rescheduled successfully" in t for t in wa_mock.texts), "relays the API's own success message")


def test_typed_cancel_prioritizes_a_real_appointment_over_a_leftover_in_progress_booking():
    print("\n--- Typing 'cancel' with a real appointment on file always wins over stale in-progress-booking data ---")
    # Live-reported bug: a long-abandoned draft booking (started once, never finished, never
    # cleared) sitting in the clipboard was mistaken for something actively "in progress right
    # now", so "cancel my appointment" answered with a generic "cancelled, start over" message
    # instead of ever looking up the patient's real appointment. A real, locally-recorded
    # appointment must always take priority, regardless of any leftover draft data.
    booking = booking_slots.empty()
    booking_slots.fill(booking, "lang", "en", source="user")
    booking_slots.fill(booking, "doctor", {"id": "d1", "fullName": "Dr. Avinash"}, raw="Avinash", source="user")
    context = {"lang": "en", "booking": booking}
    db_mock = _RecordingDb(
        initial_state={"current_step": "awaiting_doctor_name", "context": context},
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "cancel_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel")

    run(_run())
    check(db_mock.get_booked_calls == 1, "looks up real booked appointments even with a stale in-progress booking present")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
        f"offers to cancel the real appointment instead of just abandoning the draft, got {db_mock._state!r}",
    )
    check(t("cancelled", "en") not in wa_mock.texts, "must not silently answer with the generic abandon message when a real appointment exists")


def test_typed_cancel_abandons_in_progress_booking_only_when_no_real_appointment_exists():
    print("\n--- Typing 'cancel' mid-way through a NEW booking abandons it when there's no real appointment to check ---")
    booking = booking_slots.empty()
    booking_slots.fill(booking, "lang", "en", source="user")
    booking_slots.fill(booking, "doctor", {"id": "d1", "fullName": "Dr. Avinash"}, raw="Avinash", source="user")
    context = {"lang": "en", "booking": booking}
    db_mock = _RecordingDb(
        initial_state={"current_step": "awaiting_doctor_name", "context": context},
        booked_appointments=[],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "cancel_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel")

    run(_run())
    check(wa_mock.texts == [t("cancelled", "en")], f"sends the plain 'cancelled' message, not a real-appointment prompt, got {wa_mock.texts!r}")
    check(db_mock.cleared is True, "clears conversation state")


def test_typed_cancel_with_no_in_progress_booking_still_resolves_a_real_appointment():
    print("\n--- Typing 'cancel' with nothing in progress still falls through to real-appointment cancel ---")
    context = {"lang": "en", "booking": booking_slots.empty()}
    db_mock = _RecordingDb(
        initial_state={"current_step": None, "context": context},
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "cancel_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel")

    run(_run())
    check(db_mock.get_booked_calls == 1, "looks up real booked appointments when nothing was in progress")
    check(db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
          "still offers to cancel the real appointment, same as before this fix")


def test_cancel_as_the_very_first_message_of_a_fresh_conversation_still_resolves_a_real_appointment():
    print("\n--- 'cancel my appointment' as literally the first message (e.g. right after a previous cancel cleared state) ---")
    # Live-reported: no prior conversation_state at all (has_lang_init is False) routed
    # cancel_appointment/reschedule_appointment through the SAME first-message shortcut every
    # other intent uses, which only ever leads to the booking flow -- the cancel intent was
    # silently dropped. High-confidence language here exercises the immediate path in
    # _confirm_or_start_language (no confirming_language step in between).
    db_mock = _RecordingDb(
        initial_state=None,
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "cancel_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel my appointment")

    run(_run())
    check(db_mock.get_booked_calls == 1, "looks up the real appointment instead of falling into the booking flow")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
        f"offers to cancel the real appointment, not a location-share prompt, got {db_mock._state!r}",
    )
    check(not any("location" in t.lower() for t in wa_mock.texts), "must never mention sharing location for a cancel request")


def test_cancel_as_first_message_with_low_confidence_language_still_resolves_after_confirming():
    print("\n--- Same, but language needs an explicit confirm step first (low-confidence guess) ---")
    db_mock = _RecordingDb(
        initial_state=None,
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "cancel_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "low"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel my appointment")
                check(
                    db_mock._state is not None and db_mock._state["current_step"] == "confirming_language",
                    f"first asks to confirm the guessed language, got {db_mock._state!r}",
                )
                await conversation.handle_message(client, "919876543210", "User", "button_reply", "lang_confirm_yes")

    run(_run())
    check(db_mock.get_booked_calls == 1, "resolves the real appointment once language is confirmed")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "confirming_appointment_cancel",
        f"offers to cancel the real appointment after confirming language, got {db_mock._state!r}",
    )


def test_check_my_appointment_as_first_message_shows_a_read_only_status_not_a_cancel_prompt():
    print("\n--- 'do I have a booking?' as the first message -- read-only status, not a Cancel confirm prompt ---")
    # Live-reported bugs, in order:
    # 1. A plain status question ("mujhe ye btao mera koi booking already hai") had no
    #    matching intent at all, so it fell into the generic booking flow (welcome +
    #    "share your location") instead of ever answering whether a booking exists.
    # 2. After adding check_my_appointment and merging it into cancel_appointment's own
    #    resolution logic, it showed "Cancel your appointment with X on Y? Confirm/Cancel" --
    #    misleading for a patient who only asked whether they have a booking, not to cancel
    #    it. action="status" (see _INTENT_TO_APPT_ACTION in appointment_actions.py) fixes
    #    this: same appointment lookup, but a plain read-only message, no Confirm/Cancel.
    db_mock = _RecordingDb(
        initial_state=None,
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "check_my_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "do I have any appointment")

    run(_run())
    check(db_mock.get_booked_calls == 1, "looks up the real appointment instead of falling into the booking flow")
    check(len(wa_mock.buttons) == 0, "must NOT show a Confirm/Cancel button for a plain status question")
    check(
        wa_mock.texts and "Dr. A" in wa_mock.texts[-1],
        f"sends a plain text status message naming the doctor, got {wa_mock.texts!r}",
    )
    check(not any("location" in t.lower() for t in wa_mock.texts), "must never mention sharing location for a status question")
    check(db_mock.cleared is True, "clears conversation state -- nothing further to confirm")


def test_check_my_appointment_from_a_returning_user_also_shows_read_only_status():
    print("\n--- Same, but from a returning user (already has lang set) -- the OTHER routing branch ---")
    context = {"lang": "en", "booking": booking_slots.empty()}
    db_mock = _RecordingDb(
        initial_state={"current_step": None, "context": context},
        booked_appointments=[{"id": "row1", "hms_appointment_id": "appt-1", "preferred_date": "2026-08-20"}],
    )
    wa_mock = _RecordingWhatsApp()
    nlu_result = {"intent": "check_my_appointment", "entities": {}, "confidence": "high", "detected_language": "en", "language_confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.intent_router, "db", db_mock), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)), \
             patch.object(conversation.hms_client, "get_appointment", AsyncMock(return_value=_LIVE_APPT)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "do I have any appointment")

    run(_run())
    check(db_mock.get_booked_calls == 1, "looks up the real appointment for a returning user too")
    check(len(wa_mock.buttons) == 0, "must NOT show a Confirm/Cancel button for a plain status question, regardless of has_lang_init")
    check(db_mock.cleared is True, "clears conversation state")


def test_no_active_appointment_offers_a_tappable_book_appointment_button():
    print("\n--- 'no active appointment' response offers a tappable Book Appointment button, not just typed text ---")
    db_mock = _RecordingDb(booked_appointments=[])
    wa_mock = _RecordingWhatsApp()
    context = {"lang": "en"}

    async def _run():
        with patch.object(conversation, "db", db_mock), patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await conversation._start_appointment_action_flow(client, "919876543210", context, None, action="cancel")

    run(_run())
    check(len(wa_mock.buttons) == 1, "sends exactly one button prompt")
    body_text, buttons = wa_mock.buttons[0]
    check("book" in body_text.lower() or "appointment" in body_text.lower(), f"body still explains there's nothing to cancel, got {body_text!r}")
    check(buttons == [("start_booking", "Book Appointment")], f"offers a single start_booking button, got {buttons!r}")


def test_tapping_the_book_appointment_button_starts_a_fresh_booking_regardless_of_state():
    print("\n--- Tapping the Book Appointment button always starts fresh, even with no prior conversation_state ---")
    # The button is offered right after _start_appointment_action_flow calls
    # db.clear_conversation_state -- so by the time a patient could actually tap it, there is
    # no conversation_state left for this phone at all. The tap must still work.
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "button_reply", "start_booking")

    run(_run())
    check(len(wa_mock.lists) == 1, "sends the language-choice list, same as any other fresh conversation start")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "choosing_language",
        f"transitions to choosing_language, got {db_mock._state!r}",
    )


if __name__ == "__main__":
    test_no_active_appointment_sends_message_and_clears_state()
    test_single_live_appointment_goes_straight_to_confirmation()
    test_multiple_live_appointments_offers_disambiguation_list()
    test_multiple_live_appointments_with_status_action_uses_its_own_step_and_stays_read_only()
    test_cancelled_local_candidate_is_filtered_out_via_live_reverification()
    test_appointment_more_than_a_day_past_its_date_is_excluded_even_if_status_is_stale()
    test_appointment_from_yesterday_is_still_within_the_grace_window()
    test_confirming_cancel_confirm_calls_hms_client_and_updates_local_status()
    test_confirming_cancel_decline_does_not_call_hms_client()
    test_confirming_cancel_server_rejection_surfaces_message_without_crashing()
    test_confirming_reschedule_confirm_calls_hms_client_with_parsed_date()
    test_typed_cancel_prioritizes_a_real_appointment_over_a_leftover_in_progress_booking()
    test_typed_cancel_abandons_in_progress_booking_only_when_no_real_appointment_exists()
    test_typed_cancel_with_no_in_progress_booking_still_resolves_a_real_appointment()
    test_cancel_as_the_very_first_message_of_a_fresh_conversation_still_resolves_a_real_appointment()
    test_cancel_as_first_message_with_low_confidence_language_still_resolves_after_confirming()
    test_check_my_appointment_as_first_message_shows_a_read_only_status_not_a_cancel_prompt()
    test_check_my_appointment_from_a_returning_user_also_shows_read_only_status()
    test_no_active_appointment_offers_a_tappable_book_appointment_button()
    test_tapping_the_book_appointment_button_starts_a_fresh_booking_regardless_of_state()

    print(f"\n{'=' * 60}")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed.")
