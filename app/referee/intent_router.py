import json
import logging
import time
import re
from app.messengers.redis_client import get_redis
from app import db, nlu_client
from app.decision_maker.normalizer import normalize_datetime_to_date
from app.config import settings

logger = logging.getLogger("intent_router")

from app.nlu_config import INTENT_REGISTRY

REQUIRED_ENTITIES = {
    k: v["required_entities"]
    for k, v in INTENT_REGISTRY.items()
    if v["required_entities"] or k == "cancel_appointment"
}

# Per-language follow-up copy. Keyed the same way as app/i18n.py (en/hi/hg/bn) so this stays
# consistent with the rest of the bot's language handling — previously these prompts were
# hardcoded Hinglish regardless of the patient's chosen language.
_DOCTOR_OR_SPECIALTY_PROMPT = {
    "en": "Which doctor would you like to see, or which specialty do you need? (e.g. Dr. Avinash or Gynaecologist)",
    "hi": "आप किस डॉक्टर से मिलना चाहते हैं या किस विशेषज्ञता के लिए परामर्श चाहते हैं? (जैसे: डॉ. अविनाश या स्त्री रोग विशेषज्ञ)",
    "hg": "Aap kis doctor se milna chahte hain ya kis specialty ke liye consult karna chahte hain? (Jaise: Dr. Avinash ya Gynaecologist)",
    "bn": "আপনি কোন ডাক্তারের সাথে দেখা করতে চান বা কোন বিশেষত্বের জন্য পরামর্শ করতে চান? (যেমন: ডঃ অবিনাশ বা স্ত্রীরোগ বিশেষজ্ঞ)",
}
_DATETIME_PROMPT = {
    "en": "When would you like the appointment? Please share a date (e.g. today, tomorrow, or a specific date).",
    "hi": "आप अपॉइंटमेंट कब बुक करना चाहते हैं? कृपया तारीख बताएं (जैसे: आज, कल या कोई खास तारीख)।",
    "hg": "Aap appointment kab ki book karna chahte hain? Kripya date batayein (jaise: aaj, kal, parso ya koi specific date).",
    "bn": "আপনি কখন অ্যাপয়েন্টমেন্ট বুক করতে চান? অনুগ্রহ করে তারিখ জানান (যেমন: আজ, আগামীকাল বা নির্দিষ্ট তারিখ)।",
}
_NEW_DOCTOR_PROMPT = {
    "en": "Which new doctor would you like to select? Please tell us their name.",
    "hi": "आप किस नए डॉक्टर को चुनना चाहते हैं? कृपया उनका नाम बताएं।",
    "hg": "Aap kis naye doctor ko select karna chahte hain? Kripya unka naam batayein.",
    "bn": "আপনি কোন নতুন ডাক্তার নির্বাচন করতে চান? অনুগ্রহ করে তাদের নাম বলুন।",
}
_GENERIC_SLOT_PROMPT = {
    "en": "Could you tell me more about {slot}?",
    "hi": "कृपया {slot} के बारे में बताएं।",
    "hg": "Kripya {slot} ke baare mein batayein.",
    "bn": "অনুগ্রহ করে {slot} সম্পর্কে বলুন।",
}
_RESCHEDULE_CLARIFY_PROMPT = {
    "en": "You already have an appointment on {date}. Would you like to book a new one, or reschedule it?",
    "hi": "आपकी पहले से ही {date} को एक अपॉइंटमेंट है। क्या आप नई बुक करना चाहते हैं या इसे रीशेड्यूल करना चाहते हैं?",
    "hg": "Aapki already ek appointment hai {date}. Naya book karna hai ya usko reschedule karna hai?",
    "bn": "আপনার ইতিমধ্যে {date} তারিখে একটি অ্যাপয়েন্টমেন্ট আছে। আপনি কি নতুন বুক করতে চান নাকি এটি পুনঃনির্ধারণ করতে চান?",
}


