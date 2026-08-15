import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import httpx

# Stub ODBC database before importing app modules
_fake = types.ModuleType("aioodbc")
_fake.Pool = object
async def _create_pool(*a, **k):
    raise NotImplementedError
_fake.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake)

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

# Setup path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Set test environment
os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

# Import system under test
from app import nlu_client, db
from app.referee import intent_router, flow_policy
from app.config import settings

failures = []

def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"❌ FAIL: {message}")
    else:
        print(f"✅ PASS: {message}")

class MockDB:
    def __init__(self):
        self.has_active_appt = False
        self.active_appt_date = "2026-08-10"
        self.active_appt_check_should_fail = False

    async def get_pool(self):
        from unittest.mock import MagicMock, AsyncMock
        
        mock_cur = AsyncMock()
        if self.has_active_appt:
            # We mock datetime.date or str
            from datetime import date
            mock_cur.fetchone.return_value = [date(2026, 8, 10)]
        else:
            mock_cur.fetchone.return_value = None
            
        mock_conn = AsyncMock()
        mock_conn.cursor = MagicMock()
        mock_conn.cursor.return_value.__aenter__.return_value = mock_cur
        
        mock_pool = MagicMock()
        mock_pool.acquire = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn
        return mock_pool

    async def get_upcoming_active_appointment(self, phone):
        # intent_router.py now delegates to this db.py function instead of running its
        # own inline SQL (SOLID rebuild Phase 3) -- the mock has to speak the same
        # interface, or intent_router's broad `except Exception` silently swallows the
        # AttributeError and defaults to "no active appointment", masking this test's
        # whole scenario instead of failing loudly.
        if self.active_appt_check_should_fail:
            raise RuntimeError("simulated DB outage")
        if self.has_active_appt:
            return True, self.active_appt_date
        return False, None

db_mock = MockDB()

def test_classify_message_wrapper():
    print("\n--- Running classify_message Wrapper Tests ---")
    # Verify classify_message successfully returns validated dictionary
    async def _run():
        async with httpx.AsyncClient() as client:
            return await nlu_client.classify_message(client, "hello")
    res = asyncio.run(_run())
    check(res["intent"] == "greeting", "classify hello should return greeting")
    check(res["_validated"] is True, "classify output must have _validated = True")

def test_multi_turn_slot_filling():
    print("\n--- Running Multi-Turn Slot-Filling Tests ---")
    # Turn 1: "appointment chahiye" -> missing doctor/specialty
    res1 = {"intent": "book_appointment", "confidence": "high", "entities": {}}
    routed1 = asyncio.run(intent_router.route_intent("user_1", res1, "appointment chahiye"))
    check(routed1.action == "ask_followup", "Turn 1 action must be ask_followup")
    check("specialty" in routed1.followup_prompt or "doctor" in routed1.followup_prompt,
          "Turn 1 prompt should ask for doctor or specialty")

    # Turn 2: "gyno" -> book_appointment no longer requires datetime up front (the booking
    # flow always collects day/shift as its own dedicated step later), so specialty alone
    # satisfies every required slot and this proceeds straight to business logic.
    res2 = {"intent": "check_availability", "confidence": "low", "entities": {"specialty": "gyno"}}
    routed2 = asyncio.run(intent_router.route_intent("user_1", res2, "gyno"))
    check(routed2.action == "proceed_to_business_logic", "Turn 2 action must proceed to business logic")
    check(routed2.entities.get("specialty") == "gyno", "Entities must preserve specialty gyno from previous turn")

