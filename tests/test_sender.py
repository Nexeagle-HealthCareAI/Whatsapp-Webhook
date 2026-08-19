"""
tests/test_sender.py
-----------------------
Covers sender.py's dispatch_job -- specifically the per-recipient ordering fix: two jobs
queued back-to-back for the SAME phone must have their Meta API calls happen in enqueue
order even though dispatch_job fires every job as an independent concurrent task for
throughput, while two jobs for DIFFERENT phones must stay fully concurrent (no throughput
lost across the conversations that matter for the rate limit).

Live-reported bug this guards against: a language-confirmation text queued immediately
before a location-request prompt arrived on the patient's phone out of order, because
nothing previously guaranteed the first job's HTTP round-trip actually completed before the
second one's did. Run directly: python3 tests/test_sender.py
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

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

import sender  # noqa: E402

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
        self.counters: dict[str, int] = {}

    async def lrem(self, key, count, value):
        lst = self.lists.get(key, [])
        if value in lst:
            lst.remove(value)
            return 1
        return 0

    async def lpush(self, key, *values):
        self.lists.setdefault(key, [])
        for v in values:
            self.lists[key].insert(0, v)

    async def zadd(self, key, mapping):
        pass

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, ttl):
        return True


class FakeResponse:
    status_code = 200
    text = "ok"


class FakeClient:
    """Records the order in which .post is actually CALLED (not completed) and lets each
    call sleep for a controlled duration before returning, to simulate network jitter."""

    def __init__(self, delays: dict[str, float] | None = None):
        self.calls: list[str] = []
        self._delays = delays or {}

    async def post(self, url, headers=None, json=None):
        label = json.get("text", {}).get("body", "") or json.get("to", "")
        self.calls.append(label)
        delay = self._delays.get(label, 0)
        if delay:
            await asyncio.sleep(delay)
        return FakeResponse()


def _job(to: str, body: str) -> str:
    return json.dumps({"payload": {"to": to, "text": {"body": body}}, "attempt": 0})


def test_same_recipient_jobs_are_sent_in_enqueue_order_despite_jitter():
    redis = FakeRedis()
    # "first" is artificially SLOWER than "second" -- without per-recipient chaining,
    # "second" would complete (and thus arrive on the patient's phone) first.
    client = FakeClient(delays={"first": 0.05, "second": 0.0})

    async def _run():
        last_task_per_phone: dict = {}
        background_tasks: set = set()
        t1 = sender.dispatch_job(client, redis, _job("919999999999", "first"), last_task_per_phone, background_tasks)
        t2 = sender.dispatch_job(client, redis, _job("919999999999", "second"), last_task_per_phone, background_tasks)
        await asyncio.gather(t1, t2)

    asyncio.run(_run())
    check(client.calls == ["first", "second"], f"same-recipient jobs must be POSTed in enqueue order, got {client.calls!r}")


def test_different_recipients_stay_concurrent():
    redis = FakeRedis()
    # "slow" belongs to phone A; if phone B's job were wrongly chained behind it, "fast"
    # wouldn't even start being posted until after "slow" finished.
    client = FakeClient(delays={"slow": 0.05, "fast": 0.0})
    start_order: list[str] = []
    original_post = client.post
    async def tracking_post(url, headers=None, json=None):
        start_order.append(json.get("text", {}).get("body", ""))
        return await original_post(url, headers=headers, json=json)
    client.post = tracking_post

    async def _run():
        last_task_per_phone: dict = {}
        background_tasks: set = set()
        t1 = sender.dispatch_job(client, redis, _job("919999999999", "slow"), last_task_per_phone, background_tasks)
        t2 = sender.dispatch_job(client, redis, _job("918888888888", "fast"), last_task_per_phone, background_tasks)
        await asyncio.gather(t1, t2)

    asyncio.run(_run())
    check(start_order == ["slow", "fast"], f"both POSTs should start immediately regardless of the other phone's delay, got start order {start_order!r}")


def test_last_task_per_phone_is_cleaned_up_after_completion():
    redis = FakeRedis()
    client = FakeClient()

    async def _run():
        last_task_per_phone: dict = {}
        background_tasks: set = set()
        task = sender.dispatch_job(client, redis, _job("919999999999", "hi"), last_task_per_phone, background_tasks)
        await task
        return last_task_per_phone

    remaining = asyncio.run(_run())
    check(remaining == {}, f"a completed send's entry must be pruned, not leaked forever, got {remaining!r}")


if __name__ == "__main__":
    test_same_recipient_jobs_are_sent_in_enqueue_order_despite_jitter()
    test_different_recipients_stay_concurrent()
    test_last_task_per_phone_is_cleaned_up_after_completion()

    print("\n" + "=" * 50)
    if failures:
        print(f"SENDER TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL SENDER TESTS PASSED")
