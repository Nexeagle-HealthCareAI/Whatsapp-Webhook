"""
Two things that made the first minutes of a conversation feel broken (live-reported on Dev):
  - _start sent the welcome twice (a text, then the language list repeating it);
  - a remark made while the patient was choosing a sort order ("galti ho gya") was answered, and then re-sending the sort
    prompt crashed (_send_sort_prompt's current_step was never passed), which _trigger_step_prompt turns into a full
    restart -- so the patient was thrown back to the language list.
Same style as test_last_search_reuse.py. Run directly: python3 tests/test_smooth_start.py
"""

import asyncio
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

from app import conversation  # noqa: E402
from app.conversation import language  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


class _Wa:
    def __init__(self):
        self.texts, self.lists, self.buttons = [], [], []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_list(self, client, to, body_text, button_label, rows, section_title="Options"):
        self.lists.append((body_text, rows))

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))


class _Db:
    async def save_conversation_state(self, phone, step, context):
        pass

    async def save_last_specialty(self, phone, category):
        pass


def test_start_sends_one_welcome_not_two():
    print("\n--- _start: a single message, carrying the welcome, what the bot does and the language choice ---")
    wa, saved = _Wa(), (conversation.whatsapp_client, conversation._transition_to)
    conversation.whatsapp_client = wa

    async def transition(*a, **k):
        pass
    conversation._transition_to = transition
    try:
        run(language._start(None, "919876543210"))
    finally:
        conversation.whatsapp_client, conversation._transition_to = saved
    check(wa.texts == [], f"no separate welcome text, got {wa.texts!r}")
    check(len(wa.lists) == 1, f"exactly one list message, got {len(wa.lists)}")
    body = wa.lists[0][0]
    check("Welcome" in body and "Doctor search" in body and "language" in body.lower(), "the one message still welcomes, names what the bot does, and asks for a language")
    check(len(body) <= 1024, f"within WhatsApp's 1024-character body limit, got {len(body)}")


def test_reprompting_the_sort_step_does_not_crash():
    print("\n--- _prompt_choosing_sort: re-sends the sort list instead of raising (which restarted the conversation) ---")
    wa = _Wa()
    saved = (conversation.whatsapp_client, conversation.db, conversation._transition_to)
    conversation.whatsapp_client, conversation.db = wa, _Db()

    async def transition(*a, **k):
        pass
    conversation._transition_to = transition
    try:
        context = {"lang": "en", "specialty_category": "General Physician", "current_step": "choosing_sort", "city": "Kolkata"}
        error = None
        try:
            run(conversation._prompt_choosing_sort(None, "919876543210", context))
        except Exception as exc:  # noqa: BLE001
            error = exc
    finally:
        conversation.whatsapp_client, conversation.db, conversation._transition_to = saved
    check(error is None, f"does not raise, got {error!r}")
    check(len(wa.lists) == 1, f"sends the sort list again, got {len(wa.lists)} lists")


def test_a_failed_prompt_is_the_only_thing_that_restarts():
    print("\n--- _trigger_step_prompt still restarts when a prompt really fails (unchanged safety net) ---")
    starts = []

    async def boom(client, phone, context):
        raise RuntimeError("x")

    async def fake_start(client, phone, init=None):
        starts.append(phone)
    saved = (conversation.STEP_REGISTRY["choosing_sort"], conversation._start)
    conversation.STEP_REGISTRY["choosing_sort"] = {"handler": saved[0]["handler"], "prompt": boom}
    conversation._start = fake_start
    try:
        run(conversation._trigger_step_prompt(None, "919876543210", "choosing_sort", {}))
    finally:
        conversation.STEP_REGISTRY["choosing_sort"], conversation._start = saved
    check(starts == ["919876543210"], "a prompt that raises still falls back to _start")


if __name__ == "__main__":
    test_start_sends_one_welcome_not_two()
    test_reprompting_the_sort_step_does_not_crash()
    test_a_failed_prompt_is_the_only_thing_that_restarts()

    print("\n" + "=" * 50)
    if failures:
        print(f"SMOOTH START TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL SMOOTH START TESTS PASSED")