def test_booking_vs_reschedule_ambiguity():
    print("\n--- Running Reschedule vs Book Ambiguity Tests ---")
    db_mock.has_active_appt = True
    
    # 1. Booking when an appointment exists -> should ask clarification
    nlu_val = {"intent": "book_appointment", "confidence": "high", "entities": {"specialty": "gyno", "datetime": "2026-08-11"}}
    routed = asyncio.run(intent_router.route_intent("user_2", nlu_val, "book gynecology for tomorrow"))
    check(routed.action == "ask_followup", "Should ask clarification if active appointment exists")
    check("reschedule" in routed.followup_prompt, "Clarification prompt should offer rescheduling option")

    # 2. User chooses "reschedule" -> should switch to reschedule_appointment
    res_input = {"intent": "unknown", "confidence": "low", "entities": {}}
    routed_res = asyncio.run(intent_router.route_intent("user_2", res_input, "reschedule"))
    check(routed_res.intent == "reschedule_appointment", "Reschedule choice should change intent to reschedule")
    check(routed_res.entities.get("datetime") == "2026-08-11", "Should retain slots across clarification")

    # 3. User chooses "naya" -> should proceed with book_appointment
    # Reset state to mock another user clarification
    asyncio.run(_mock_redis_instance.delete("nlu:session:user_3"))
    routed_clarify = asyncio.run(intent_router.route_intent("user_3", nlu_val, "book gynecology for tomorrow"))
    
    routed_new = asyncio.run(intent_router.route_intent("user_3", res_input, "book new one"))
    check(routed_new.action == "proceed_to_business_logic", "Choosing new booking should proceed")
    check(routed_new.intent == "book_appointment", "Intent should remain book_appointment")

    db_mock.has_active_appt = False


def test_active_appointment_check_failure_aborts_instead_of_guessing():
    print("\n--- Running Active-Appointment DB-Failure Tests ---")
    # If the active-appointment lookup itself fails (DB outage, pool exhaustion under
    # load, ...), route_intent must NOT silently assume "no active appointment" and let
    # a book_appointment through -- that would risk a real duplicate booking, worst
    # exactly when the DB is already under heavy load. It must fail loud instead: return
    # action="error" and let the caller tell the patient to try again.
    db_mock.active_appt_check_should_fail = True
    try:
        nlu_val = {"intent": "book_appointment", "confidence": "high", "entities": {"specialty": "gyno", "datetime": "2026-08-11"}}
        routed = asyncio.run(intent_router.route_intent("user_db_failure", nlu_val, "book gynecology for tomorrow"))
        check(routed.action == "error", "a failed active-appointment check must return action='error', not proceed")
        check(routed.action != "proceed_to_business_logic", "a failed DB check must never silently let booking through")
    finally:
        db_mock.active_appt_check_should_fail = False

def test_confidence_gate_on_zero_slot_intents():
    print("\n--- Running Confidence Gate Tests (cancel/back/greeting) ---")
    # cancel_appointment has no REQUIRED_ENTITIES check (maps to []), so nothing else
    # verifies the classification before it wipes conversation state — the router's own
    # confidence score is the only safety net. A low-confidence guess must be reported as
    # low, not silently upgraded to a safe 0.9 the way a slot-filled book_appointment is.
    low_conf = {"intent": "cancel_appointment", "confidence": "low", "entities": {}}
    routed = asyncio.run(intent_router.route_intent("user_conf_1", low_conf, "nahi cancel jaisa kuch nahi"))
    check(routed.action == "proceed_to_business_logic", "cancel still routes to proceed (router doesn't block it)")
    check(routed.confidence < 0.7, "low-confidence cancel must NOT report a safe/high confidence")

    high_conf = {"intent": "cancel_appointment", "confidence": "high", "entities": {}}
    routed = asyncio.run(intent_router.route_intent("user_conf_2", high_conf, "cancel my appointment"))
    check(routed.confidence >= 0.7, "high-confidence cancel reports a confidence that clears the gate")

    # book_appointment DOES have a REQUIRED_ENTITIES check — reaching proceed_to_business_logic
    # already means every slot passed that check across however many turns it took, so this
    # must report a safe confidence even when the single message that filled the last slot
    # was itself low-confidence (this is exactly turn 3 of test_multi_turn_slot_filling).
    db_mock.has_active_appt = False  # belt-and-suspenders reset; test_booking_vs_reschedule_ambiguity also resets this itself now
    low_conf_but_slots_filled = {"intent": "book_appointment", "confidence": "low", "entities": {"specialty": "gyno", "datetime": "kal"}}
    routed = asyncio.run(intent_router.route_intent("user_conf_3", low_conf_but_slots_filled, "kal"))
    check(routed.action == "proceed_to_business_logic", "slot-filled book_appointment proceeds")
    check(routed.confidence >= 0.7, "slot-filling safety net overrides a low per-message confidence")

