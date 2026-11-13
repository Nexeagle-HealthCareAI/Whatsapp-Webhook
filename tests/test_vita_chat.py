"""
Covers Vita as the front door of the doctor search (app/conversation/vita_chat.py, app/messengers/vita_client.py):
who it is switched on for, what goes to Vita and what comes back, the hand-over to the bot's own booking when a doctor
is picked, and every way it falls back to the fixed buttons. Vita itself is faked here; the live router is exercised
by tests/e2e. Same style as test_last_search_reuse.py -- stubs aioodbc/redis before importing app modules, no pytest.
Run directly: python3 tests/test_vita_chat.py
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

import httpx  # noqa: E402

from app import conversation  # noqa: E402
from app.config import settings  # noqa: E402
from app.conversation import specialty_browsing, vita_chat  # noqa: E402
from app.messengers import vita_client  # noqa: E402
from app.messengers.hms_client import HmsApiError  # noqa: E402
from app.messengers.vita_client import VitaRejected, VitaUnavailable  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


PHONE = "919876543210"
DOCTOR_ID = "54bd9a9c-1e8a-40ea-b4cf-4e2815222d31"
ROW_REPLY = {"kind": "doctor_list", "session_id": "vs-1", "whatsapp": [
    {"type": "text", "text": {"body": "Here are some skin doctors"}},
    {"type": "interactive", "interactive": {"type": "list", "body": {"text": "Doctors"}, "action": {
        "button": "Choose doctor", "sections": [{"title": "Doctors", "rows": [{"id": f"doc:{DOCTOR_ID}", "title": "Dr A"}]}]}}},
]}
SELECTED = {"kind": "doctor_selected", "session_id": "vs-1", "doctor": {"doctorId": DOCTOR_ID, "name": "Dr. Ashok Rao"},
            "whatsapp": [{"type": "text", "text": {"body": "Which day suits you?"}}]}
HMS_DOCTOR = {"doctorId": DOCTOR_ID, "fullName": "Dr. Ashok Rao", "hospitalId": "h1", "hospitalName": "Kolkata General",
              "address": "Kolkata", "city": "Kolkata", "latitude": 22.5, "longitude": 88.3, "fee": 700}


class _Wa:
    def __init__(self):
        self.texts, self.prepared, self.buttons = [], [], []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_prepared(self, client, to, message):
        self.prepared.append((to, message))

    async def send_buttons(self, client, to, body_text, buttons):
        self.buttons.append((body_text, buttons))


class _Db:
    def __init__(self):
        self.saved = []

    async def save_conversation_state(self, phone, step, context):
        self.saved.append((step, dict(context)))

    async def clear_conversation_state(self, phone):
        pass


class World:
    """Swaps in fakes for everything Vita's front door touches and puts them all back."""

    def __init__(self, replies=None, allowed=PHONE, configured=True):
        self.replies = list(replies or [])
        self.calls = []
        self.wa, self.db = _Wa(), _Db()
        self.transitions, self.handed_over, self.manual = [], [], []
        self.doctor = HMS_DOCTOR
        self.allowed, self.configured = allowed, configured

    def __enter__(self):
        self._saved = (
            conversation.whatsapp_client, conversation.db, conversation._transition_to, conversation._handle_choosing_doctor,
            conversation._maybe_handle_manual_command, vita_client.chat, vita_chat.hms_client,
            settings.vita_api_url, settings.vita_api_key, settings.vita_allowed_phones,
        )
        conversation.whatsapp_client, conversation.db = self.wa, self.db

        async def transition(phone, step, context, current):
            self.transitions.append((step, current))
            self.db.saved.append((step, dict(context)))

        async def handed(client, phone, input_type, input_value, context):
            self.handed_over.append((input_type, input_value, dict(context)))

        async def manual(client, phone, context, step, lang, input_type, input_value):
            self.manual.append(input_value)
            return True

        async def chat(**kwargs):
            self.calls.append(kwargs)
            item = self.replies.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        world = self

        class _Hms:
            @staticmethod
            async def get_doctor_by_id(doctor_id):
                if world.doctor is None:
                    raise HmsApiError("Doctor not found")
                return world.doctor

        conversation._transition_to = transition
        conversation._handle_choosing_doctor = handed
        conversation._maybe_handle_manual_command = manual
        vita_client.chat = chat
        vita_chat.hms_client = _Hms
        settings.vita_api_url = "http://vita.test" if self.configured else None
        settings.vita_api_key = "k" if self.configured else None
        settings.vita_allowed_phones = self.allowed
        return self

    def __exit__(self, *exc):
        (conversation.whatsapp_client, conversation.db, conversation._transition_to, conversation._handle_choosing_doctor,
         conversation._maybe_handle_manual_command, vita_client.chat, vita_chat.hms_client,
         settings.vita_api_url, settings.vita_api_key, settings.vita_allowed_phones) = self._saved


