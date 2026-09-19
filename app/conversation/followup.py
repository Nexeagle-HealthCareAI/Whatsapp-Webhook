"""
app/conversation/followup.py
------------------------------
Handles a patient's reply to scheduler.py's day-after-visit follow-up message ("how are you
feeling? reply if you need a follow-up booked").

Live-reported bug this file exists to fix: scheduler.py sends that message completely outside
the conversation state machine (no conversation_state row is written), and a successful
booking already DELETEs whatever state existed. So a patient's reply used to land on
handle_message with a totally blank context -- exactly as if they were a brand-new patient
messaging for the first time. Their casual "I'm feeling fine" got NLU-classified and routed
into the normal symptom/specialty-matching pipeline, which (correctly, for a REAL symptom
description) replied "I couldn't confidently match that to a specialty — here's the full list
instead". Confusing and wrong for what was meant as a simple check-in reply.

The fix has two parts:
1. scheduler.py now saves a conversation_state row (current_step="awaiting_followup_reply")
   right after sending the follow-up, so the reply arrives with real state instead of none.
2. This step is intercepted VERY EARLY in handle_message -- see
   _maybe_handle_followup_reply_step in app/conversation/__init__.py, called before language
   auto-detect/safety triage/NLU even run, NOT via the late STEP_REGISTRY dispatch. This
   matters: if it were only wired through STEP_REGISTRY, a message NLU-classifies as
   book_appointment/describe_symptom would already have been claimed by
   _maybe_handle_global_nlu_intent long before step-dispatch is ever reached -- the exact
   same bug via a different door. Intercepting here, before any NLU call, closes it for real.

Same cross-reference convention as every other sibling file (checkin.py, appointment_actions.py):
db/whatsapp_client/safety/nlu_client and the core-orchestration functions
(_advance_booking_flow/_start/_transition_to) are looked up via a function-body-local
`from app import conversation`, never a module-level import -- see checkin.py's own docstring
for the full reasoning (tests monkeypatch conversation.db/whatsapp_client by whole-name
reassignment, and a module-level import here would capture a stale reference + risk a
circular-import ordering hazard).
"""
import logging
import re

from app.decision_maker import booking_slots
from app.conversation.shared import _match_choice
from app.i18n import t

logger = logging.getLogger("conversation")

# Explicit "yes, book a follow-up" -- checked first, since an unambiguous request should
# always win over any implied sentiment in the same message.
_WANTS_BOOKING_PATTERNS = [
    "yes", "yeah", "sure", "book", "booking",
    "haan", "han", "chahiye", "karna hai", "karwana hai", "karvana hai",
    "हाँ", "हां", "चाहिए", "बुक",
    "হ্যাঁ", "বুক",
]

# Direct discomfort words -- unambiguous on their own, no negation-order sensitivity needed.
_FEELING_UNWELL_DIRECT_PATTERNS = [
    "kharab", "kharaab", "problem", "dard", "pain", "issue", "takleef", "taklif", "bura",
    "खराब", "दर्द", "समस्या", "तकलीफ", "बुरा",
    "খারাপ", "ব্যথা", "সমস্যা",
]

# "Feeling fine/good/better" words -- also reused by _negated_wellness below to catch the
# negated form ("not fine") regardless of word order.
_FEELING_WELL_PATTERNS = [
    "theek", "thik", "teek", "accha", "acha", "achha", "fine", "good", "better", "well", "ok", "okay", "great",
    "ठीक", "अच्छा", "बेहतर",
    "ভালো", "ঠিক",
]

_NEGATION_WORDS = ["nahi", "nhi", "not", "नहीं", "না"]

# Casual Hindi/Hinglish word order isn't fixed -- "accha nahi" and "nahi accha" both mean
# "not good". Checking within a small character window (rather than an exact phrase) catches
# both orders instead of just one.
_NEGATION_PROXIMITY_CHARS = 25


def _keyword_positions(text: str, keywords: list[str]) -> list[int]:
    positions = []
    for kw in keywords:
        if kw.isascii():
            for m in re.finditer(r"\b" + re.escape(kw) + r"\b", text):
                positions.append(m.start())
        else:
            start = 0
            while (idx := text.find(kw, start)) != -1:
                positions.append(idx)
                start = idx + 1
    return positions


