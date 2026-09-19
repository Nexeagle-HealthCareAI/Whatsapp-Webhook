"""
Regression tests for the follow-up reply bug: scheduler.py's day-after-visit "how are you
feeling?" message was sent completely outside the conversation state machine, so a patient's
reply landed on a blank state and got NLU-classified/routed as if from a brand-new patient --
a casual "I'm feeling fine" was misread as a symptom description and answered with a
nonsensical "couldn't confidently match that to a specialty" list.

Covers both halves of the fix:
1. scheduler.py now saves conversation_state (current_step="awaiting_followup_reply") right
   after a successful send.
2. app/conversation/followup.py + __init__.py's _maybe_handle_followup_reply_step handle
   every reply scenario: wants to book, feeling fine, feeling unwell (both word orders --
   "accha nahi" and "nahi accha" both mean "not good"), an emergency reply, a button tap on
   the follow-up-booking offer, and genuinely unclear text (must fall back to casual chat,
   NEVER to symptom/specialty matching -- the actual regression test for the original bug).

Same style as test_booking_confirm.py -- stubs aioodbc/redis before importing app modules,
mocks conversation.db/whatsapp_client/safety/nlu_client directly, no pytest.
Run directly: python3 tests/test_followup_reply.py
"""

import asyncio
import os
import sys
import types
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
from app.conversation import followup  # noqa: E402
import scheduler  # noqa: E402

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

    async def send_typing_indicator(self, client, message_id):
        pass


class _RecordingDb:
    def __init__(self):
        self.cleared = False
        self._state = None
        self.save_calls: list[tuple] = []

    async def clear_conversation_state(self, phone):
        self.cleared = True

    async def save_conversation_state(self, phone, step, context):
        self.save_calls.append((phone, step, context))
        self._state = {"current_step": step, "context": context}


def _context(**overrides):
    ctx = {"lang": "en", "hms_appointment_id": "appt-1"}
    ctx.update(overrides)
    return ctx


# --- scheduler.py: state saved after send -------------------------------------------------

def test_scheduler_saves_awaiting_followup_state_after_a_successful_send():
    print("\n--- scheduler.py must save conversation_state so the next reply isn't blank ---")

    class _DB:
        async def list_due_followups(self, visit_date):
            return [{
                "id": "row-1", "phone_number": "919876543210", "hms_appointment_id": "appt-1",
                "preferred_language": "hi", "patient_display_name": "Riya",
            }]

        async def save_conversation_state(self, phone, step, context):
            saved["phone"], saved["step"], saved["context"] = phone, step, context

        async def mark_followup_sent(self, row_id):
            marked.append(row_id)

    saved = {}
    marked = []
    sent = []

    async def _send_text(client, to, body):
        sent.append((to, body))

    async def _run():
        with patch.object(scheduler, "db", _DB()), patch.object(scheduler, "send_text", _send_text):
            async with httpx.AsyncClient() as client:
                from datetime import date
                await scheduler.send_followups_for(client, date(2026, 9, 18))

    run(_run())
    check(len(sent) == 1, "the follow-up text is still sent as before")
    check(saved.get("step") == "awaiting_followup_reply", f"state is saved with the new step, got {saved!r}")
    check(saved.get("context", {}).get("lang") == "hi", "the patient's preferred_language carries into the new state")
    check(marked == ["row-1"], "still marks the follow-up as sent")


def test_followup_text_uses_the_real_doctor_name_when_known():
    print("\n--- Live-reported: the follow-up text said the literal 'your doctor' instead of the "
          "real name -- doctor_name is now stored on the appointment row and used here ---")

    class _DB:
        async def list_due_followups(self, visit_date):
            return [{
                "id": "row-1", "phone_number": "919876543210", "hms_appointment_id": "appt-1",
                "preferred_language": "en", "patient_display_name": "Riya", "doctor_name": "Dr. Sharma",
            }]

        async def save_conversation_state(self, phone, step, context):
            pass

        async def mark_followup_sent(self, row_id):
            pass

    sent = []

    async def _send_text(client, to, body):
        sent.append(body)

    async def _run():
        with patch.object(scheduler, "db", _DB()), patch.object(scheduler, "send_text", _send_text):
            async with httpx.AsyncClient() as client:
                from datetime import date
                await scheduler.send_followups_for(client, date(2026, 9, 18))

    run(_run())
    check(
        sent and "Dr. Sharma" in sent[0] and "your doctor" not in sent[0],
        f"uses the real doctor name, not the old hardcoded placeholder, got {sent!r}",
    )


