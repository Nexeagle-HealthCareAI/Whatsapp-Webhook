"""
tests/test_outbound_queue.py
------------------------------
Covers app/messengers/outbound_queue.py (enqueue, the rate limiter, backoff/dead-letter,
delayed-job promotion) and confirms whatsapp_client.py's send_text actually routes through
it instead of calling Meta directly. Run directly: python3 tests/test_outbound_queue.py
"""

import asyncio
import json
import os
import sys
import time
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub aioodbc/redis before importing app.* -- same convention as every other test file.
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

from app.messengers import outbound_queue, whatsapp_client  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


class FakeRedis:
    """A real-enough in-memory stand-in for the specific Redis commands
    outbound_queue.py/sender.py use -- lists, a sorted set, and a plain counter. Not a
    general-purpose Redis mock, just enough to exercise the actual logic under test."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.counters: dict[str, int] = {}

    async def lpush(self, key, *values):
        self.lists.setdefault(key, [])
        for v in values:
            self.lists[key].insert(0, v)
        return len(self.lists[key])

    async def blmove(self, src, dest, timeout, src_dir="LEFT", dest_dir="RIGHT"):
        lst = self.lists.get(src, [])
        if not lst:
            return None
        item = lst.pop() if src_dir.upper() == "RIGHT" else lst.pop(0)
        self.lists.setdefault(dest, [])
        if dest_dir.upper() == "LEFT":
            self.lists[dest].insert(0, item)
        else:
            self.lists[dest].append(item)
        return item

    async def lrem(self, key, count, value):
        lst = self.lists.get(key, [])
        if value in lst:
            lst.remove(value)
            return 1
        return 0

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, ttl):
        return True

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {})
        self.zsets[key].update(mapping)
        return len(mapping)

    async def zrangebyscore(self, key, min, max):
        zset = self.zsets.get(key, {})
        return [member for member, score in zset.items() if min <= score <= max]

    async def zrem(self, key, *values):
        zset = self.zsets.get(key, {})
        removed = 0
        for v in values:
            if v in zset:
                del zset[v]
                removed += 1
        return removed


def test_enqueue_pushes_a_well_formed_job():
    redis = FakeRedis()
    payload = {"messaging_product": "whatsapp", "to": "919999999999", "text": {"body": "hi"}}
    asyncio.run(outbound_queue.enqueue(redis, payload))

    check(len(redis.lists.get(outbound_queue.OUTBOX_KEY, [])) == 1, "enqueue pushes exactly one job")
    job = json.loads(redis.lists[outbound_queue.OUTBOX_KEY][0])
    check(job["payload"] == payload, "the queued job carries the exact payload given")
    check(job["attempt"] == 0, "a freshly enqueued job starts at attempt 0")
    check("enqueued_at" in job, "a freshly enqueued job records when it was queued")


def test_rate_limiter_allows_up_to_the_limit_then_blocks():
    redis = FakeRedis()
    frozen_time = 1_000_000.0
    original_time = outbound_queue.time.time
    outbound_queue.time.time = lambda: frozen_time  # freeze so every call lands in one bucket
    try:
        async def _run():
            for _ in range(3):
                await asyncio.wait_for(outbound_queue.acquire_send_slot(redis, limit_per_sec=3), timeout=0.5)
            # The 4th call, same frozen second, same limit of 3 -- should NOT complete quickly,
            # since the bucket never rolls over while time is frozen.
            try:
                await asyncio.wait_for(outbound_queue.acquire_send_slot(redis, limit_per_sec=3), timeout=0.3)
                return False  # it returned -- the limiter did NOT block as expected
            except asyncio.TimeoutError:
                return True  # still blocked after 0.3s -- correct

        still_blocked = asyncio.run(_run())
        check(still_blocked, "the 4th send in the same second blocks instead of proceeding past the limit")
    finally:
        outbound_queue.time.time = original_time


def test_requeue_with_backoff_schedules_a_retry():
    redis = FakeRedis()
    job = {"payload": {"to": "919999999999"}, "attempt": 0, "enqueued_at": time.time()}
    asyncio.run(outbound_queue.requeue_with_backoff(redis, job, max_attempts=5))

    check(len(redis.zsets.get(outbound_queue.DELAYED_KEY, {})) == 1, "a transient failure is scheduled in the delayed set")
    check(len(redis.lists.get(outbound_queue.DEAD_KEY, [])) == 0, "attempt 1 of 5 does NOT go to the dead-letter list yet")
    (raw_job, ready_at), = redis.zsets[outbound_queue.DELAYED_KEY].items()
    check(json.loads(raw_job)["attempt"] == 1, "the retried job's attempt counter incremented")
    check(ready_at > time.time(), "the retry is scheduled for the future, not immediately")


def test_requeue_with_backoff_dead_letters_after_max_attempts():
    redis = FakeRedis()
    job = {"payload": {"to": "919999999999"}, "attempt": 4, "enqueued_at": time.time()}  # one short of 5
    asyncio.run(outbound_queue.requeue_with_backoff(redis, job, max_attempts=5))

    check(len(redis.lists.get(outbound_queue.DEAD_KEY, [])) == 1, "the 5th failed attempt goes to the dead-letter list")
    check(len(redis.zsets.get(outbound_queue.DELAYED_KEY, {})) == 0, "a dead-lettered job is NOT also scheduled for retry")


def test_promote_ready_delayed_jobs_moves_only_expired_ones():
    redis = FakeRedis()
    now = time.time()
    still_waiting = json.dumps({"payload": {"to": "future"}, "attempt": 1})
    ready = json.dumps({"payload": {"to": "past"}, "attempt": 1})
    redis.zsets[outbound_queue.DELAYED_KEY] = {
        still_waiting: now + 100,  # not due yet
        ready: now - 5,  # already due
    }

    asyncio.run(outbound_queue.promote_ready_delayed_jobs(redis))

    check(redis.lists.get(outbound_queue.OUTBOX_KEY, []) == [ready], "only the job whose backoff has elapsed is promoted back to the main queue")
    check(still_waiting in redis.zsets[outbound_queue.DELAYED_KEY], "a job not yet due stays in the delayed set")
    check(ready not in redis.zsets[outbound_queue.DELAYED_KEY], "a promoted job is removed from the delayed set")


def test_send_text_enqueues_instead_of_calling_meta_directly():
    redis = FakeRedis()
    original_get_redis = whatsapp_client.get_redis
    whatsapp_client.get_redis = lambda: redis
    try:
        asyncio.run(whatsapp_client.send_text(None, "919999999999", "Booking confirmed"))
        check(len(redis.lists.get(outbound_queue.OUTBOX_KEY, [])) == 1, "send_text queues a job instead of sending immediately")
        job = json.loads(redis.lists[outbound_queue.OUTBOX_KEY][0])
        check(job["payload"]["text"]["body"] == "Booking confirmed", "the queued job carries the exact message text")
        check(job["payload"]["to"] == "919999999999", "the queued job carries the exact recipient")
    finally:
        whatsapp_client.get_redis = original_get_redis


if __name__ == "__main__":
    test_enqueue_pushes_a_well_formed_job()
    test_rate_limiter_allows_up_to_the_limit_then_blocks()
    test_requeue_with_backoff_schedules_a_retry()
    test_requeue_with_backoff_dead_letters_after_max_attempts()
    test_promote_ready_delayed_jobs_moves_only_expired_ones()
    test_send_text_enqueues_instead_of_calling_meta_directly()

    print("\n" + "=" * 50)
    if failures:
        print(f"OUTBOUND QUEUE TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL OUTBOUND QUEUE TESTS PASSED")
