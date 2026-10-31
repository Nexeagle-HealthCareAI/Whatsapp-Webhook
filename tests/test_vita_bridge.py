"""
Vita mode (app/vita_bridge.py + conversation._maybe_hand_to_vita): with VITA_CHAT_URL set, the
patient conversation goes to Vita (the 1HMS NLP Router's POST /v2/agent/chat, channel
"whatsapp") and Vita's ready-made WhatsApp messages are queued for the patient in order; the
bot's own flows (QR triggers, hospital-QR buttons, follow-up replies, appointment actions,
WhatsApp Flows) stay with the old handler. Contract: 1HMS-NLP-Router/docs/vita_whatsapp.md.

Same style as the other test_*.py files -- stubs aioodbc/redis before importing app modules,
mocks Vita with httpx.MockTransport, no pytest. Run directly: python3 tests/test_vita_bridge.py
"""

import asyncio
import json
import os
import sys
import types
from unittest.mock import patch

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
        self.ttl = {}

    @classmethod
    def from_url(cls, *args, **kwargs):
        return _mock_redis_instance

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        self.ttl[key] = ex
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

from app import conversation, vita_bridge  # noqa: E402
from app.config import Settings, settings  # noqa: E402

failures = []
PHONE = "919876543210"
VITA_URL = "http://vita.test/v2/agent/chat"


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


class _LegacyReached(Exception):
    """Raised by the patched _load_turn_context: proves the old handler took the message."""


class _Db:
    def __init__(self, state=None):
        self.state = state

    async def get_conversation_state(self, phone):
        return self.state


class _WhatsApp:
    def __init__(self):
        self.typing = []

    async def send_typing_indicator(self, client, message_id):
        self.typing.append(message_id)