def test_nlu_confidence_gate_rejection():
    print("\n--- Running NLU Confidence Gate Rejection Tests ---")
    # Genuinely incomplete under book_appointment's current requirements (doctor_name or
    # specialty) -- specialty alone no longer qualifies as "incomplete" since datetime was
    # dropped from the requirement, see app/listener/nlu_config.py.
    low_conf_incomplete = {"intent": "book_appointment", "confidence": "low", "entities": {}}
    routed = asyncio.run(intent_router.route_intent("user_conf_4", low_conf_incomplete, "book appointment"))
    
    check(routed.intent == "out_of_scope", "low-confidence incomplete intent must be overridden to out_of_scope")
    check(routed.entities == {}, "low-confidence incomplete intent must have its entities cleared")

def test_live_time_of_day_extraction():
    print("\n--- Running Live time_of_day Extraction Tests (real Sarvam call) ---")
    # "kal subah" losing "subah" was the original bug: normalize_datetime_to_date collapsed
    # the whole string to a bare ISO date. Verifies the fix against the real model, not just
    # the local normalization logic — nlu_config.py's prompt has to actually get Sarvam to
    # emit time_of_day as a separate key for this to work end to end.
    async def _run(text):
        async with httpx.AsyncClient() as client:
            return await nlu_client.classify_message(client, text)

    res = asyncio.run(_run("kal subah Dr Sharma se appointment chahiye"))
    check(res["entities"].get("time_of_day") == "Morning", f"'kal subah' should extract time_of_day=Morning, got {res}")
    check(res["entities"].get("datetime"), "datetime should still be present alongside time_of_day")

    res = asyncio.run(_run("is Dr. Sen available tomorrow evening?"))
    check(res["entities"].get("time_of_day") == "Evening", f"'tomorrow evening' should extract time_of_day=Evening, got {res}")

def test_stale_session_discarded_on_step_mismatch():
    print("\n--- Running Stale Session Discard Tests (dual-memory desync fix) ---")
    # Reproduces the concrete bug: a follow-up question gets saved under one SQL step, then
    # the SQL step machine moves on WITHOUT going through route_intent again (e.g. a button
    # tap, which bypasses NLU entirely — see the hot-swap-style desync analysis). The stale
    # session must not resurface and hijack a later, unrelated turn just because its
    # 15-minute TTL hasn't expired yet.
    db_mock.has_active_appt = False
    wa_id = "user_stale_1"

    incomplete = {"intent": "book_appointment", "confidence": "high", "entities": {"datetime": "kal"}}
    routed1 = asyncio.run(intent_router.route_intent(wa_id, incomplete, "kal appointment chahiye", "en", "choosing_search_mode"))
    check(routed1.action == "ask_followup", "incomplete booking should ask a follow-up")

    # SQL step has since moved on (e.g. to "confirming") to a DIFFERENT step than the one
    # the session was saved under — an unrelated, low-confidence message arrives there.
    unrelated = {"intent": "out_of_scope", "confidence": "low", "entities": {}}
    routed2 = asyncio.run(intent_router.route_intent(wa_id, unrelated, "haan sahi hai", "en", "confirming"))
    check(routed2.intent != "book_appointment", f"stale session from a different step must not hijack this turn, got intent={routed2.intent!r}")
    check("datetime" not in routed2.entities, "stale datetime from the abandoned session must not leak into this turn")

def test_legitimate_multi_turn_still_merges_with_matching_step():
    print("\n--- Running Legitimate Same-Step Multi-Turn Test ---")
    # The fix must not break the real case: slot-filling across turns where the SQL step
    # genuinely hasn't moved (the patient is just answering the router's own question).
    db_mock.has_active_appt = False
    wa_id = "user_stale_2"
    step = "choosing_search_mode"

    incomplete = {"intent": "book_appointment", "confidence": "high", "entities": {"datetime": "kal"}}
    routed1 = asyncio.run(intent_router.route_intent(wa_id, incomplete, "kal appointment chahiye", "en", step))
    check(routed1.action == "ask_followup", "turn 1 should ask for the missing doctor/specialty")

    answer = {"intent": "check_availability", "confidence": "low", "entities": {"specialty": "gyno"}}
    routed2 = asyncio.run(intent_router.route_intent(wa_id, answer, "gyno", "en", step))
    check(routed2.action == "proceed_to_business_logic", "same-step follow-up answer should complete the booking")
    check(routed2.entities.get("datetime") == "kal", "datetime from turn 1 must be preserved when the step hasn't moved")
    check(routed2.entities.get("specialty") == "gyno", "specialty from turn 2 must be merged in")

