"""
Regression tests for peer-review Batch 4's "chup-chaap tooti cheezein" (quietly broken
things):

H1: context["current_step"] was never actually written -- every context.get("current_step")
    read downstream of handle_message's state load always got None. Two compounding bugs:
    _step_for_action's "stay on the current search step" branch was dead code (always fell
    through to "choosing_search_mode"), and _transition_to's history push never fired on the
    search/booking path (so "back" mid-search always restarted the whole conversation instead
    of stepping back one screen).
H2: confirming_wider_search had become unreachable dead code (superseded by auto-widening in
    _send_doctor_list, not merely a casualty of H1) -- removed, along with its now-orphaned
    i18n strings.
H3: a ~150-line try around NLU classification + everything downstream of it mislabeled any
    unrelated failure (a DB write, an HMS lookup, a WhatsApp send) as "NLU client parsing or
    routing failed". Narrowed to cover only the actual classification call; genuine downstream
    failures now propagate to worker.py's Batch-3 retry-then-notify safety net instead.

Same style as the other test_*.py files -- stubs aioodbc/redis before importing app modules,
no pytest. Run directly: python3 tests/test_batch4_broken_state_machine.py
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
from app.config import Settings  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


# --- H1: _step_for_action's "stay on this step" branch ------------------------------------

def test_step_for_action_stays_on_the_current_search_step_when_it_is_known():
    print("\n--- H1: with a real current_step in context, 'ask doctor' must stay put, not restart ---")
    for step in ["awaiting_symptom", "awaiting_doctor_name", "choosing_specialty_group",
                 "choosing_specialty", "choosing_sort", "choosing_doctor"]:
        result = conversation._step_for_action("ask", "doctor", {"current_step": step})
        check(result == step, f"stays on {step!r} instead of restarting, got {result!r}")


def test_step_for_action_still_falls_back_when_current_step_is_genuinely_unknown():
    print("\n--- A brand-new conversation (no current_step at all) must still get the safe default ---")
    result = conversation._step_for_action("ask", "doctor", {})
    check(result == "choosing_search_mode", f"falls back to choosing_search_mode, got {result!r}")


def test_step_for_action_does_not_resurrect_the_removed_confirming_wider_search():
    print("\n--- H2: confirming_wider_search must NOT be revived by the H1 fix -- it was deliberately removed ---")
    result = conversation._step_for_action("ask", "doctor", {"current_step": "confirming_wider_search"})
    check(
        result == "choosing_search_mode",
        f"confirming_wider_search is no longer in the stay-on-step set, got {result!r}",
    )


def test_confirming_wider_search_fully_removed_from_step_registry():
    print("\n--- H2: the whole dead step must be gone, not just unreachable ---")
    check(
        "confirming_wider_search" not in conversation.STEP_REGISTRY,
        "confirming_wider_search is no longer a registered step",
    )
    check(
        not hasattr(conversation, "_handle_confirming_wider_search"),
        "the dead handler function itself is deleted, not just unregistered",
    )


# --- H1: context["current_step"] is now actually populated from the real DB state ---------

def test_handle_message_populates_context_current_step_from_real_state():
    print("\n--- H1: handle_message must write the REAL current_step into context, not leave it unset ---")
    seen_context = {}

    class _DB:
        async def get_conversation_state(self, phone):
            return {"current_step": "choosing_search_mode", "context": {"lang": "en"}}

        async def save_conversation_state(self, phone, step, context):
            seen_context.update(context)

        async def clear_conversation_state(self, phone):
            pass

    async def _run():
        with patch.object(conversation, "db", _DB()), \
             patch.object(conversation.whatsapp_client, "send_typing_indicator", AsyncMock()), \
             patch.object(conversation.whatsapp_client, "send_text", AsyncMock()):
            async with httpx.AsyncClient() as client:
                # button_reply input skips the NLU block entirely (only runs for text input)
                # -- reaches the STEP_REGISTRY dispatch for choosing_search_mode directly and
                # deterministically, no network involved, which is where context (with
                # current_step now populated) actually gets saved back.
                await conversation.handle_message(client, "919876543210", "Riya", "button_reply", "name")

    run(_run())
    check(
        seen_context.get("current_step") == "choosing_search_mode",
        f"context persisted with the real current_step, got {seen_context!r}",
    )


def test_transition_pushes_history_on_the_search_path_now_that_current_step_is_real():
    print("\n--- H1: _advance_booking_flow's own _transition_to call must now actually push history ---")
    saved = {}

    class _DB:
        async def save_conversation_state(self, phone, step, context):
            saved["step"] = step
            saved["context"] = context

    sent = []

    async def _fake_send_location_request(client, phone, body):
        sent.append(body)

    booking = {
        "lang": {"value": "en", "raw": None, "candidates": [], "status": "filled", "source": "user"},
        "location": {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None},
        "doctor": {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None},
        "date": {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None},
        "shift": {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None},
        "patient": {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None},
    }
    # Simulates what handle_message now does at the top of every turn (the H1 fix) --
    # current_step carries the REAL previous step instead of being absent.
    context = {"lang": "en", "current_step": "awaiting_symptom", "pending_specialty": "Cardiologist (Heart)"}

    async def _run():
        with patch.object(conversation, "db", _DB()), \
             patch.object(conversation.whatsapp_client, "send_location_request", _fake_send_location_request):
            async with httpx.AsyncClient() as client:
                await conversation._advance_booking_flow(client, "919876543210", context, booking)

    run(_run())
    history = saved.get("context", {}).get("_history", [])
    check(len(history) == 1, f"a history entry IS pushed now, got {len(history)}")
    check(
        history and history[0].get("current_step") == "awaiting_symptom",
        f"the pushed entry records the real previous step, got {history!r}",
    )


# --- H3: the narrowed NLU try -------------------------------------------------------------

def test_nlu_classification_failure_still_degrades_gracefully():
    print("\n--- H3: classify_message itself failing must still be caught -- normal step handling continues ---")
    sent = []

    class _DB:
        async def get_conversation_state(self, phone):
            return {"current_step": "choosing_language", "context": {}}

        async def save_conversation_state(self, phone, step, context):
            pass

        async def clear_conversation_state(self, phone):
            pass

    async def _fake_send_buttons(client, phone, body, buttons):
        sent.append(("buttons", body))

    async def _run():
        with patch.object(conversation, "db", _DB()), \
             patch.object(conversation.whatsapp_client, "send_typing_indicator", AsyncMock()), \
             patch.object(conversation.whatsapp_client, "send_buttons", _fake_send_buttons), \
             patch.object(conversation.safety, "check_safety_triage", lambda text, lang: None), \
             patch.object(conversation.nlu_client, "classify_message", AsyncMock(side_effect=httpx.ConnectError("Sarvam down"))):
            async with httpx.AsyncClient() as client:
                # Must not raise -- a genuine NLU outage is exactly the case this narrower
                # try is still meant to swallow gracefully.
                await conversation.handle_message(client, "919876543210", "Riya", "text", "hello")

    run(_run())
    check(True, "handle_message did not raise when NLU classification itself failed")


def test_downstream_failure_after_nlu_now_propagates_instead_of_being_mislabeled():
    print("\n--- H3: a failure AFTER classify_message (e.g. intent routing) must propagate, "
          "not get silently mislabeled as an 'NLU parsing' failure and swallowed ---")

    class _DB:
        async def get_conversation_state(self, phone):
            # lang already set (has_lang_init=True) -- otherwise "book_appointment" takes the
            # EARLIER language-confirmation shortcut and never reaches route_intent at all.
            return {"current_step": "choosing_doctor", "context": {"lang": "en"}}

        async def save_conversation_state(self, phone, step, context):
            pass

    async def _run():
        with patch.object(conversation, "db", _DB()), \
             patch.object(conversation.whatsapp_client, "send_typing_indicator", AsyncMock()), \
             patch.object(conversation.safety, "check_safety_triage", lambda text, lang: None), \
             patch.object(
                 conversation.nlu_client, "classify_message",
                 AsyncMock(return_value={"intent": "book_appointment", "confidence": "high", "entities": {}}),
             ), \
             patch.object(conversation.intent_router, "route_intent", AsyncMock(side_effect=RuntimeError("router exploded"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Riya", "text", "book me an appointment")

    raised = False
    try:
        run(_run())
    except RuntimeError:
        raised = True
    check(raised, "the real, unrelated failure propagates out of handle_message instead of being swallowed here")


# --- H5: LOCATION_API_BASE_URL is now a real override point --------------------------------

def test_location_api_base_url_is_overridable_via_env():
    print("\n--- H5: settings.location_api_base_url must actually honour an env override now ---")
    base_env = {k: os.environ[k] for k in [
        "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
        "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
    ]}
    with patch.dict(os.environ, {**base_env, "LOCATION_API_BASE_URL": "https://loc.nexeagle.com"}):
        overridden = Settings()
    check(
        overridden.location_api_base_url == "https://loc.nexeagle.com",
        f"env override takes effect, got {overridden.location_api_base_url!r}",
    )
    with patch.dict(os.environ, base_env, clear=False):
        os.environ.pop("LOCATION_API_BASE_URL", None)
        default = Settings()
    check(
        default.location_api_base_url == "https://loc-dev.nexeagle.com",
        f"still defaults to the dev subdomain when unset, got {default.location_api_base_url!r}",
    )


if __name__ == "__main__":
    test_step_for_action_stays_on_the_current_search_step_when_it_is_known()
    test_step_for_action_still_falls_back_when_current_step_is_genuinely_unknown()
    test_step_for_action_does_not_resurrect_the_removed_confirming_wider_search()
    test_confirming_wider_search_fully_removed_from_step_registry()
    test_handle_message_populates_context_current_step_from_real_state()
    test_transition_pushes_history_on_the_search_path_now_that_current_step_is_real()
    test_nlu_classification_failure_still_degrades_gracefully()
    test_downstream_failure_after_nlu_now_propagates_instead_of_being_mislabeled()
    test_location_api_base_url_is_overridable_via_env()

    print("\n" + "=" * 50)
    if failures:
        print(f"BATCH 4 TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL BATCH 4 TESTS PASSED")