def _matches_any(text: str, patterns: list[str]) -> bool:
    return bool(_keyword_positions(text, patterns))


def _negated_wellness(text: str) -> bool:
    """True if a negation word and a "feeling well" word both appear near each other, in
    EITHER order -- "accha nahi" and "nahi accha" must both be caught."""
    negation_positions = _keyword_positions(text, _NEGATION_WORDS)
    if not negation_positions:
        return False
    wellness_positions = _keyword_positions(text, _FEELING_WELL_PATTERNS)
    return any(abs(a - b) <= _NEGATION_PROXIMITY_CHARS for a in negation_positions for b in wellness_positions)


async def handle_awaiting_followup_reply(client, phone: str, input_type: str, input_value: str, context: dict) -> None:
    from app import conversation

    lang = context.get("lang")

    if input_type != "text" or not input_value.strip():
        await conversation.whatsapp_client.send_text(client, phone, t("followup_reply_hint", lang))
        return

    text = input_value.strip().lower()

    # Safety first -- reuses the exact same emergency-triage logic the main flow uses, so a
    # genuinely alarming reply here ("chest pain") gets the real emergency response, not a
    # generic "sorry to hear that, want a follow-up?" offer.
    safety_alert = conversation.safety.check_safety_triage(text, lang or "en")
    if safety_alert and safety_alert.get("is_emergency"):
        await conversation.whatsapp_client.send_text(client, phone, safety_alert["alert_message"])
        return

    if _matches_any(text, _WANTS_BOOKING_PATTERNS):
        await _start_followup_booking(client, phone, lang, context)
        return

    if _matches_any(text, _FEELING_UNWELL_DIRECT_PATTERNS) or _negated_wellness(text):
        await conversation._transition_to(phone, "confirming_followup_booking", context, "awaiting_followup_reply")
        await _prompt_confirming_followup_booking(client, phone, context)
        return

    if _matches_any(text, _FEELING_WELL_PATTERNS):
        await conversation.whatsapp_client.send_text(client, phone, t("followup_ack_feeling_well", lang))
        await conversation.db.clear_conversation_state(phone)
        return

    # Genuinely unclear -- same casual-chat LLM the main flow's own off-topic fallback uses.
    # Deliberately does NOT transition or clear state, so the patient stays on THIS step and
    # a later message is re-checked against the buckets above, instead of ever drifting into
    # symptom/specialty matching -- the original bug this file exists to fix.
    try:
        dynamic_reply = await conversation.nlu_client.generate_conversational_response(
            client, "general_chat", context, input_value
        )
        await conversation.whatsapp_client.send_text(client, phone, dynamic_reply or t("error_nlu_fallback", lang))
    except Exception as exc:
        logger.warning("Follow-up casual chat generation failed: %s", exc)
        await conversation.whatsapp_client.send_text(client, phone, t("error_nlu_fallback", lang))


async def _prompt_confirming_followup_booking(client, phone: str, context: dict) -> None:
    from app import conversation

    lang = context.get("lang")
    await conversation.whatsapp_client.send_buttons(
        client, phone,
        t("followup_feeling_unwell_offer", lang),
        [
            ("followup_book", t("followup_book_btn", lang)),
            ("followup_no_thanks", t("followup_no_thanks_btn", lang)),
        ],
    )


async def handle_confirming_followup_booking(client, phone: str, input_type: str, input_value: str, context: dict) -> None:
    from app import conversation

    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["followup_book", "followup_no_thanks"])
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("confirm_choose_hint", lang))
        return
    if choice == "followup_no_thanks":
        await conversation.whatsapp_client.send_text(client, phone, t("followup_no_thanks_ack", lang))
        await conversation.db.clear_conversation_state(phone)
        return
    await _start_followup_booking(client, phone, lang, context)


async def _start_followup_booking(client, phone: str, lang: str | None, context: dict) -> None:
    from app import conversation

    if not lang:
        # preferred_language was somehow unset on the appointment record -- fall back to
        # asking, same as any other never-yet-known-language conversation.
        await conversation._start(client, phone)
        return
    booking = booking_slots.empty()
    booking_slots.fill(booking, "lang", lang, source="user")
    new_context = {"lang": lang, "booking": booking, "session_id": context.get("session_id")}
    await conversation._advance_booking_flow(client, phone, new_context, booking)
