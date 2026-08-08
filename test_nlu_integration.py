import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, patch

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
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

# Set test environment
os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

# Import system under test
from app import nlu_client, intent_router, db
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

db_mock = MockDB()

def test_classify_message_wrapper():
    print("\n--- Running classify_message Wrapper Tests ---")
    # Verify classify_message successfully returns validated dictionary
    res = asyncio.run(nlu_client.classify_message("hello"))
    check(res["intent"] == "greeting", "classify hello should return greeting")
    check(res["_validated"] is True, "classify output must have _validated = True")

def test_multi_turn_slot_filling():
    print("\n--- Running Multi-Turn Slot-Filling Tests ---")
    # Turn 1: "appointment chahiye" -> missing doctor/specialty AND datetime
    res1 = {"intent": "book_appointment", "confidence": "high", "entities": {}}
    routed1 = asyncio.run(intent_router.route_intent("user_1", res1, "appointment chahiye"))
    check(routed1.action == "ask_followup", "Turn 1 action must be ask_followup")
    check("specialty" in routed1.followup_prompt or "doctor" in routed1.followup_prompt, 
          "Turn 1 prompt should ask for doctor or specialty")

    # Turn 2: "gyno" -> still missing datetime
    res2 = {"intent": "check_availability", "confidence": "low", "entities": {"specialty": "gyno"}}
    routed2 = asyncio.run(intent_router.route_intent("user_1", res2, "gyno"))
    check(routed2.action == "ask_followup", "Turn 2 action must be ask_followup")
    check("datetime" in routed2.followup_prompt or "kab" in routed2.followup_prompt, 
          "Turn 2 prompt should ask for datetime")

    # Turn 3: "kal" -> all required slots filled
    res3 = {"intent": "book_appointment", "confidence": "low", "entities": {"datetime": "kal"}}
    routed3 = asyncio.run(intent_router.route_intent("user_1", res3, "kal"))
    check(routed3.action == "proceed_to_business_logic", "Turn 3 action must proceed to business logic")
    check(routed3.entities.get("specialty") == "gyno", "Entities must preserve specialty gyno from previous turn")
    check(routed3.entities.get("datetime") == "kal" or routed3.entities.get("datetime") is not None, 
          "Entities must preserve datetime")

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

def test_gemini_fallback_simulation():
    print("\n--- Running Gemini Fallback Simulation Tests ---")
    original_api_key = settings.sarvam_api_key
    settings.sarvam_api_key = "invalid_key_to_force_failure"
    
    try:
        # If Sarvam fails, it should seamlessly fallback to Gemini (if set up) or hard fallback
        # Let's verify it returns successfully and falls back
        res = asyncio.run(nlu_client.classify_message("hello"))
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
    test_gemini_fallback_simulation()

    intent_router.db = original_db

    print("\n" + "=" * 50)
    if failures:
        print(f"❌ NLU INTEGRATION TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("✅ ALL NLU INTEGRATION TESTS PASSED SUCCESSFULLY!")