def test_followup_text_falls_back_to_a_localized_generic_phrase_for_old_rows():
    print("\n--- A row booked before doctor_name existed has NULL there -- must still get a sensible, "
          "LOCALIZED fallback, not crash or show 'None' ---")

    class _DB:
        async def list_due_followups(self, visit_date):
            return [{
                "id": "row-1", "phone_number": "919876543210", "hms_appointment_id": "appt-1",
                "preferred_language": "hi", "patient_display_name": "Riya", "doctor_name": None,
            }]

        async def save_conversation_state(self, phone, step, context):
            pass

        async def mark_followup_sent(self, row_id):
            pass

    sent = []

    async def _send_text(client, to, body):
        sent.append(body)

    async def _run():
        with patch.object(scheduler, "db", _DB()), patch.object(scheduler, "send_text", _send_text):
            async with httpx.AsyncClient() as client:
                from datetime import date
                await scheduler.send_followups_for(client, date(2026, 9, 18))

    run(_run())
    check(
        sent and "आपके डॉक्टर" in sent[0] and "None" not in sent[0],
        f"falls back to the Hindi generic phrase (matching the patient's own language), got {sent!r}",
    )


def test_scheduler_still_marks_sent_even_if_the_state_save_itself_fails():
    print("\n--- best-effort: a DB hiccup saving state must not resend the follow-up text itself ---")

    class _DB:
        async def list_due_followups(self, visit_date):
            return [{
                "id": "row-1", "phone_number": "919876543210", "hms_appointment_id": "appt-1",
                "preferred_language": "en", "patient_display_name": "Riya",
            }]

        async def save_conversation_state(self, phone, step, context):
            raise RuntimeError("DB hiccup")

        async def mark_followup_sent(self, row_id):
            marked.append(row_id)

    marked = []

    async def _send_text(client, to, body):
        pass

    async def _run():
        with patch.object(scheduler, "db", _DB()), patch.object(scheduler, "send_text", _send_text):
            async with httpx.AsyncClient() as client:
                from datetime import date
                await scheduler.send_followups_for(client, date(2026, 9, 18))

    run(_run())
    check(marked == ["row-1"], "still marks sent even though the state-save failed")


# --- followup.handle_awaiting_followup_reply -----------------------------------------------

def test_wants_booking_starts_the_booking_flow():
    print("\n--- 'book karna hai' must start a fresh booking, not fall into symptom matching ---")
    wa_mock = _RecordingWhatsApp()
    advance_calls = []

    async def _advance(client, phone, context, booking):
        advance_calls.append((phone, context.get("lang")))

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_advance_booking_flow", _advance):
            async with httpx.AsyncClient() as client:
                await followup.handle_awaiting_followup_reply(
                    client, "919876543210", "text", "haan book karna hai", _context()
                )

    run(_run())
    check(len(advance_calls) == 1, f"starts the booking flow exactly once, got {advance_calls!r}")
    check(advance_calls[0][1] == "en", "carries the known language into the fresh booking")


def test_emergency_reply_shows_the_real_safety_alert():
    print("\n--- a genuinely alarming reply must get the real emergency response ---")
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await followup.handle_awaiting_followup_reply(
                    client, "919876543210", "text", "severe chest pain ho raha hai", _context()
                )

    run(_run())
    check(
        wa_mock.texts and "EMERGENCY" in wa_mock.texts[0],
        f"shows the real emergency alert, not a generic follow-up reply, got {wa_mock.texts!r}",
    )