def turn(input_type, input_value, context=None, step="vita_chat"):
    context = {"lang": "en", "city": "Kolkata", "session_id": "s", **(context or {})}
    return context, run(vita_chat.maybe_handle(None, PHONE, context, step, input_type, input_value))


def test_who_it_is_switched_on_for():
    print("\n--- enabled_for: configured AND on the allowlist AND not switched off for this conversation ---")
    with World(allowed="+91 98765 43210, 8000000000"):
        check(vita_chat.enabled_for("919876543210") is True, "a number in another format on the list (last 10 digits) is on")
        check(vita_chat.enabled_for("918000000000") is True, "the second number on the list is on")
        check(vita_chat.enabled_for("919999999999") is False, "a number not on the list is off")
        check(vita_chat.enabled_for("") is False, "an empty number is never on")
        check(vita_chat.enabled_for("919876543210", {"vita_off": True}) is False, "off for the rest of a conversation that fell back")
    with World(allowed="*"):
        check(vita_chat.enabled_for("919999999999") is True, "* switches it on for everyone")
    with World(allowed=""):
        check(vita_chat.enabled_for(PHONE) is False, "an empty list is off for everyone")
    with World(configured=False, allowed="*"):
        check(vita_chat.enabled_for(PHONE) is False, "no URL or key means off, whatever the list says")


def test_the_search_mode_step_is_the_front_door():
    print("\n--- _send_search_mode_prompt: Vita for a switched-on number, the buttons for everyone else ---")
    with World() as w:
        run(specialty_browsing._send_search_mode_prompt(None, PHONE, {"lang": "en"}))
        check(w.transitions == [("vita_chat", "choosing_location")], f"moves to the vita_chat step, got {w.transitions}")
        check(len(w.wa.texts) == 1 and not w.wa.buttons, "sends the introduction, not the three search buttons")
    with World(allowed="911111111111") as w:
        async def phrase(client, step, context, key):
            return "How would you like to find a doctor?"
        saved = conversation._phrase
        conversation._phrase = phrase
        try:
            run(specialty_browsing._send_search_mode_prompt(None, PHONE, {"lang": "en"}))
        finally:
            conversation._phrase = saved
        check(len(w.wa.buttons) == 1 and len(w.wa.buttons[0][1]) == 3, "a number that is not switched on gets the three search buttons, as before")
        check(w.transitions == [("choosing_search_mode", "choosing_location")], f"and stays on choosing_search_mode, got {w.transitions}")


def test_text_goes_to_vita_and_its_messages_come_back():
    print("\n--- A typed message goes to Vita with the session and city; every message of the reply is sent ---")
    with World(replies=[ROW_REPLY]) as w:
        context, handled = turn("text", "skin ka doctor chahiye", {"vita_session_id": "vs-0"})
        check(handled is True, "the turn is Vita's")
        check(w.calls == [{"text": "skin ka doctor chahiye", "session_id": "vs-0", "city": "Kolkata"}], f"sent text + session + city, got {w.calls}")
        check(len(w.wa.prepared) == 2 and all(to == PHONE for to, _ in w.wa.prepared), "both messages of the reply are sent to the patient")
        check(context.get("vita_session_id") == "vs-1", "the session Vita returned is kept for the next turn")
        check(w.db.saved and w.db.saved[-1][0] == "vita_chat", "the conversation stays in the vita_chat step")


def test_taps_and_locations_go_back_as_the_right_fields():
    print("\n--- A tapped row is a reply_id, a shared location is lat/lng ---")
    with World(replies=[ROW_REPLY, ROW_REPLY, ROW_REPLY]) as w:
        turn("list_reply", "more", {"vita_session_id": "vs-1"})
        turn("button_reply", "refine:fee", {"vita_session_id": "vs-1"})
        turn("location", "22.57,88.36", {"vita_session_id": "vs-1"})
        check(w.calls[0].get("reply_id") == "more" and "text" not in w.calls[0], f"list tap -> reply_id, got {w.calls[0]}")
        check(w.calls[1].get("reply_id") == "refine:fee", f"button tap -> reply_id, got {w.calls[1]}")
        check(w.calls[2].get("lat") == 22.57 and w.calls[2].get("lng") == 88.36, f"location -> lat/lng, got {w.calls[2]}")