class _Vita:
    """httpx.MockTransport handler: records each request, answers from a queue of
    (status, json body) or an exception to raise."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests = []
        self.headers = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        self.headers.append(dict(request.headers))
        answer = self.answers.pop(0) if self.answers else (200, _text_reply("ok"))
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return httpx.Response(status, json=body)


def _text_reply(body, session_id="s-1", **extra):
    return {"kind": "info", "language": "english", "session_id": session_id, "message": body,
            "whatsapp": [{"type": "text", "text": {"body": body}}], **extra}


def _list_reply(session_id="s-1"):
    lst = {"type": "interactive", "interactive": {
        "type": "list", "body": {"text": "Doctors near you"},
        "action": {"button": "Choose", "sections": [
            {"title": "Doctors", "rows": [{"id": "doc:00000000-0000-0000-0000-000000000001", "title": "Dr A"}]},
        ]}}}
    return {"kind": "doctor_list", "language": "english", "session_id": session_id, "message": "Doctors near you",
            "whatsapp": [{"type": "text", "text": {"body": "Here you go"}}, lst]}


def _drive(vita, *turns, state=None, url=VITA_URL, fallback="message", api_key="k-123", db=None):
    """Runs handle_message for each (input_type, input_value) turn. Returns (sent payloads,
    legacy-reached count, the db stub, the whatsapp stub)."""
    _mock_redis_instance.data.clear()
    _mock_redis_instance.ttl.clear()
    sent = []
    legacy = []
    db = db or _Db(state)
    wa = _WhatsApp()

    async def _enqueue(redis, payload):
        sent.append(payload)

    async def _legacy(*a, **k):
        legacy.append(a)
        raise _LegacyReached()

    async def _run():
        with patch.object(settings, "vita_chat_url", url), \
             patch.object(settings, "vita_api_key", api_key), \
             patch.object(settings, "vita_fallback", fallback), \
             patch.object(conversation, "db", db), \
             patch.object(conversation, "whatsapp_client", wa), \
             patch.object(conversation, "_load_turn_context", _legacy), \
             patch.object(vita_bridge.outbound_queue, "enqueue", _enqueue):
            async with httpx.AsyncClient(transport=httpx.MockTransport(vita)) as client:
                for i, (input_type, input_value) in enumerate(turns):
                    try:
                        await conversation.handle_message(client, PHONE, "Riya", input_type, input_value, f"m{i}")
                    except _LegacyReached:
                        pass

    run(_run())
    return sent, len(legacy), db, wa


def test_text_goes_to_vita_and_messages_are_queued_in_order():
    print("\n--- text -> Vita, messages queued in order with 'to' ---")
    vita = _Vita((200, _list_reply()))
    sent, legacy, _, wa = _drive(vita, ("text", "I need a skin doctor"))
    req = vita.requests[0] if vita.requests else {}
    check(req.get("channel") == "whatsapp" and req.get("text") == "I need a skin doctor", "text sent as text, channel whatsapp")
    check(req.get("verified_mobile") == PHONE, "sender's number sent as verified_mobile")
    check("session_id" not in req, "no session_id on the first message")
    check(vita.headers and vita.headers[0].get("x-api-key") == "k-123", "X-Api-Key header sent")
    check(len(sent) == 2, "both of Vita's messages queued")
    check([m.get("type") for m in sent] == ["text", "interactive"], "queued in Vita's order")
    check(all(m.get("to") == PHONE and m.get("messaging_product") == "whatsapp" for m in sent),
          "each message carries to + messaging_product")
    check(sent and sent[1]["interactive"]["action"]["sections"][0]["rows"][0]["id"].startswith("doc:"),
          "Vita's message body passed through untouched")
    check(legacy == 0, "old handler (and its safety triage / NLU) did not run")
    check(wa.typing == ["m0"], "typing indicator still sent")


def test_list_and_button_replies_send_reply_id():
    print("\n--- list / button reply -> reply_id exactly as received ---")
    vita = _Vita((200, _text_reply("a")), (200, _text_reply("b")))
    _drive(vita, ("list_reply", "doc:00000000-0000-0000-0000-000000000001"), ("button_reply", "say:No, just checking"))
    check(vita.requests[0].get("reply_id") == "doc:00000000-0000-0000-0000-000000000001" and "text" not in vita.requests[0],
          "list_reply -> reply_id, no text")
    check(vita.requests[1].get("reply_id") == "say:No, just checking", "button_reply -> reply_id")


def test_location_sends_lat_lng():
    print("\n--- location -> lat / lng ---")
    vita = _Vita((200, _text_reply("found")))
    _drive(vita, ("location", "22.5726,88.3639"))
    req = vita.requests[0]
    check(req.get("lat") == 22.5726 and req.get("lng") == 88.3639, "lat / lng sent as numbers")
    check("text" not in req and "reply_id" not in req, "no text with a shared location")


def test_session_id_is_kept_per_phone():
    print("\n--- session id reused across messages ---")
    vita = _Vita((200, _text_reply("first", session_id="sess-42")), (200, _text_reply("second", session_id="sess-42")))
    _drive(vita, ("text", "fever for 3 days"), ("text", "and a cough"))
    check(vita.requests[1].get("session_id") == "sess-42", "second message carries the first reply's session_id")
    key = f"vita:wa:session:{PHONE}"
    check(json.loads(_mock_redis_instance.data[key])["session_id"] == "sess-42", "session stored under vita:wa:session:<phone>")
    check(_mock_redis_instance.ttl.get(key) == 1800, "session TTL is 30 minutes")


def test_ui_language_from_bot_state():
    print("\n--- ui_language from the bot's saved language ---")
    vita = _Vita((200, _text_reply("ok")))
    _drive(vita, ("text", "doctor chahiye"), state={"current_step": None, "context": {"lang": "hg"}})
    check(vita.requests[0].get("ui_language") == "hinglish", "hg -> hinglish")


def test_stale_doctor_row_422_says_list_expired():
    print("\n--- 422 for a stale doc: id -> 'list has expired' ---")
    vita = _Vita((422, {"detail": "not shown in this session"}))
    sent, legacy, _, _ = _drive(vita, ("list_reply", "doc:00000000-0000-0000-0000-00000000dead"))
    check(len(sent) == 1 and "expired" in sent[0]["text"]["body"], "one 'list has expired' text")
    check(legacy == 0, "no fallback to the old handler")
    vita = _Vita((422, {"detail": "stale"}))
    sent, _, _, _ = _drive(vita, ("list_reply", "doc:00000000-0000-0000-0000-00000000dead"),
                           state={"current_step": None, "context": {"lang": "hi"}})
    check(len(sent) == 1 and "सूची" in sent[0]["text"]["body"], "Hindi patient gets the Hindi text")


def test_5xx_with_message_fallback_sends_apology_with_108():
    print("\n--- 5xx / timeout with VITA_FALLBACK=message -> apology with 108 ---")
    vita = _Vita((500, {"detail": "Internal agent error"}))
    sent, legacy, _, _ = _drive(vita, ("text", "I need a doctor"))
    check(len(sent) == 1 and "In an emergency call 108" in sent[0]["text"]["body"], "apology includes the 108 line")
    check("try again in a minute" in sent[0]["text"]["body"], "apology says to try again in a minute")
    check(legacy == 0, "old handler not run with fallback=message")
    vita = _Vita(httpx.ReadTimeout("slow"))
    sent, legacy, _, _ = _drive(vita, ("text", "I need a doctor"))
    check(len(sent) == 1 and "108" in sent[0]["text"]["body"] and legacy == 0, "timeout -> same apology")
    vita = _Vita((200, {"kind": "info"}))
    sent, legacy, _, _ = _drive(vita, ("text", "I need a doctor"))
    check(len(sent) == 1 and "108" in sent[0]["text"]["body"], "unknown reply shape -> same apology")


def test_reply_without_whatsapp_sends_message_as_text():
    print("\n--- reply without 'whatsapp' but with 'message' ---")
    vita = _Vita((200, {"kind": "info", "session_id": "s-9", "message": "Plain answer"}))
    sent, _, _, _ = _drive(vita, ("text", "hello"))
    check(len(sent) == 1 and sent[0].get("text") == {"body": "Plain answer"} and sent[0]["to"] == PHONE,
          "message sent as one text")


def test_legacy_fallback_runs_old_handler():
    print("\n--- 5xx with VITA_FALLBACK=legacy -> old handler ---")
    vita = _Vita((503, {"detail": "down"}))
    sent, legacy, _, _ = _drive(vita, ("text", "I need a doctor"), fallback="legacy")
    check(legacy == 1, "old handler called")
    check(sent == [], "no apology sent")


def test_vita_off_leaves_bot_unchanged():
    print("\n--- no VITA_CHAT_URL -> old handler, Vita never called ---")
    vita = _Vita()
    sent, legacy, _, wa = _drive(vita, ("text", "I need a doctor"), ("list_reply", "doc:x"), url=None)
    check(legacy == 2 and vita.requests == [] and sent == [], "both messages to the old handler, no Vita call")
    check(wa.typing == ["m0", "m1"], "typing indicator unchanged")


def test_bot_owned_messages_stay_with_bot():
    print("\n--- QR / CHECKIN / hospbook / Flows / follow-up / appointment steps stay with the bot ---")
    cases = [
        (("text", "CHECKIN APLO4F"), None, "CHECKIN trigger"),
        (("text", "DRBOOK 3fa85f64-5717-4562-b3fc-2c963f66afa6"), None, "DRBOOK trigger"),
        (("text", "Hey, I've scanned the QR code for Apollo! QR ID of this hospital is APLO4F"), None, "hospital QR text"),
        (("text", "RX att-1"), None, "prescription QR trigger"),
        (("button_reply", "hospbook_book"), None, "hospbook_book button"),
        (("button_reply", "hospbook_status"), None, "hospbook_status button"),
        (("button_reply", "start_booking"), None, "start_booking button"),
        (("nfm_reply", '{"name": "Riya"}'), None, "WhatsApp Flow reply"),
        (("text", "I'm feeling fine"), {"current_step": "awaiting_followup_reply", "context": {}}, "follow-up reply step"),
        (("button_reply", "yes"), {"current_step": "confirming_appointment_cancel", "context": {}}, "appointment action step"),
        (("location", "22.5,88.3"), {"current_step": "checkin_awaiting_location", "context": {}}, "check-in location step"),
    ]
    for turn, state, label in cases:
        vita = _Vita()
        sent, legacy, _, _ = _drive(vita, turn, state=state)
        check(legacy == 1 and vita.requests == [], f"{label} -> old handler, not Vita")


def test_bot_flow_continues_with_bot_until_state_clears():
    print("\n--- a QR booking keeps going with the bot; afterwards Vita takes over ---")
    vita = _Vita((200, _text_reply("Vita here")))

    class _StepDb(_Db):
        def __init__(self):
            super().__init__(None)
            self.turn = 0

        async def get_conversation_state(self, phone):
            # turn 0: DRBOOK with no state; turn 1: bot saved "choosing_slot"; turn 2: booking done, state cleared
            states = [None, {"current_step": "choosing_slot", "context": {"lang": "en"}}, None]
            state = states[min(self.turn, 2)]
            self.turn += 1
            return state

    db = _StepDb()
    sent, legacy, _, _ = _drive(vita, ("text", "DRBOOK 3fa85f64-5717-4562-b3fc-2c963f66afa6"),
                                ("button_reply", "slot_today_morning"), ("text", "I need a skin doctor"), db=db)
    check(legacy == 2, "trigger and the next step went to the old handler")
    check(len(vita.requests) == 1 and vita.requests[0].get("text") == "I need a skin doctor",
          "after the bot's flow cleared its state, Vita took the next message")


def test_blank_env_values_are_safe():
    print("\n--- blank env values from docker-compose keep Vita off and defaults safe ---")
    s = Settings(vita_chat_url="", vita_api_key="", vita_timeout_seconds="", vita_fallback="")
    check(s.vita_chat_url is None and s.vita_api_key is None, "empty URL / key -> None (Vita off)")
    check(s.vita_timeout_seconds == 12.0 and s.vita_fallback == "message", "empty timeout / fallback -> defaults")
    s = Settings(vita_chat_url=" http://x/v2/agent/chat ", vita_fallback="LEGACY", vita_timeout_seconds="8")
    check(s.vita_chat_url == "http://x/v2/agent/chat" and s.vita_fallback == "legacy" and s.vita_timeout_seconds == 8.0,
          "values parsed")
    if not os.environ.get("VITA_CHAT_URL"):
        check(Settings(_env_file=None).vita_chat_url is None, "Vita off by default")


if __name__ == "__main__":
    test_text_goes_to_vita_and_messages_are_queued_in_order()
    test_list_and_button_replies_send_reply_id()
    test_location_sends_lat_lng()
    test_session_id_is_kept_per_phone()
    test_ui_language_from_bot_state()
    test_stale_doctor_row_422_says_list_expired()
    test_5xx_with_message_fallback_sends_apology_with_108()
    test_reply_without_whatsapp_sends_message_as_text()
    test_legacy_fallback_runs_old_handler()
    test_vita_off_leaves_bot_unchanged()
    test_bot_owned_messages_stay_with_bot()
    test_bot_flow_continues_with_bot_until_state_clears()
    test_blank_env_values_are_safe()

    print(f"\n{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
