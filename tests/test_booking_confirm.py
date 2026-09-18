"""
Regression tests for peer-review Batch 3's C1 (double-booking): _handle_confirming /
_do_book_appointment previously had NO coverage at all (flagged as T3 -- "the money path,
untested"). Covers:

1. The actual bug: a fast double-tap producing two concurrent confirms for the same phone
   must not both reach hms_client.book_appointment -- guarded by a short-lived per-phone
   Redis lock (booking:confirm-lock:{phone}).
2. The agreed soft duplicate check: an existing appointment with identical patient details
   (name/age/gender/doctor/date) triggers a warn-and-confirm, not a hard block or a silent
   second booking -- "Book anyway" proceeds, "Change details" reopens the patient-details form
   (NOT a full cancel -- explicit product decision).
3. The pre-existing happy path (never had a regression test before this).

Same style as test_appointment_cancel_reschedule.py -- stubs aioodbc/redis before importing
app modules, mocks db/whatsapp_client/hms_client directly, no pytest.
Run directly: python3 tests/test_booking_confirm.py
"""

import asyncio
import os
import sys
import types
from datetime import date
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

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        return True

    async def lpush(self, key, value):
        self.data.setdefault(key, []).append(value)
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
    def __init__(self):
        self.texts: list[str] = []
        self.buttons: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))


class _RecordingDb:
    def __init__(self, has_pending=False, has_duplicate=False):
        self._has_pending = has_pending
        self._has_duplicate = has_duplicate
        self._state = None
        self.cleared = False
        self.created_appointments: list[dict] = []
        self.booked: list[tuple] = []
        self.failed: list[str] = []
        self.duplicate_check_calls: list[tuple] = []

    async def has_pending_appointment(self, phone, preferred_date):
        return self._has_pending

    async def has_duplicate_appointment_details(self, phone, preferred_date, doctor_id, name, age, gender):
        self.duplicate_check_calls.append((phone, preferred_date, doctor_id, name, age, gender))
        return self._has_duplicate

    async def create_pending_appointment(self, phone, preferred_date, **kwargs):
        row_id = f"row-{len(self.created_appointments)}"
        self.created_appointments.append({"phone": phone, "preferred_date": preferred_date, **kwargs})
        return row_id

    async def mark_appointment_booked(self, row_id, hms_appointment_id):
        self.booked.append((row_id, hms_appointment_id))

    async def mark_appointment_failed(self, row_id):
        self.failed.append(row_id)

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self.cleared = True
        self._state = None


def _base_context(**overrides):
    context = {
        "lang": "en",
        "preferred_date": "2026-09-20",
        "doctor_id": "doc-1",
        "doctor_name": "Dr. Sharma",
        "shift_label": "morning",
        "patient_display_name": "Riya",
        "patient_age": 8,
        "patient_gender": "Female",
    }
    context.update(overrides)
    return context


