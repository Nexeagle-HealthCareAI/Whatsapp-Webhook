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
import json
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
        self.lists = {}

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

    async def lrem(self, key, count, value):
        lst = self.lists.get(key, [])
        if value in lst:
            lst.remove(value)
            return 1
        return 0

    async def rpoplpush(self, src, dest):
        lst = self.lists.get(src, [])
        if not lst:
            return None
        value = lst.pop()
        self.lists.setdefault(dest, []).insert(0, value)
        return value


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


def _seed_processing(job: dict) -> str:
    """Simulates what main()'s own BLMOVE already did before calling handle_job in real
    usage -- puts the raw job string into PROCESSING_KEY so tests can assert handle_job's
    own finally block correctly removes it again once it's done, on every exit path."""
    raw_job = json.dumps(job)
    _mock_redis_instance.lists.setdefault(worker.PROCESSING_KEY, []).append(raw_job)
    return raw_job


def test_success_on_first_attempt_marks_processed_no_retry_no_notice():
    print("\n--- Normal path: unaffected by the new retry-on-crash logic ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    handle_message_mock = AsyncMock(return_value=None)
    job = _job()
    raw_job = _seed_processing(job)

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", handle_message_mock):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _mock_redis_instance, raw_job, job)

    run(_run())
    check(handle_message_mock.await_count == 1, f"handle_message called exactly once, got {handle_message_mock.await_count}")
    check(db_mock.processed_ids == ["msg-1"], "message marked processed")
    check(wa_mock.texts == [], "no failure notice sent")
    check(
        raw_job not in _mock_redis_instance.lists.get(worker.PROCESSING_KEY, []),
        "Peer-review P0: the job is removed from PROCESSING_KEY once handled, not left stranded",
    )


def test_crash_then_success_on_retry_recovers_silently():
    print("\n--- A transient blip (e.g. a DB reconnect) on attempt 1 must be recovered by the retry, invisibly to the patient ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    calls = {"count": 0}
    job = _job()
    raw_job = _seed_processing(job)

    async def _handle_message(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ValueError("transient blip")

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", _handle_message):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _mock_redis_instance, raw_job, job)

    run(_run())
    check(calls["count"] == 2, f"handle_message is retried exactly once after the first failure, got {calls['count']}")
    check(db_mock.processed_ids == ["msg-1"], "message marked processed once the retry succeeds")
    check(wa_mock.texts == [], "no failure notice sent -- the retry recovered it, the patient never needs to know")
    check(raw_job not in _mock_redis_instance.lists.get(worker.PROCESSING_KEY, []), "still removed from PROCESSING_KEY")


def test_crash_on_both_attempts_notifies_patient_and_gives_up():
    print("\n--- Previously: total silence. Now: one retry, then the patient is told and the message is marked processed (given up on) ---")
    db_mock = _RecordingDb()
    wa_mock = _RecordingWhatsApp()
    calls = {"count": 0}
    job = _job(sender="919876500000")
    raw_job = _seed_processing(job)

    async def _handle_message(*args, **kwargs):
        calls["count"] += 1
        raise ValueError("keeps failing")

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker, "whatsapp_client", wa_mock), \
             patch.object(worker.conversation, "handle_message", _handle_message):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _mock_redis_instance, raw_job, job)

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
    check(
        raw_job not in _mock_redis_instance.lists.get(worker.PROCESSING_KEY, []),
        "removed from PROCESSING_KEY even on the give-up path -- must not sit there forever",
    )


def test_notify_failure_itself_failing_does_not_crash_the_job():
    print("\n--- Best-effort: even if the failure NOTICE itself can't be sent, handle_job must not raise ---")
    db_mock = _RecordingDb()
    job = _job()
    raw_job = _seed_processing(job)

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
                await worker.handle_job(client, _mock_redis_instance, raw_job, job)  # must not raise

    run(_run())
    check(db_mock.processed_ids == ["msg-1"], "still marks processed even when the notice itself also failed")
    check(
        raw_job not in _mock_redis_instance.lists.get(worker.PROCESSING_KEY, []),
        "removed from PROCESSING_KEY even when the failure notice itself also failed",
    )


def test_already_processed_message_is_still_removed_from_processing_key():
    print("\n--- The early-return 'already processed' path (a Meta webhook replay) must also "
          "clean up PROCESSING_KEY -- easy to miss since it returns before the retry logic runs ---")
    db_mock = _RecordingDb()
    db_mock._already_processed.add("msg-1")
    job = _job()
    raw_job = _seed_processing(job)
    handle_message_mock = AsyncMock()

    async def _run():
        with patch.object(worker, "db", db_mock), \
             patch.object(worker.conversation, "handle_message", handle_message_mock):
            async with httpx.AsyncClient() as client:
                await worker.handle_job(client, _mock_redis_instance, raw_job, job)

    run(_run())
    check(handle_message_mock.await_count == 0, "handle_message is never called for an already-processed message")
    check(raw_job not in _mock_redis_instance.lists.get(worker.PROCESSING_KEY, []), "still cleaned up from PROCESSING_KEY")


def test_sweep_stuck_jobs_recovers_a_crash_stranded_job():
    print("\n--- app.messengers.redis_client.sweep_stuck_jobs: the actual P0 fix -- a job left "
          "in a processing list by a previous crash must be requeued at the next startup ---")
    from app.messengers.redis_client import sweep_stuck_jobs

    _mock_redis_instance.lists.clear()
    stuck_job = json.dumps(_job(message_id="msg-stranded"))
    _mock_redis_instance.lists["some:processing"] = [stuck_job]

    async def _run():
        return await sweep_stuck_jobs(_mock_redis_instance, "some:processing", "some:main")

    recovered = run(_run())
    check(recovered == 1, f"reports exactly one job recovered, got {recovered}")
    check(_mock_redis_instance.lists.get("some:processing") == [], "the processing list is now empty")
    check(stuck_job in _mock_redis_instance.lists.get("some:main", []), "the job landed back on the main queue")


def test_sweep_stuck_jobs_is_a_no_op_on_a_clean_startup():
    print("\n--- The normal case: no crash happened, nothing to recover ---")
    from app.messengers.redis_client import sweep_stuck_jobs

    _mock_redis_instance.lists.clear()

    async def _run():
        return await sweep_stuck_jobs(_mock_redis_instance, "some:processing", "some:main")

    recovered = run(_run())
    check(recovered == 0, f"reports zero recovered on a clean startup, got {recovered}")


def test_conversation_logger_actually_imports_asyncio():
    print("\n--- Found while implementing this fix: conversation_logger.py called asyncio.run(main()) "
          "at the bottom but never imported asyncio -- would have crashed with NameError on every "
          "single start, meaning this process was very likely crash-looping in production ---")
    import ast
    import pathlib

    source = pathlib.Path(__file__).resolve().parent.parent / "conversation_logger.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imported = {n.name for node in ast.walk(tree) if isinstance(node, ast.Import) for n in node.names}
    check("asyncio" in imported, f"conversation_logger.py must import asyncio, got imports: {imported!r}")


if __name__ == "__main__":
    test_success_on_first_attempt_marks_processed_no_retry_no_notice()
    test_crash_then_success_on_retry_recovers_silently()
    test_crash_on_both_attempts_notifies_patient_and_gives_up()
    test_notify_failure_itself_failing_does_not_crash_the_job()
    test_already_processed_message_is_still_removed_from_processing_key()
    test_sweep_stuck_jobs_recovers_a_crash_stranded_job()
    test_sweep_stuck_jobs_is_a_no_op_on_a_clean_startup()
    test_conversation_logger_actually_imports_asyncio()

    print("\n" + "=" * 50)
    if failures:
        print(f"WORKER TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL WORKER TESTS PASSED")