def _pick(prompt_map: dict, lang: str | None) -> str:
    return prompt_map.get(lang or "hg", prompt_map["hg"])

class RoutedResult:
    def __init__(
        self, action: str, intent: str, entities: dict, followup_prompt: str | None = None,
        confidence: float = 0.9,
    ):
        self.action = action  # "ask_followup" or "proceed_to_business_logic"
        self.intent = intent
        self.entities = entities
        self.followup_prompt = followup_prompt
        # Only meaningful when action == "proceed_to_business_logic" — see the confidence
        # computation at the bottom of route_intent() for why this isn't just a passthrough
        # of the current turn's raw NLU confidence.
        self.confidence = confidence

    def __repr__(self):
        return f"<RoutedResult action={self.action} intent={self.intent} entities={self.entities} confidence={self.confidence}>"


_CONFIDENCE_SCORE = {"high": 0.9, "medium": 0.75, "low": 0.3}

# Intents with no REQUIRED_ENTITIES entry (cancel_appointment maps to an empty list;
# navigate_back and greeting aren't in the map at all) skip the missing-slot check
# entirely and always reach "proceed_to_business_logic" on the very message that named
# them — there is no slot-filling safety net verifying the classification is right.
# These are exactly the intents where the raw NLU confidence must actually gate
# execution, since misreading e.g. "nahi... cancel jaisa kuch nahi bola" as
# cancel_appointment would otherwise wipe a booking on a single low-confidence guess.
_NO_SLOT_SAFETY_NET = {
    k for k, v in INTENT_REGISTRY.items() if not v["has_slot_safety_net"] and k != "out_of_scope"
}


def _proceed_confidence(intent: str, current_turn_confidence: str | None) -> float:
    """Confidence to report when action="proceed_to_business_logic".

    For intents backed by a REQUIRED_ENTITIES check (book_appointment, check_availability,
    change_selection, ask_pricing, reschedule_appointment), reaching this point already
    means every required slot was filled and validated across however many turns it took —
    that IS the trust signal, not the raw confidence of whichever single message happened to
    fill the last slot. A multi-turn booking where turn 3 is just "kal" (a low-confidence
    fragment on its own) must not be blocked here; see test_multi_turn_slot_filling in
    test_nlu_integration.py.

    For _NO_SLOT_SAFETY_NET intents, there is no such multi-turn validation, so the current
    turn's own confidence is what gets reported."""
    if intent in _NO_SLOT_SAFETY_NET:
        return _CONFIDENCE_SCORE.get(current_turn_confidence, _CONFIDENCE_SCORE["low"])
    return _CONFIDENCE_SCORE["high"]


def get_followup_prompt(intent: str, missing_slot, lang: str | None = None) -> str:
    """Returns a friendly conversational question for a missing slot, in the patient's
    chosen language (falls back to Hinglish if lang is unknown, matching this bot's
    established default tone — see app/i18n.py)."""
    if isinstance(missing_slot, (tuple, list)):
        if "doctor_name" in missing_slot or "specialty" in missing_slot:
            return _pick(_DOCTOR_OR_SPECIALTY_PROMPT, lang)

    if missing_slot == "datetime":
        return _pick(_DATETIME_PROMPT, lang)

    if missing_slot == "new_doctor_name":
        return _pick(_NEW_DOCTOR_PROMPT, lang)

    return _pick(_GENERIC_SLOT_PROMPT, lang).format(slot=missing_slot)


async def clear_session(wa_id: str) -> None:
    """Drops any accumulated multi-turn state (including an awaiting_clarification
    follow-up) for this patient. Called by conversation.py, via app/flow_policy.py,
    right before route_intent() would otherwise consult that state -- see
    flow_policy.py's module docstring for why a global intent (cancel/back/greeting)
    must never be swallowed by a stale or in-progress local session here."""
    redis = get_redis()
    await redis.delete(f"nlu:session:{wa_id}")


