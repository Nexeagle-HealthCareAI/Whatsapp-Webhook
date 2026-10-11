"""
The Health Wiki's WhatsApp messages are sent by easyHMSAPI from the same WhatsApp number as this bot, so Meta's
"delivered / read / failed" callbacks for them arrive at this webhook. app/front_door/whatsapp_ingest.py passes those on to
easyHMSAPI (when HEALTH_WIKI_INTERNAL_KEY is set) and must never let that affect Meta's answer or the booking bot.
Run directly: python3 tests/test_health_wiki_delivery.py
"""

import os
import sys
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Placeholder settings, so this runs on a plain checkout (no .env, no secrets).
for _k, _v in {
    "WHATSAPP_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "pn1", "WHATSAPP_VERIFY_TOKEN": "v", "WHATSAPP_APP_SECRET": "s",
    "REDIS_URL": "redis://x", "SQLSERVER_CONN_STRING": "x", "INTERNAL_EVENTS_TOKEN": "i",
}.items():
    os.environ.setdefault(_k, _v)

_fake = types.ModuleType("aioodbc")
_fake.Pool = object


async def _create_pool(*a, **k):
    raise NotImplementedError


_fake.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake)

_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")


class MockRedis:
    def __init__(self):
        self.data = {}

    @classmethod
    def from_url(cls, *args, **kwargs):
        return _mock_redis_instance

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def lpush(self, key, value):
        self.data.setdefault(key, []).append(value)
        return True


_mock_redis_instance = MockRedis()
_fake_redis_mod.asyncio.Redis = MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

import asyncio
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app import main
from app.config import settings
from app.front_door import whatsapp_ingest as ingest

client = TestClient(main.app)
failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def _status_payload(statuses, phone_number_id=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": phone_number_id or settings.whatsapp_phone_number_id}, "statuses": statuses}}]}],
    }


def _signed_post(payload):
    body = json.dumps(payload).encode("utf-8")
    sig = hmac.new(settings.whatsapp_app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body, headers={"X-Hub-Signature-256": f"sha256={sig}"})


def test_only_delivered_read_and_failed_are_passed_on():
    check(ingest._health_wiki_delivery({"id": "w1", "status": "sent"}) is None, "a 'sent' status says nothing new")
    check(ingest._health_wiki_delivery({"id": "w1", "status": "delivered"}) == {"messageId": "w1", "status": "delivered", "detail": None}, "delivered is passed on")
    check(ingest._health_wiki_delivery({"id": "w1", "status": "read"})["status"] == "read", "read is passed on")
    check(ingest._health_wiki_delivery({"status": "delivered"}) is None, "a status with no message id is ignored")


def test_a_failure_carries_whatsapps_own_reason():
    d = ingest._health_wiki_delivery({"id": "w2", "status": "failed", "errors": [{"code": 131049, "title": "Message not delivered to maintain healthy ecosystem engagement"}]})
    check(d is not None and d["detail"].startswith("131049: Message not delivered"), f"keeps code and title, got {d!r}")
    check(ingest._health_wiki_delivery({"id": "w3", "status": "failed"})["detail"] is None, "a failure with no error block has no detail")


def test_statuses_for_another_environments_number_are_ignored():
    mine = ingest._extract_statuses(_status_payload([{"id": "w1", "status": "delivered"}]))
    other = ingest._extract_statuses(_status_payload([{"id": "w1", "status": "delivered"}], phone_number_id="another-number"))
    check(len(mine) == 1, "a status for this number is read")
    check(other == [], "a status for a different phone_number_id is ignored")


def test_nothing_is_passed_on_while_the_key_is_unset():
    sent = []

    async def fake_pass(delivery):
        sent.append(delivery)

    original, key = ingest._pass_on_to_health_wiki, settings.health_wiki_internal_key
    ingest._pass_on_to_health_wiki = fake_pass
    try:
        settings.health_wiki_internal_key = None
        r = _signed_post(_status_payload([{"id": "w1", "status": "delivered"}]))
        check(r.status_code == 200 and sent == [], f"acks 200 and passes nothing on without the key, got {r.status_code} {sent!r}")
        settings.health_wiki_internal_key = "k"
        r = _signed_post(_status_payload([{"id": "w1", "status": "delivered"}, {"id": "w2", "status": "sent"}]))
        check(r.status_code == 200 and [d["messageId"] for d in sent] == ["w1"], f"passes on only the delivered one with the key, got {sent!r}")
    finally:
        ingest._pass_on_to_health_wiki = original
        settings.health_wiki_internal_key = key


def test_a_pass_on_failure_never_raises():
    class Boom:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise RuntimeError("network down")

        async def __aexit__(self, *a):
            return False

    original = ingest.httpx.AsyncClient
    ingest.httpx.AsyncClient = Boom
    try:
        asyncio.run(ingest._pass_on_to_health_wiki({"messageId": "w1", "status": "delivered", "detail": None}))
        check(True, "an unreachable easyHMSAPI is logged, not raised")
    except Exception as exc:  # noqa: BLE001
        check(False, f"raised {exc!r}")
    finally:
        ingest.httpx.AsyncClient = original


if __name__ == "__main__":
    tests = [
        test_only_delivered_read_and_failed_are_passed_on,
        test_a_failure_carries_whatsapps_own_reason,
        test_statuses_for_another_environments_number_are_ignored,
        test_nothing_is_passed_on_while_the_key_is_unset,
        test_a_pass_on_failure_never_raises,
    ]
    for test in tests:
        test()
    if failures:
        print(f"FAILED ({len(failures)})")
        sys.exit(1)
    print(f"PASSED — {len(tests)} checks, Health Wiki delivery pass-on verified")