def test_feeling_fine_gets_a_warm_acknowledgment_not_specialty_matching():
    print("\n--- THE original bug's exact scenario: 'Thik mehsoos kr raha hu' must be acknowledged, not treated as a symptom ---")
    wa_mock = _RecordingWhatsApp()
    db_mock = _RecordingDb()

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock), patch.object(conversation, "db", db_mock):
            async with httpx.AsyncClient() as client:
                await followup.handle_awaiting_followup_reply(
                    client, "919876543210", "text", "Thik mehsoos kr raha hu", _context()
                )

    run(_run())
    check(
        wa_mock.texts and "couldn't confidently match" not in wa_mock.texts[0],
        f"never shows the specialty-mismatch message, got {wa_mock.texts!r}",
    )
    check(db_mock.cleared, "conversation state is cleared -- nothing more to do")


def test_feeling_unwell_both_word_orders_offer_a_followup_booking():
    print("\n--- 'accha nahi' and 'nahi accha' (reversed order) must BOTH be caught ---")
    for phrase in ["accha nahi mehsoos kar raha hu", "nahi accha mehsoos kar raha hu"]:
        wa_mock = _RecordingWhatsApp()
        transition_calls = []

        async def _fake_transition(phone, next_step, context, current_step):
            transition_calls.append(next_step)

        async def _run():
            with patch.object(conversation, "whatsapp_client", wa_mock), \
                 patch.object(conversation, "_transition_to", _fake_transition):
                async with httpx.AsyncClient() as client:
                    await followup.handle_awaiting_followup_reply(client, "919876543210", "text", phrase, _context())

        run(_run())
        check(
            transition_calls == ["confirming_followup_booking"],
            f"{phrase!r} -> transitions to confirming_followup_booking, got {transition_calls!r}",
        )
        check(len(wa_mock.buttons) == 1, f"{phrase!r} -> offers the Book/No-thanks buttons, got {wa_mock.buttons!r}")


def test_unclear_text_falls_back_to_casual_chat_and_stays_on_this_step():
    print("\n--- Regression test for the actual original bug: unclear text must NEVER reach symptom/specialty matching ---")
    wa_mock = _RecordingWhatsApp()
    db_mock = _RecordingDb()
    llm_calls = []

    async def _fake_llm(client, step, context, user_message):
        llm_calls.append((step, user_message))
        return "That's good to know, thanks for letting us know!"

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "db", db_mock), \
             patch.object(conversation.nlu_client, "generate_conversational_response", _fake_llm):
            async with httpx.AsyncClient() as client:
                await followup.handle_awaiting_followup_reply(
                    client, "919876543210", "text", "kal se thoda weird lag raha", _context()
                )

    run(_run())
    check(len(llm_calls) == 1, f"falls back to the casual-chat LLM exactly once, got {llm_calls!r}")
    check(not db_mock.cleared, "state is NOT cleared -- stays on awaiting_followup_reply for the next message")
    check(len(db_mock.save_calls) == 0, "does NOT transition away from this step either")
    check(
        wa_mock.texts and "couldn't confidently match" not in wa_mock.texts[0] and "specialty" not in wa_mock.texts[0].lower(),
        f"never routes into specialty language, got {wa_mock.texts!r}",
    )


def test_non_text_input_gets_a_hint_not_silence():
    print("\n--- A stray button tap or empty input on this step must not be silently dropped ---")
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock):
            async with httpx.AsyncClient() as client:
                await followup.handle_awaiting_followup_reply(client, "919876543210", "button_reply", "some_old_button", _context())

    run(_run())
    check(len(wa_mock.texts) == 1, f"sends exactly one hint, got {wa_mock.texts!r}")


# --- followup.handle_confirming_followup_booking --------------------------------------------