def test_confirm_books_normally_and_releases_the_lock():
    print("\n--- Happy path: never had a regression test before this batch ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "book_appointment", AsyncMock(return_value={"appointmentId": "appt-1"})):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming(client, phone, "Sender Name", "button_reply", "confirm", _base_context())

    run(_run())
    check(len(db_mock.created_appointments) == 1, "creates exactly one pending appointment")
    check(db_mock.created_appointments[0]["doctor_id"] == "doc-1", "doctor_id is threaded through to the DB row (new in this batch)")
    check(db_mock.booked == [("row-0", "appt-1")], "marks the appointment booked with HMS's real id")
    check(db_mock.cleared, "conversation state is cleared after a successful booking")
    check(f"booking:confirm-lock:{phone}" not in _mock_redis_instance.data, "the confirm-lock is released once booking finishes")


def test_double_tap_second_confirm_is_told_to_wait_not_booked_again():
    print("\n--- The actual C1 bug: a fast double-tap must not reach book_appointment twice ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"
    book_calls = {"count": 0}

    async def _book(*args, **kwargs):
        book_calls["count"] += 1
        return {"appointmentId": "appt-1"}

    async def _run():
        # Simulates the SECOND confirm arriving while the first is still "in flight" --
        # the lock is already held (the first confirm's own lock acquisition), mirroring
        # what a real concurrent double-tap looks like without needing real concurrency.
        await _mock_redis_instance.set(f"booking:confirm-lock:{phone}", "1", nx=True, ex=20)
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "book_appointment", _book):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming(client, phone, "Sender Name", "button_reply", "confirm", _base_context())

    run(_run())
    check(book_calls["count"] == 0, "book_appointment is never called while the lock is already held")
    check(len(db_mock.created_appointments) == 0, "no pending-appointment row is created either")
    check(
        wa_mock.texts and "wait" in wa_mock.texts[0].lower(),
        f"the patient is told to wait, not left silent or double-booked, got {wa_mock.texts!r}",
    )


def test_lock_is_released_even_when_booking_fails_so_a_retry_is_not_blocked():
    print("\n--- Lock must not be held for the full TTL on failure -- a retry (e.g. worker.py's "
          "single-retry-on-crash) must be able to proceed immediately ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "book_appointment", AsyncMock(side_effect=HmsApiError("rejected"))):
            async with httpx.AsyncClient() as client:
                try:
                    await conversation._handle_confirming(client, phone, "Sender Name", "button_reply", "confirm", _base_context())
                except HmsApiError:
                    pass

    run(_run())
    check(db_mock.failed == ["row-0"], "the pending-appointment row is marked failed")
    check(f"booking:confirm-lock:{phone}" not in _mock_redis_instance.data, "the lock is released immediately on failure, not left to expire")


def test_already_pending_still_works_and_releases_the_lock():
    print("\n--- Pre-existing has_pending_appointment check must still work under the new lock ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb(has_pending=True)
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"

    async def _run():
        with patch.object(conversation, "db", db_mock), patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming(client, phone, "Sender Name", "button_reply", "confirm", _base_context())

    run(_run())
    check(len(db_mock.created_appointments) == 0, "no new appointment created when one is already pending")
    check(db_mock.cleared, "state is cleared")
    check(f"booking:confirm-lock:{phone}" not in _mock_redis_instance.data, "lock released")


def test_duplicate_details_warns_instead_of_silently_booking_again():
    print("\n--- The agreed soft duplicate check: same patient/doctor/date already exists ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb(has_duplicate=True)
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "book_appointment", AsyncMock(return_value={"appointmentId": "appt-1"})):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming(client, phone, "Sender Name", "button_reply", "confirm", _base_context())

    run(_run())
    check(len(db_mock.created_appointments) == 0, "does NOT silently create a second identical appointment")
    check(len(db_mock.duplicate_check_calls) == 1, "checks for a duplicate exactly once")
    check(len(wa_mock.buttons) == 1, "shows the duplicate warning as a button prompt")
    check(
        db_mock._state is not None and db_mock._state["current_step"] == "confirming_duplicate_booking",
        f"transitions to the new confirming_duplicate_booking step, got {db_mock._state!r}",
    )
    check(f"booking:confirm-lock:{phone}" not in _mock_redis_instance.data, "lock is released while awaiting the patient's answer, not held")


def test_duplicate_warning_book_anyway_proceeds_without_looping():
    print("\n--- 'Book anyway' on the duplicate warning must actually book, not re-show the same warning ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb(has_duplicate=True)  # would still say "duplicate" if asked again
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"
    context = _base_context(duplicate_confirmed=False)

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "book_appointment", AsyncMock(return_value={"appointmentId": "appt-1"})):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_duplicate_booking(client, phone, "button_reply", "confirm", context)

    run(_run())
    check(len(db_mock.duplicate_check_calls) == 0, "duplicate_confirmed short-circuits the check -- never re-queried, so it can't loop")
    check(len(db_mock.created_appointments) == 1, "the appointment IS actually created this time")
    check(db_mock.booked == [("row-0", "appt-1")], "and actually booked")


def test_duplicate_warning_change_details_reopens_the_form_not_a_full_cancel():
    print("\n--- Explicit product decision: 'Change details' means fix a mistake, not restart ---")
    _mock_redis_instance.data.clear()
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    phone = "919876543210"
    flow_calls = []

    async def _fake_send_patient_details_flow(client, phone, context):
        flow_calls.append(context)

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_send_patient_details_flow", _fake_send_patient_details_flow):
            async with httpx.AsyncClient() as client:
                await conversation._handle_confirming_duplicate_booking(
                    client, phone, "button_reply", "change_details", _base_context()
                )

    run(_run())
    check(len(flow_calls) == 1, "reopens the patient-details Flow")
    check(not db_mock.cleared, "does NOT clear conversation state -- doctor/date must survive, unlike a real cancel")
    check(
        flow_calls[0].get("doctor_id") == "doc-1",
        f"doctor/date context is preserved into the reopened form, got {flow_calls[0]!r}",
    )


if __name__ == "__main__":
    test_confirm_books_normally_and_releases_the_lock()
    test_double_tap_second_confirm_is_told_to_wait_not_booked_again()
    test_lock_is_released_even_when_booking_fails_so_a_retry_is_not_blocked()
    test_already_pending_still_works_and_releases_the_lock()
    test_duplicate_details_warns_instead_of_silently_booking_again()
    test_duplicate_warning_book_anyway_proceeds_without_looping()
    test_duplicate_warning_change_details_reopens_the_form_not_a_full_cancel()

    print("\n" + "=" * 50)
    if failures:
        print(f"BOOKING CONFIRM TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL BOOKING CONFIRM TESTS PASSED")