def test_short_datetime_reply_recovered_when_nlu_returns_out_of_scope():
    print("\n--- Running Short Datetime-Reply Recovery Test (live-reported bug) ---")
    # Originally reported against book_appointment ("hi, i have to book appointment with Dr,
    # Radha" -> router asks for a date -> patient replies just "today" -> router asks the SAME
    # question again, forever). book_appointment no longer asks for datetime up front (the
    # booking flow collects day/shift as its own dedicated step later, so asking here was
    # redundant -- see app/listener/nlu_config.py), so that exact scenario can't recur for
    # book_appointment. reschedule_appointment still requires datetime up front, so it's used
    # here to keep covering the actual mechanism under test: recovering a short, context-free
    # "today"-style reply that Sarvam classifies as out_of_scope with empty entities, since
    # there's otherwise nothing for the merge logic to pick up and the follow-up would repeat
    # forever. normalize_datetime_to_date only matches a short, unambiguous set of literal
    # date phrasings, so running it unconditionally on the raw text is safe.
    db_mock.has_active_appt = False
    wa_id = "user_short_datetime_1"

    turn1 = {"intent": "reschedule_appointment", "confidence": "high", "entities": {}}
    routed1 = asyncio.run(intent_router.route_intent(wa_id, turn1, "I need to reschedule my appointment"))
    check(routed1.action == "ask_followup", "turn 1 should ask for the missing datetime")
    check("date" in routed1.followup_prompt.lower(), f"turn 1 prompt should ask for a date, got {routed1.followup_prompt!r}")

    # Exactly what a bare "today" gets classified as out of context, in practice.
    turn2 = {"intent": "out_of_scope", "confidence": "low", "entities": {}}
    routed2 = asyncio.run(intent_router.route_intent(wa_id, turn2, "today"))
    check(
        routed2.action == "proceed_to_business_logic",
        f"turn 2 ('today') must complete the reschedule instead of re-asking, got action={routed2.action!r} entities={routed2.entities!r}",
    )
    check(routed2.entities.get("datetime"), f"'today' should have been recovered as a datetime, got {routed2.entities!r}")


def test_global_intent_escapes_awaiting_clarification_loop():
    print("\n--- Running Global-Intent Override Test (live-reported stuck-loop bug) ---")
    # Live-reported: a patient with an active appointment tries to book a new one, gets
    # asked "book new or reschedule?" (awaiting_clarification state saved in Redis) -- then
    # sends a plain "hi", and the bot repeats the EXACT SAME clarification question. Sending
    # "cancel" instead has the identical problem. Neither "hi" nor "cancel" match either of
    # awaiting_clarification's own keyword lists (reschedule words / new-booking words), so
    # its "else: keep asking" branch just replays the question -- forever, regardless of what
    # the patient actually said, since conversation.py's own cancel/back/greeting handling
    # never even runs (it's gated behind route_intent returning proceed_to_business_logic,
    # which this branch never reaches).
    db_mock.has_active_appt = True

    for label, message_intent, message_text in [
        ("hi", "greeting", "hi"),
        ("cancel", "cancel_appointment", "cancel"),
        ("back", "navigate_back", "back"),
    ]:
        wa_id = f"user_global_override_{label}"
        step = "confirming"

        turn1 = {"intent": "book_appointment", "confidence": "high", "entities": {"doctor_name": "Radha", "datetime": "2026-08-13"}}
        routed1 = asyncio.run(intent_router.route_intent(wa_id, turn1, "book appointment with radha tomorrow", "en", step))
        check(routed1.action == "ask_followup", f"[{label}] turn 1 should ask to clarify new-vs-reschedule (active appointment exists)")

        # This is what conversation.py's handle_message now does before calling
        # route_intent() on the next turn -- see the call site right before "2. Route intent
        # using intent_router" in app/conversation.py.
        turn2 = {"intent": message_intent, "confidence": "high", "entities": {}}
        if flow_policy.is_global_override(turn2["intent"], turn2["confidence"]):
            asyncio.run(intent_router.clear_session(wa_id))
        routed2 = asyncio.run(intent_router.route_intent(wa_id, turn2, message_text, "en", step))

        check(
            routed2.action == "proceed_to_business_logic",
            f"[{label}] must escape the clarification loop, got action={routed2.action!r}",
        )
        check(routed2.intent == message_intent, f"[{label}] must be routed as its own real intent, got {routed2.intent!r}")

    db_mock.has_active_appt = False