def test_book_button_starts_booking_flow():
    print("\n--- Tapping 'Book follow-up' on the offer must start a fresh booking ---")
    wa_mock = _RecordingWhatsApp()
    advance_calls = []

    async def _advance(client, phone, context, booking):
        advance_calls.append(phone)

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation, "_advance_booking_flow", _advance):
            async with httpx.AsyncClient() as client:
                await followup.handle_confirming_followup_booking(
                    client, "919876543210", "button_reply", "followup_book", _context()
                )

    run(_run())
    check(len(advance_calls) == 1, "booking flow started")


def test_no_thanks_button_acknowledges_and_clears_state():
    print("\n--- Tapping 'No, thanks' must just acknowledge and stop, not start booking ---")
    wa_mock = _RecordingWhatsApp()
    db_mock = _RecordingDb()

    async def _run():
        with patch.object(conversation, "whatsapp_client", wa_mock), patch.object(conversation, "db", db_mock):
            async with httpx.AsyncClient() as client:
                await followup.handle_confirming_followup_booking(
                    client, "919876543210", "button_reply", "followup_no_thanks", _context()
                )

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one acknowledgment")
    check(db_mock.cleared, "state is cleared")


# --- end-to-end: the early intercept in handle_message itself -----------------------------

def test_handle_message_intercepts_before_nlu_even_when_nlu_would_misclassify():
    print("\n--- THE actual fix: even if NLU would classify the reply as describe_symptom/book_appointment, "
          "the followup step must win BEFORE that NLU result is ever acted on -- proves this isn't just "
          "registered too late via STEP_REGISTRY (which _maybe_handle_global_nlu_intent would reach first) ---")

    class _DB:
        async def get_conversation_state(self, phone):
            return {"current_step": "awaiting_followup_reply", "context": {"lang": "en", "hms_appointment_id": "appt-1"}}

        async def clear_conversation_state(self, phone):
            self.cleared = True

    db_mock = _DB()
    db_mock.cleared = False
    wa_mock = _RecordingWhatsApp()
    classify_calls = []

    async def _classify(client, text):
        # Simulates NLU misclassifying "feeling fine" as a symptom-description request --
        # exactly the original bug's mechanism. If this ever gets called/acted on before
        # followup.py's check, the fix has regressed back to the late-dispatch version.
        classify_calls.append(text)
        return {"intent": "describe_symptom", "entities": {"symptom": "feeling fine"}, "confidence": "high"}

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.nlu_client, "classify_message", _classify):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Riya", "text", "Thik mehsoos kr raha hu")

    run(_run())
    check(len(classify_calls) == 0, f"NLU classification is never even invoked for this step, got {classify_calls!r}")
    check(
        wa_mock.texts and "couldn't confidently match" not in wa_mock.texts[0],
        f"never reaches the specialty-mismatch message, got {wa_mock.texts!r}",
    )
    check(db_mock.cleared, "the followup handler ran and cleared state, confirming it -- not some other path -- handled this")


if __name__ == "__main__":
    test_scheduler_saves_awaiting_followup_state_after_a_successful_send()
    test_followup_text_uses_the_real_doctor_name_when_known()
    test_followup_text_falls_back_to_a_localized_generic_phrase_for_old_rows()
    test_scheduler_still_marks_sent_even_if_the_state_save_itself_fails()
    test_wants_booking_starts_the_booking_flow()
    test_emergency_reply_shows_the_real_safety_alert()
    test_feeling_fine_gets_a_warm_acknowledgment_not_specialty_matching()
    test_feeling_unwell_both_word_orders_offer_a_followup_booking()
    test_unclear_text_falls_back_to_casual_chat_and_stays_on_this_step()
    test_non_text_input_gets_a_hint_not_silence()
    test_book_button_starts_booking_flow()
    test_no_thanks_button_acknowledges_and_clears_state()
    test_handle_message_intercepts_before_nlu_even_when_nlu_would_misclassify()

    print("\n" + "=" * 50)
    if failures:
        print(f"FOLLOW-UP REPLY TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL FOLLOW-UP REPLY TESTS PASSED")