def test_picking_a_doctor_hands_over_to_the_bots_own_booking():
    print("\n--- doctor_selected: Vita's own messages are NOT sent; the bot's choosing-doctor step runs with the 1HMS doctor ---")
    with World(replies=[SELECTED]) as w:
        context, handled = turn("list_reply", f"doc:{DOCTOR_ID}", {"vita_session_id": "vs-1"})
        check(handled is True, "handled")
        check(not w.wa.prepared and not w.wa.texts, "Vita's 'which day' message is not sent -- the bot asks that itself")
        check(len(w.handed_over) == 1, f"the bot's own _handle_choosing_doctor runs once, got {len(w.handed_over)}")
        input_type, input_value, ctx = w.handed_over[0]
        check((input_type, input_value) == ("list_reply", DOCTOR_ID), f"as a tap on that doctor, got {(input_type, input_value)}")
        check(ctx["doctor_options"] == {DOCTOR_ID: HMS_DOCTOR}, "with the doctor as 1HMS returns it (same shape as the bot's own list)")
        check("vita_session_id" not in ctx, "the Vita session is dropped, the conversation is the bot's again")


def test_a_doctor_1hms_cannot_confirm_is_not_booked():
    print("\n--- doctor_selected but 1HMS has no such doctor: say so, stay with Vita, start no booking ---")
    with World(replies=[SELECTED]) as w:
        w.doctor = None
        turn("list_reply", f"doc:{DOCTOR_ID}", {"vita_session_id": "vs-1"})
        check(not w.handed_over, "no booking step is started")
        check(len(w.wa.texts) == 1, "the patient is told something went wrong")
        check(w.db.saved and w.db.saved[-1][0] == "vita_chat", "and stays in the vita_chat step")


def test_vita_down_falls_back_to_the_buttons():
    print("\n--- Vita unreachable or erroring: a notice, the three buttons, and Vita off for this conversation ---")
    for failure in (VitaUnavailable("down"), VitaRejected(401), VitaRejected(429)):
        with World(replies=[failure]) as w:
            async def phrase(client, step, context, key):
                return "How would you like to find a doctor?"
            saved = conversation._phrase
            conversation._phrase = phrase
            try:
                context, handled = turn("text", "bukhar hai")
            finally:
                conversation._phrase = saved
            name = type(failure).__name__ + (f" {failure.status_code}" if isinstance(failure, VitaRejected) else "")
            check(handled is True, f"{name}: the turn is still answered")
            check(len(w.wa.texts) == 1 and len(w.wa.buttons) == 1, f"{name}: notice + the search buttons")
            check(context.get("vita_off") is True, f"{name}: Vita is off for the rest of the conversation")
            check(vita_chat.enabled_for(PHONE, context) is False, f"{name}: so the front door no longer sends it back to Vita")


def test_an_expired_session_is_recovered():
    print("\n--- 422: an old tap asks again; a typed message starts a new Vita session ---")
    with World(replies=[VitaRejected(422)]) as w:
        context, _ = turn("list_reply", "doc:old", {"vita_session_id": "vs-old"})
        check("vita_session_id" not in context, "the dead session is dropped")
        check(len(w.wa.texts) == 1 and not w.wa.buttons, "the patient is asked again, in Vita (no fallback)")
        check(context.get("vita_off") is not True, "Vita stays on")
    with World(replies=[VitaRejected(422), ROW_REPLY]) as w:
        context, _ = turn("text", "skin doctor", {"vita_session_id": "vs-old"})
        check(len(w.calls) == 2 and w.calls[1].get("session_id") is None, f"retried once without the old session, got {w.calls}")
        check(len(w.wa.prepared) == 2, "and the answer is sent")


def test_appointment_management_leaves_vita():
    print("\n--- 'cancel my appointment' is not Vita's: the bot's own handling takes the message ---")
    for text in ("meri appointment cancel karni hai", "I want to reschedule", "मेरी अपॉइंटमेंट कैंसिल करनी है"):
        with World() as w:
            context, handled = turn("text", text, {"vita_session_id": "vs-1"})
            check(handled is False, f"{text!r}: not handled here, the regular pipeline continues")
            check(not w.calls, f"{text!r}: Vita is not asked")
            check(context.get("vita_off") is True and w.db.saved[-1][0] == "choosing_search_mode", f"{text!r}: back on the bot's own step")
    with World(replies=[ROW_REPLY]) as w:
        _, handled = turn("text", "skin doctor appointment chahiye")
        check(handled is True and len(w.calls) == 1, "a search that merely says 'appointment' still goes to Vita")