def test_legitimate_followup_answer_not_treated_as_a_global_override():
    print("\n--- Running Non-Regression Test: ordinary follow-up answers still merge ---")
    # The fix must not clear a genuinely in-progress multi-turn session just because some
    # OTHER unrelated message happens to arrive -- only an actual global-intent message
    # (cancel/back/greeting) should clear it. An ordinary follow-up answer like "gyno" is
    # none of those, so flow_policy.is_global_override must say no, and the session must
    # still merge normally.
    db_mock.has_active_appt = False
    wa_id = "user_global_override_no_false_positive"
    step = "choosing_search_mode"

    turn1 = {"intent": "book_appointment", "confidence": "high", "entities": {"datetime": "kal"}}
    routed1 = asyncio.run(intent_router.route_intent(wa_id, turn1, "kal appointment chahiye", "en", step))
    check(routed1.action == "ask_followup", "turn 1 should ask for the missing doctor/specialty")

    turn2 = {"intent": "check_availability", "confidence": "low", "entities": {"specialty": "gyno"}}
    check(not flow_policy.is_global_override(turn2["intent"], turn2["confidence"]), "'gyno' is not a global intent, must not trigger a session clear")
    routed2 = asyncio.run(intent_router.route_intent(wa_id, turn2, "gyno", "en", step))
    check(routed2.action == "proceed_to_business_logic", "ordinary follow-up should still complete the booking")
    check(routed2.entities.get("datetime") == "kal", "datetime from turn 1 must still be preserved")
    check(routed2.entities.get("specialty") == "gyno", "specialty from turn 2 must still be merged in")


def test_gemini_fallback_simulation():
    print("\n--- Running Gemini Fallback Simulation Tests ---")
    original_api_key = settings.sarvam_api_key
    settings.sarvam_api_key = "invalid_key_to_force_failure"
    
    try:
        # If Sarvam fails, it should seamlessly fallback to Gemini (if set up) or hard fallback
        # Let's verify it returns successfully and falls back
        async def _run():
            async with httpx.AsyncClient() as client:
                return await nlu_client.classify_message(client, "hello")
        res = asyncio.run(_run())
        check(res["intent"] in ("greeting", "out_of_scope"), "Fallback classification should return valid response")
    finally:
        settings.sarvam_api_key = original_api_key

if __name__ == "__main__":
    # Swap db mock
    original_db = intent_router.db
    intent_router.db = db_mock

    # Run tests
    test_classify_message_wrapper()
    test_multi_turn_slot_filling()
    test_booking_vs_reschedule_ambiguity()
    test_active_appointment_check_failure_aborts_instead_of_guessing()
    test_confidence_gate_on_zero_slot_intents()
    test_nlu_confidence_gate_rejection()
    test_stale_session_discarded_on_step_mismatch()
    test_legitimate_multi_turn_still_merges_with_matching_step()
    test_short_datetime_reply_recovered_when_nlu_returns_out_of_scope()
    test_global_intent_escapes_awaiting_clarification_loop()
    test_legitimate_followup_answer_not_treated_as_a_global_override()
    test_live_time_of_day_extraction()
    test_gemini_fallback_simulation()

    intent_router.db = original_db

    print("\n" + "=" * 50)
    if failures:
        print(f"❌ NLU INTEGRATION TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("✅ ALL NLU INTEGRATION TESTS PASSED SUCCESSFULLY!")
