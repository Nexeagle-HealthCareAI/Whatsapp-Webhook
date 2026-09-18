"""
Regression tests for peer-review Batch 3's C2 (silent death): previously, any exception from
conversation.handle_message other than the two it already catches internally
(HmsApiError/httpx.HTTPError) left the patient with total silence -- worker.py's own try only
ever wrapped json.loads + task creation, never what happened once handle_job's task actually
started running. Covers the new behaviour: one immediate retry inside the same job, and only
after BOTH attempts fail does the patient get notified and the message get marked processed
(so a genuinely-recovering transient error isn't given up on after just one blip, but a
repeatedly-failing message doesn't retry forever either).

Same style as the other test_*.py files -- stubs aioodbc/redis before importing app modules,
no pytest. Run directly: python3 tests/test_worker.py
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

import worker  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


class _RecordingDb:
    def __init__(self):
        self.processed_ids: list[str] = []
        self._already_processed: set[str] = set()
        self._states: dict = {}

    async def is_message_processed(self, message_id):
        return message_id in self._already_processed

    async def mark_message_processed(self, message_id):
        self.processed_ids.append(message_id)

    async def get_conversation_state(self, phone):
        return self._states.get(phone)


class _RecordingWhatsApp:
    def __init__(self):
        self.texts: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append((to, body))


def _job(message_id="msg-1", sender="919876543210"):
    return {"message_id": message_id, "sender": sender, "sender_name": "Riya", "input_type": "text", "input_value": "hi"}


def test_success_on_first_attempt_marks_processed_no_retry_no_notice():
    print("\n--- Normal path: unaffected by the new retry-on-crash logic ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    handle_message_mock = AsyncMock(return_value=None)

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", handle_message_mock):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _job())

    run(_run())
    check(handle_message_mock.await_count == 1, f"handle_message called exactly once, got {handle_message_mock.await_count}")
    check(db_mock.processed_ids == ["msg-1"], "message marked processed")
    check(wa_mock.texts == [], "no failure notice sent")


def test_crash_then_success_on_retry_recovers_silently():
    print("\n--- A transient blip (e.g. a DB reconnect) on attempt 1 must be recovered by the retry, invisibly to the patient ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    calls = {"count": 0}

    async def _handle_message(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("transient blip")

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", _handle_message):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _job())

    run(_run())
    check(calls["count"] == 2, f"handle_message is retried exactly once after the first failure, got {calls['count']}")
    check(db_mock.processed_ids == ["msg-1"], "message marked processed once the retry succeeds")
    check(wa_mock.texts == [], "no failure notice sent -- the retry recovered it, the patient never needs to know")


def test_crash_on_both_attempts_notifies_patient_and_gives_up():
    print("\n--- Previously: total silence. Now: one retry, then the patient is told and the message is marked processed (given up on) ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    calls = {"count": 0}

    async def _handle_message(*args, **kwargs):
        calls["count"] += 1
        raise ValueError("keeps failing")

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", _handle_message):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _job(sender="919876500000"))

    run(_run())
    check(calls["count"] == 2, f"exactly one retry, not an infinite loop, got {calls['count']} attempts")
    check(
        db_mock.processed_ids == ["msg-1"],
        "message marked processed even though both attempts failed -- so a later webhook replay doesn't retry it forever",
    )
    check(len(wa_mock.texts) == 1, f"the patient gets exactly one apology text, not silence, got {wa_mock.texts!r}")
    check(wa_mock.texts[0][0] == "919876500000", "the notice goes to the actual sender")
    check("try again" in wa_mock.texts[0][1].lower() or "gadbad" in wa_mock.texts[0][1].lower(),
          f"uses the existing generic error_hms copy, got {wa_mock.texts[0][1]!r}")


def test_notify_failure_itself_failing_does_not_crash_the_job():
    print("\n--- Best-effort: even if the failure NOTICE itself can't be sent, handle_job must not raise ---")
    db_mock = _RecordingDb()

    class _BrokenWhatsApp:
        async def send_text(self, client, to, body):
            raise httpx.ConnectError("also down")

    async def _handle_message(*args, **kwargs):
        raise ValueError("keeps failing")

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", _BrokenWhatsApp()), \
             patch.object(worker.conversation, "handle_message", AsyncMock(side_effect=ValueError("keeps failing"))):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _job())  # must not raise

    run(_run())
    check(db_mock.processed_ids == ["msg-1"], "still marks processed even when the notice itself also failed")


if __name__ == "__main__":
    test_success_on_first_attempt_marks_processed_no_retry_no_notice()
    test_crash_then_success_on_retry_recovers_silently()
    test_crash_on_both_attempts_notifies_patient_and_gives_up()
    test_notify_failure_itself_failing_does_not_crash_the_job()

    print("\n" + "=" * 50)
    if failures:
        print(f"WORKER TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL WORKER TESTS PASSED")
