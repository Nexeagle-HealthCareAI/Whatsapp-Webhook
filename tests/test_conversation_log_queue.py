"""
tests/test_conversation_log_queue.py
--------------------------------------
Covers app/messengers/conversation_log_queue.py (log_event, log_conversion) -- the
fire-and-forget producer side of the conversation-journey logging feature. The consumer
(conversation_logger.py) and the actual SQL (app/db/conversation_log.py) need a real SQL
Server to exercise meaningfully, so aren't covered here; what matters for the request path
is that these two functions never block on anything but a Redis LPUSH, and never raise even
when Redis itself fails. Run directly: python3 tests/test_conversation_log_queue.py
"""

import asyncio
import json
import os
import sys
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object
async def _create_pool(*a, **k):
    raise NotImplementedError
_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")
class _StubRedis:
    @classmethod
    def from_url(cls, *a, **k):
        return cls()
_fake_redis_mod.asyncio.Redis = _StubRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

from app.messengers import conversation_log_queue  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


class FakeRedis:
    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    async def lpush(self, key, *values):
        self.lists.setdefault(key, [])
        for v in values:
            self.lists[key].insert(0, v)
        return len(self.lists[key])


class ExplodingRedis:
    """Simulates Redis being briefly unavailable -- log_event/log_conversion must swallow
    this, never let it propagate into the caller's turn."""
    async def lpush(self, *a, **k):
        raise ConnectionError("redis is down")


def test_log_event_pushes_a_well_formed_job():
    redis = FakeRedis()
    asyncio.run(conversation_log_queue.log_event(
        redis, "sess-1", "919999999999", "in", "text", "Dr Aqeb se appointment", "awaiting_doctor_name"
    ))
    check(len(redis.lists.get(conversation_log_queue.LOG_KEY, [])) == 1, "log_event pushes exactly one job")
    job = json.loads(redis.lists[conversation_log_queue.LOG_KEY][0])
    check(job["kind"] == "event", "job is tagged as a plain event")
    check(job["session_id"] == "sess-1", "session_id carried through exactly")
    check(job["direction"] == "in", "direction carried through exactly")
    check(job["content"] == "Dr Aqeb se appointment", "full content carried through, not truncated or redacted")
    check(job["step"] == "awaiting_doctor_name", "step carried through exactly")
    check("at" in job, "a timestamp is recorded so order is recoverable regardless of processing order")


def test_log_event_skips_silently_without_a_session_id():
    redis = FakeRedis()
    asyncio.run(conversation_log_queue.log_event(redis, None, "919999999999", "in", "text", "hi", None))
    check(conversation_log_queue.LOG_KEY not in redis.lists, "no job is pushed when there's no session_id to attribute it to")


def test_log_event_never_raises_when_redis_fails():
    async def _run():
        await conversation_log_queue.log_event(
            ExplodingRedis(), "sess-1", "919999999999", "in", "text", "hi", "choosing_language"
        )
        return True  # only reached if the call above didn't raise
    completed = asyncio.run(_run())
    check(completed, "a Redis failure during log_event must never propagate into the caller's turn")


def test_log_conversion_pushes_a_well_formed_job():
    redis = FakeRedis()
    asyncio.run(conversation_log_queue.log_conversion(redis, "sess-1", "appt-123"))
    check(len(redis.lists.get(conversation_log_queue.LOG_KEY, [])) == 1, "log_conversion pushes exactly one job")
    job = json.loads(redis.lists[conversation_log_queue.LOG_KEY][0])
    check(job["kind"] == "conversion", "job is tagged as a conversion marker")
    check(job["session_id"] == "sess-1", "session_id carried through exactly")
    check(job["appointment_id"] == "appt-123", "appointment_id carried through exactly")


def test_log_conversion_never_raises_when_redis_fails():
    async def _run():
        await conversation_log_queue.log_conversion(ExplodingRedis(), "sess-1", "appt-123")
        return True
    completed = asyncio.run(_run())
    check(completed, "a Redis failure during log_conversion must never propagate into the caller's turn")


if __name__ == "__main__":
    test_log_event_pushes_a_well_formed_job()
    test_log_event_skips_silently_without_a_session_id()
    test_log_event_never_raises_when_redis_fails()
    test_log_conversion_pushes_a_well_formed_job()
    test_log_conversion_never_raises_when_redis_fails()

    print("\n" + "=" * 50)
    if failures:
        print(f"CONVERSATION LOG QUEUE TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL CONVERSATION LOG QUEUE TESTS PASSED")