async def route_intent(
    wa_id: str, validated_nlu_result: dict, raw_text: str = "", lang: str | None = None,
    current_step: str | None = None,
) -> RoutedResult:
    redis = get_redis()
    redis_key = f"nlu:session:{wa_id}"

    # 1. Load existing session state
    stored_state = None
    stored_str = await redis.get(redis_key)
    if stored_str:
        try:
            stored_state = json.loads(stored_str)
        except Exception as e:
            logger.error("Failed to parse NLU session state: %s", e)

    # This Redis session and the SQL conversation_state (app/db.py) are two independent
    # stores of "what's going on" — nothing keeps them in sync. A follow-up question saved
    # here doesn't move conversation_state, so a button/list tap (which never reaches this
    # function at all — classify_message only runs on input_type=="text") can let the SQL
    # step machine move on to a completely different step while this session sits untouched
    # until its 15-minute TTL. A LATER unrelated low-confidence message can then merge with
    # that stale entry and hijack a turn meant for whatever step the user has since reached
    # (see the phase-3-fix-4 discussion — this bug was traced concretely, not assumed).
    #
    # step_at_save is this session's fingerprint of the SQL step it was created under. If
    # the step has since moved on without this function's involvement, the session no
    # longer describes "what's going on" and must not be merged into this turn.
    if stored_state and stored_state.get("step_at_save") != current_step:
        logger.info(
            "Discarding stale NLU session for %s: saved under step=%r, now at step=%r",
            wa_id, stored_state.get("step_at_save"), current_step,
        )
        stored_state = None
        await redis.delete(redis_key)

    new_intent = validated_nlu_result.get("intent", "out_of_scope")
    new_confidence = validated_nlu_result.get("confidence", "low")
    new_entities = validated_nlu_result.get("entities", {}) or {}

    # A short, context-free reply to the router's own "when would you like the appointment?"
    # follow-up (e.g. just "today") routinely gets classified out_of_scope with empty
    # entities -- Sarvam has no way to know a bare date word answers a question it never saw,
    # since each message is classified independently. That leaves nothing for the merge below
    # to pick up, and the same follow-up prompt gets sent again forever (live-reported: "hi, i
    # have to book appointment with Dr, Radha" -> asked for a date -> "today" -> asked again).
    # normalize_datetime_to_date only matches a short, unambiguous set of literal date
    # phrasings (today/kal/parso/a real date/etc.), so running it unconditionally on the raw
    # text is safe -- it won't fire on unrelated text, it just recovers what the model missed.
    if "datetime" not in new_entities:
        fallback_date = normalize_datetime_to_date(raw_text)
        if fallback_date:
            new_entities = {**new_entities, "datetime": fallback_date}

    score = _CONFIDENCE_SCORE.get(new_confidence, 0.3)
    if score < settings.nlu_confidence_threshold:
        intent_to_check = new_intent
        if stored_state and (not new_intent or new_intent == "out_of_scope"):
            intent_to_check = stored_state.get("intent")
        
        merged_entities = {**(stored_state.get("entities", {}) if stored_state else {}), **new_entities}
        requirements = REQUIRED_ENTITIES.get(intent_to_check, [])
        slots_filled = True
        for req in requirements:
            if isinstance(req, (tuple, list)):
                if not any(merged_entities.get(opt) for opt in req):
                    slots_filled = False
                    break
            else:
                if not merged_entities.get(req):
                    slots_filled = False
                    break
                    
        if not slots_filled:
            logger.info(
                "NLU result confidence %s (%s) is below threshold %s and slots are not fully filled. Rejecting entities %r to prevent slot pollution.",
                new_confidence, score, settings.nlu_confidence_threshold, new_entities
            )
            new_entities = {}
            if new_intent not in _NO_SLOT_SAFETY_NET:
                new_intent = "out_of_scope"

    current_intent = new_intent
    current_entities = new_entities
    awaiting_clarification = False
    resolved_clarification_this_turn = False

    # 2. Check if we have an existing state to merge/resolve
    if stored_state:
        stored_intent = stored_state.get("intent")
        stored_entities = stored_state.get("entities", {}) or {}
        awaiting_clarification = stored_state.get("awaiting_clarification", False)

        if awaiting_clarification:
            # The user was asked to clarify "Naya book karna hai ya reschedule"
            raw_normalized = raw_text.lower().strip()
            # Simple keyword matching for clarification
            is_reschedule = any(k in raw_normalized for k in ["reschedule", "change", "shift", "badal", "parso", "kal", "time"]) or new_intent == "reschedule_appointment"
            is_new_booking = any(k in raw_normalized for k in ["new", "naya", "dusra", "another", "fresh", "book"])

            if is_reschedule:
                current_intent = "reschedule_appointment"
                current_entities = {**stored_entities, **new_entities}
                awaiting_clarification = False
            elif is_new_booking:
                current_intent = "book_appointment"
                current_entities = stored_entities  # keep slots, drop clarification
                awaiting_clarification = False
                resolved_clarification_this_turn = True
            else:
                # Keep asking
                active_date = stored_state.get("active_appt_date", "")
                return RoutedResult(
                    action="ask_followup",
                    intent=stored_intent,
                    entities=stored_entities,
                    followup_prompt=_pick(_RESCHEDULE_CLARIFY_PROMPT, lang).format(date=active_date)
                )
        else:
            # General state merging:
            # Switch to new intent if new intent is a distinct high/medium confidence intent (not out_of_scope)
            if new_intent and new_intent != "out_of_scope" and new_intent != stored_intent and new_confidence in ("high", "medium"):
                current_intent = new_intent
                current_entities = new_entities
            else:
                current_intent = stored_intent
                current_entities = {**stored_entities, **new_entities}

    # 3. Check for required entities
    requirements = REQUIRED_ENTITIES.get(current_intent, [])
    missing_slot = None
    for req in requirements:
        if isinstance(req, (tuple, list)):
            # Check if any of the elements in the tuple is present
            if not any(current_entities.get(opt) for opt in req):
                missing_slot = req
                break
        else:
            if not current_entities.get(req):
                missing_slot = req
                break

    if missing_slot:
        # Save updated state and ask follow up
        state_to_save = {
            "intent": current_intent,
            "entities": current_entities,
            "awaiting_clarification": False,
            "step_at_save": current_step,
            "updated_at": time.time()
        }
        await redis.set(redis_key, json.dumps(state_to_save), ex=900)  # 15 minutes TTL

        prompt = get_followup_prompt(current_intent, missing_slot, lang)
        return RoutedResult(
            action="ask_followup",
            intent=current_intent,
            entities=current_entities,
            followup_prompt=prompt
        )

    # 4. Check for active upcoming appointment conflict (Only if all slots are present for book_appointment)
    if current_intent == "book_appointment" and not awaiting_clarification and not resolved_clarification_this_turn:
        # Check active booked/pending appointments for this user in the DB
        has_active = False
        active_date_str = None
        try:
            has_active, active_date_str = await db.get_upcoming_active_appointment(wa_id)
        except Exception as exc:
            logger.error("DB check for active appointments failed: %s", exc)

        if has_active:
            # Persist clarification state and ask clarifying question
            state_to_save = {
                "intent": "book_appointment",
                "entities": current_entities,
                "awaiting_clarification": True,
                "active_appt_date": active_date_str,
                "step_at_save": current_step,
                "updated_at": time.time()
            }
            await redis.set(redis_key, json.dumps(state_to_save), ex=900)

            prompt = _pick(_RESCHEDULE_CLARIFY_PROMPT, lang).format(date=active_date_str)
            return RoutedResult(
                action="ask_followup",
                intent=current_intent,
                entities=current_entities,
                followup_prompt=prompt
            )

    # 5. Success! Clear session state and proceed to business logic
    await redis.delete(redis_key)
    return RoutedResult(
        action="proceed_to_business_logic",
        intent=current_intent,
        entities=current_entities,
        confidence=_proceed_confidence(current_intent, new_confidence),
    )