def test_commands_and_other_steps_and_unsupported_input():
    print("\n--- 'back'/'cancel' stay the bot's; other steps are untouched; an image gets the introduction ---")
    with World() as w:
        _, handled = turn("text", "back")
        check(handled is True and w.manual == ["back"] and not w.calls, "'back' goes to the bot's own command handling, not Vita")
    with World() as w:
        _, handled = turn("text", "hello", step="choosing_search_mode")
        check(handled is False and not w.calls, "outside the vita_chat step nothing is touched")
    with World() as w:
        _, handled = turn("image", "media-id")
        check(handled is True and not w.calls and len(w.wa.texts) == 1, "an image gets the introduction again and Vita is not asked")
    with World(allowed="911111111111") as w:
        context, handled = turn("text", "bukhar hai")
        check(handled is True and not w.calls and context.get("vita_off") is True, "switched off while mid-conversation: quietly back to the bot's own steps")


def test_vita_client_requests_and_errors():
    print("\n--- vita_client.chat: what it sends, and how it reports each kind of failure ---")
    seen = {}

    def make(handler):
        def get_client():
            return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://vita.test")
        return get_client

    def ok(request):
        seen["req"] = request
        return httpx.Response(200, json={"kind": "follow_up", "session_id": "vs-9", "whatsapp": []})

    original = vita_client._get_client
    saved = (settings.vita_api_url, settings.vita_api_key)
    settings.vita_api_url, settings.vita_api_key = "http://vita.test", "secret-key"
    try:
        vita_client._get_client = make(ok)
        reply = run(vita_client.chat(text="bukhar", session_id="vs-1", city="Kolkata"))
        import json as _json
        body = _json.loads(seen["req"].content)
        check(seen["req"].url.path == "/v2/agent/chat", "POST /v2/agent/chat")
        check(seen["req"].headers.get("x-api-key") == "secret-key", "the key goes in X-Api-Key")
        check(body == {"channel": "whatsapp", "text": "bukhar", "session_id": "vs-1", "city": "Kolkata"}, f"body, got {body}")
        check(reply["session_id"] == "vs-9", "returns Vita's reply")

        run(vita_client.chat(reply_id="doc:1"))
        check(_json.loads(seen["req"].content) == {"channel": "whatsapp", "reply_id": "doc:1"}, "a tap carries only reply_id")
        run(vita_client.chat(lat=1.5, lng=2.5))
        check(_json.loads(seen["req"].content) == {"channel": "whatsapp", "lat": 1.5, "lng": 2.5}, "a location carries only lat/lng")

        for status, expected in ((422, VitaRejected), (401, VitaRejected), (503, VitaUnavailable)):
            vita_client._get_client = make(lambda r, s=status: httpx.Response(s, json={}))
            try:
                run(vita_client.chat(text="x"))
                raised = None
            except Exception as exc:  # noqa: BLE001
                raised = exc
            check(type(raised) is expected, f"HTTP {status} -> {expected.__name__}, got {type(raised).__name__}")
        vita_client._get_client = make(lambda r: httpx.Response(200, text="<html>"))
        try:
            run(vita_client.chat(text="x"))
            raised = None
        except VitaUnavailable as exc:
            raised = exc
        check(raised is not None, "a reply that is not JSON -> VitaUnavailable")

        def boom(request):
            raise httpx.ConnectError("refused")
        vita_client._get_client = make(boom)
        try:
            run(vita_client.chat(text="x"))
            raised = None
        except VitaUnavailable as exc:
            raised = exc
        check(raised is not None, "a connection error -> VitaUnavailable")
    finally:
        vita_client._get_client = original
        settings.vita_api_url, settings.vita_api_key = saved


if __name__ == "__main__":
    test_who_it_is_switched_on_for()
    test_the_search_mode_step_is_the_front_door()
    test_text_goes_to_vita_and_its_messages_come_back()
    test_taps_and_locations_go_back_as_the_right_fields()
    test_picking_a_doctor_hands_over_to_the_bots_own_booking()
    test_a_doctor_1hms_cannot_confirm_is_not_booked()
    test_vita_down_falls_back_to_the_buttons()
    test_an_expired_session_is_recovered()
    test_appointment_management_leaves_vita()
    test_commands_and_other_steps_and_unsupported_input()
    test_vita_client_requests_and_errors()

    print("\n" + "=" * 50)
    if failures:
        print(f"VITA CHAT TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL VITA CHAT TESTS PASSED")
