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


_LIVE_APPT = {"success": True, "appointment": {"appointmentId": "appt-1", "doctorName": "Dr. A", "apptDate": "2026-08-20", "statusCode": "FUTURE"}}
_LIVE_APPT_2 = {"success": True, "appointment": {"appointmentId": "appt-2", "doctorName": "Dr. B", "apptDate": "2026-08-21", "statusCode": "FUTURE"}}


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
    check(len(wa_mock.texts) == 1, "sends exactly one 'no active appointment' message")
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
    check(len(wa_mock.texts) == 1, "treats it as having no active appointment")
    check(db_mock.cleared is True, "clears conversation state rather than confirming a cancel on an already-cancelled appointment")


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


def test_typed_cancel_with_in_progress_booking_abandons_it_not_a_real_appointment():
    print("\n--- Typing 'cancel' mid-way through a NEW booking abandons it, doesn't touch a real appointment ---")
    booking = booking_slots.empty()
    booking_slots.fill(booking, "lang", "en", source="user")
    booking_slots.fill(booking, "doctor", {"id": "d1", "fullName": "Dr. Avinash"}, raw="Avinash", source="user")
    context = {"lang": "en", "booking": booking}
    # A real, unrelated booked appointment exists -- if the fix regresses, this is what
    # would get offered for cancellation instead of the in-progress booking being abandoned.
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
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(return_value=nlu_result)):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "User", "text", "cancel")

    run(_run())
    check(db_mock.get_booked_calls == 0, "never looks up real booked appointments -- the in-progress booking is what gets abandoned")
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


if __name__ == "__main__":
    test_no_active_appointment_sends_message_and_clears_state()
    test_single_live_appointment_goes_straight_to_confirmation()
    test_multiple_live_appointments_offers_disambiguation_list()
    test_cancelled_local_candidate_is_filtered_out_via_live_reverification()
    test_confirming_cancel_confirm_calls_hms_client_and_updates_local_status()
    test_confirming_cancel_decline_does_not_call_hms_client()
    test_confirming_cancel_server_rejection_surfaces_message_without_crashing()
    test_confirming_reschedule_confirm_calls_hms_client_with_parsed_date()
    test_typed_cancel_with_in_progress_booking_abandons_it_not_a_real_appointment()
    test_typed_cancel_with_no_in_progress_booking_still_resolves_a_real_appointment()

    print(f"\n{'=' * 60}")
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("All checks passed.")
