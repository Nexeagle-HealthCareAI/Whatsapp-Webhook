"""
Regression tests for peer-review Batch 3's H4: an HMS fetch that genuinely FAILED (raised)
was previously indistinguishable from one that succeeded with zero results -- both were
reported to the patient as "nothing exists" and wiped their in-progress booking
(clear_conversation_state). Live-reported: 1HMS's own /public/specialties returned a
near-empty list during a real backend data-population gap this session -- a fetch FAILURE
(exception) is a different, and more common, way to end up with an empty list, and unlike a
confirmed empty catalogue it must never be reported as "nothing exists" nor clear the
patient's progress. Mirrors the fetch_failed pattern already proven in
_send_patient_details_flow (test_patient_details_flow.py).

Covers both sites: specialty_browsing._send_specialty_list and __init__._send_doctor_list.
Same style as the other test_*.py files -- stubs aioodbc/redis before importing app modules,
no pytest. Run directly: python3 tests/test_empty_vs_failed_fetch.py
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

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        pass


class _RecordingDb:
    def __init__(self):
        self.cleared = False

    async def clear_conversation_state(self, phone):
        self.cleared = True


# --- specialty_browsing._send_specialty_list ---------------------------------------------

def test_specialties_fetch_raising_does_not_say_nothing_exists_or_wipe_state():
    print("\n--- specialty fetch genuinely FAILS (raises) -- must retry-message, not 'no specialties' ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_specialties", AsyncMock(side_effect=httpx.ConnectError("down"))):
            async with httpx.AsyncClient() as client:
                await conversation._send_specialty_list(client, "919876543210", {"lang": "en"})

    run(_run())
    check(not db_mock.cleared, "conversation state must NOT be wiped -- the fetch failed, we don't actually know it's empty")
    check(
        wa_mock.texts and "no_specialties" not in wa_mock.texts[0] and "try again" in wa_mock.texts[0].lower(),
        f"sends the generic retry message, not the false 'no specialties' claim, got {wa_mock.texts!r}",
    )


def test_specialties_hms_api_error_uses_error_hms_not_unreachable():
    print("\n--- HmsApiError (HMS responded but rejected) uses error_hms, distinct from a network failure ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_specialties", AsyncMock(side_effect=HmsApiError("rejected"))):
            async with httpx.AsyncClient() as client:
                await conversation._send_specialty_list(client, "919876543210", {"lang": "en"})

    run(_run())
    check(not db_mock.cleared, "state preserved")
    check(
        wa_mock.texts and wa_mock.texts[0] == conversation.t("error_hms", "en"),
        f"uses error_hms (HMS-rejected), not error_hms_unreachable, got {wa_mock.texts!r}",
    )


def test_specialties_genuinely_empty_still_says_nothing_exists_and_clears_state():
    print("\n--- A genuinely empty (not failed) specialty list must still behave exactly as before ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_specialties", AsyncMock(return_value=[])):
            async with httpx.AsyncClient() as client:
                await conversation._send_specialty_list(client, "919876543210", {"lang": "en"})

    run(_run())
    check(db_mock.cleared, "state IS cleared on a genuinely confirmed empty result -- unchanged behaviour")
    check(
        wa_mock.texts and wa_mock.texts[0] == conversation.t("no_specialties", "en"),
        f"still sends the real 'no specialties' message, got {wa_mock.texts!r}",
    )


# --- __init__._send_doctor_list -----------------------------------------------------------

def _no_location_context(**overrides):
    context = {"lang": "en", "specialty_category": "Cardiologist"}
    context.update(overrides)
    return context


def test_doctors_fetch_raising_does_not_say_nothing_exists_or_wipe_state():
    print("\n--- doctor fetch genuinely FAILS (raises) -- must retry-message, not 'no doctors' ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors", AsyncMock(side_effect=httpx.ConnectError("down"))):
            async with httpx.AsyncClient() as client:
                await conversation._send_doctor_list(client, "919876543210", _no_location_context())

    run(_run())
    check(not db_mock.cleared, "conversation state must NOT be wiped -- the fetch failed, we don't actually know it's empty")
    check(
        wa_mock.texts and "no_doctors" not in wa_mock.texts[0] and "try again" in wa_mock.texts[0].lower(),
        f"sends the generic retry message, not the false 'no doctors' claim, got {wa_mock.texts!r}",
    )


def test_doctors_genuinely_empty_still_says_nothing_exists_and_clears_state():
    print("\n--- A genuinely empty (not failed) doctor list must still behave exactly as before ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "list_doctors", AsyncMock(return_value=[])):
            async with httpx.AsyncClient() as client:
                await conversation._send_doctor_list(client, "919876543210", _no_location_context())

    run(_run())
    check(db_mock.cleared, "state IS cleared on a genuinely confirmed empty result -- unchanged behaviour")
    check(
        wa_mock.texts and wa_mock.texts[0] == conversation.t("no_doctors", "en"),
        f"still sends the real 'no doctors' message, got {wa_mock.texts!r}",
    )


if __name__ == "__main__":
    test_specialties_fetch_raising_does_not_say_nothing_exists_or_wipe_state()
    test_specialties_hms_api_error_uses_error_hms_not_unreachable()
    test_specialties_genuinely_empty_still_says_nothing_exists_and_clears_state()
    test_doctors_fetch_raising_does_not_say_nothing_exists_or_wipe_state()
    test_doctors_genuinely_empty_still_says_nothing_exists_and_clears_state()

    print("\n" + "=" * 50)
    if failures:
        print(f"EMPTY-VS-FAILED FETCH TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL EMPTY-VS-FAILED FETCH TESTS PASSED")
