import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import booking_slots, city_index, db, hms_client, i18n, symptom_client, whatsapp_client
from app.config import settings
from app.geo import haversine_km
from app.hms_client import HmsApiError
from app import nlu_client, intent_router
from app.model_config import PRIMARY_NLU
from app.i18n import LANGUAGE_LABELS, LANG_PROMPT, t

logger = logging.getLogger("conversation")

_SHIFT_FALLBACK = ["Morning", "Afternoon", "Evening"]

# Kept for anything importing the old constant name (e.g. tests) — superseded by
# i18n.LANG_PROMPT as the very first message now, since language is asked before anything
# else. See _start() below.
GREETING_TEXT = "Hi! I can help you book a doctor's appointment."

_SORT_OPTIONS = ["rating", "nearest", "experience", "fee"]


def _match_choice(input_type: str, input_value: str, valid_ids: list[str]) -> str | None:
    """Accepts a button/list tap, or plain text typed by hand matching one of the choices —
    interactive messages can scroll out of easy reach, typing 'confirm' should still work."""
    if input_type in ("button_reply", "list_reply") and input_value in valid_ids:
        return input_value
    if input_type == "text":
        normalized = input_value.strip().lower()
        for valid in valid_ids:
            if normalized == valid.lower():
                return valid
    return None


def _parse_details(text: str, expected: int) -> list[str] | None:
    """'Riya, 8, Daughter' -> ['Riya', '8', 'Daughter'] when expected=3.

    Deliberately lenient about the age: free text typed on a phone keyboard will have
    inconsistent spacing and casing, and someone may well write "8 yrs" or "8 saal". The
    only hard requirement is the right number of non-empty comma-separated parts. Age is
    sanity-checked separately by _looks_like_age rather than parsed strictly here."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != expected or not all(parts):
        return None
    return parts


def _looks_like_age(value: str) -> bool:
    """Catches a swapped 'Age, Name' or a stray phone number before it reaches the record.
    Accepts '8', '8 yrs', '8 saal' — anything whose leading digits land in 0-120."""
    digits = ""
    for char in value.strip():
        if char.isdigit():
            digits += char
        elif digits:
            break
    return bool(digits) and 0 < int(digits) <= 120


def _detect_language(text: str) -> str | None:
    if not text:
        return None

    # Normalize: lowercase, strip, and strip common punctuation
    normalized = text.strip().lower()
    normalized = re.sub(r'[^\w\s\u0900-\u097F\u0980-\u09FF]', '', normalized)

    # Generic greetings: return None to present open language selection prompt
    greetings = {"hi", "hello", "hey", "hola", "namaste", "pranam", "helo", "hlo"}
    if normalized in greetings:
        return None

    # Devanagari Hindi check
    if re.search(r'[\u0900-\u097F]', text):
        return "hi"

    # Bengali check
    if re.search(r'[\u0980-\u09FF]', text):
        return "bn"

    # Hinglish keywords/typos
    hinglish_keywords = {
        "mujhe", "muje", "mjhe", "mjh", "mje",
        "chahiye", "chahie", "cahiye", "chaye", "chaiye", "chahye",
        "karna", "krna", "karana", "krne", "karne", "krni", "karni", "karo", "kro",
        "hai", "bhejo", "dikhao", "dikho", "dikhayein", "dikhaye",
        "parcha", "parchi", "pacha", "dawa", "dawae", "dawai", "dawo", "hona"
    }

    # English keywords/typos
    english_keywords = {
        "book", "bok", "boke", "buk",
        "appointment", "apointment", "apointmint", "apointmet", "apointmnt", "apointement", "appontment", "appoiment", "appoinment",
        "doctor", "doctur", "doc", "dr", "docter", "dctr",
        "find", "show", "search", "prescription", "prescribtion", "prescrip", "download", "downlod", "dawnload",
        "rx", "medicine", "list", "get"
    }

    # Benglish (romanized Bengali) keywords/typos
    benglish_keywords = {
        "amar", "amr", "amake",
        "lagbe", "lagba", "lagbo",
        "chai", "chay", "dorkar", "drkar", "proyojon",
        "daktar", "daktarer", "dekhate", "dekhabo", "dekha",
        "korte", "korbo", "korate", "krte", "ashish", "ashis",
        "oushodh", "oushadh", "oshudh", "oshud", "osudh", "osud"
    }

    words = re.findall(r'\b\w+\b', normalized)
    if not words:
        return None

    # Calculate scores: unambiguous keywords (Hinglish/Benglish) get higher weight
    # than English keywords, which are frequently used as loanwords in other languages.
    hi_score = sum(2 for w in words if w in hinglish_keywords)
    bn_score = sum(2 for w in words if w in benglish_keywords)
    en_score = sum(1 for w in words if w in english_keywords)

    if hi_score == 0 and bn_score == 0 and en_score == 0:
        return None

    max_score = max(hi_score, bn_score, en_score)

    if hi_score == max_score and hi_score > 0 and hi_score > bn_score:
        return "hg"
    elif bn_score == max_score and bn_score > 0 and bn_score > hi_score:
        return "bn"
    elif en_score == max_score and en_score > hi_score and en_score > bn_score:
        return "en"
    elif hi_score == en_score and hi_score > bn_score and hi_score > 0:
        return "hg"
    elif bn_score == en_score and bn_score > hi_score and bn_score > 0:
        return "bn"
    else:
        return None

    return None


# ---------------------------------------------------------------------------------------
# SHADOW MODE — temporary, remove together with the current_step dispatch below.
#
# Builds a booking_slots clipboard from the live conversation context and logs what it
# WOULD have asked for next, alongside what the step machine actually did. Nothing here
# affects a reply: it is a read of context that already exists, so the two can be compared
# on real traffic before the clipboard is given control.
# ---------------------------------------------------------------------------------------

# Which current_step values are an acceptable realisation of a given clipboard action.
# Sets, not single values, because the existing flow spreads one decision over several
# steps: six different steps all exist to narrow down to a doctor, and the slot picker
# asks for date and shift together in one message.
_SHADOW_EXPECTED_STEPS = {
    ("ask", "lang"): {"choosing_language", None},
    ("disambiguate", "lang"): {"confirming_language"},
    ("ask", "location"): {"choosing_location"},
    ("disambiguate", "location"): {"choosing_location"},
    ("ask", "doctor"): {
        "choosing_search_mode", "awaiting_symptom", "awaiting_doctor_name",
        "choosing_specialty_group", "choosing_specialty", "choosing_sort",
        "confirming_wider_search", "choosing_doctor",
    },
    ("disambiguate", "doctor"): {"choosing_doctor"},
    ("ask", "date"): {"choosing_slot"},
    ("ask", "shift"): {"choosing_slot"},
    ("ask", "patient"): {"awaiting_patient_details"},
    ("confirm", None): {"confirming"},
}


def _shadow_clipboard(context: dict) -> dict:
    """Legacy context dict -> booking_slots clipboard. Read-only; never mutates context."""
    slots = booking_slots.empty()

    if context.get("lang"):
        booking_slots.fill(slots, "lang", context["lang"], source="legacy")

    if context.get("patient_lat") is not None and context.get("patient_lng") is not None:
        booking_slots.fill(
            slots, "location",
            {"lat": context["patient_lat"], "lng": context["patient_lng"], "city": context.get("city")},
            raw=context.get("location_text"), source="legacy",
        )
    elif context.get("city"):
        booking_slots.fill(slots, "location", context["city"], raw=context.get("location_text"), source="legacy")

    if context.get("doctor_id"):
        booking_slots.fill(
            slots, "doctor",
            {"id": context["doctor_id"], "fullName": context.get("doctor_name")},
            raw=context.get("search_doctor_query"), source="legacy",
        )
    elif context.get("doctor_options"):
        booking_slots.mark_ambiguous(
            slots, "doctor", context["doctor_options"], raw=context.get("search_doctor_query")
        )

    if context.get("preferred_date"):
        booking_slots.fill(slots, "date", context["preferred_date"], source="legacy")
    if context.get("shift_label"):
        booking_slots.fill(slots, "shift", context["shift_label"], source="legacy")

    if context.get("patient_display_name"):
        booking_slots.fill(
            slots, "patient",
            {
                "name": context["patient_display_name"],
                "age": context.get("patient_age"),
                "gender": context.get("patient_gender"),
                "guardian": context.get("patient_guardian"),
            },
            source="legacy",
        )

    return slots


def _log_shadow(phone: str, current_step: str | None, context: dict) -> None:
    """Never raises — a shadow-comparison bug must not break a live conversation."""
    try:
        slots = _shadow_clipboard(context)
        action = booking_slots.next_action(slots)
        expected = _SHADOW_EXPECTED_STEPS.get(action, set())
        agrees = current_step in expected
        logger.info(
            "SHADOW phone=%s step=%s clipboard=%s known=%s agrees=%s",
            phone, current_step, action, sorted(booking_slots.known_summary(slots)), agrees,
        )
    except Exception:
        logger.exception("SHADOW comparison failed for %s (ignored)", phone)


async def handle_message(
    client: httpx.AsyncClient,
    phone: str,
    sender_name: str | None,
    input_type: str,
    input_value: str,
    message_id: str | None = None,
) -> None:
    if message_id:
        try:
            await whatsapp_client.send_typing_indicator(client, message_id)
        except Exception:
            # Best-effort UX polish — never let a typing-indicator failure block the
            # actual reply the patient is waiting on.
            logger.warning("Failed to send typing indicator for %s", message_id)

    state = await db.get_conversation_state(phone)
    current_step = state["current_step"] if state else None
    context = state["context"] if state else {}
    _log_shadow(phone, current_step, context)  # SHADOW MODE — remove in phase 3
    lang = context.get("lang")
    has_lang_init = lang is not None
    if input_type == "text" and input_value.strip() and has_lang_init:
        detected_lang = _detect_language(input_value)
        if detected_lang and detected_lang != lang:
            logger.info("Auto-swapping language from %s to %s for user %s", lang, detected_lang, phone)
            lang = detected_lang
            context["lang"] = lang
            if current_step:
                await db.save_conversation_state(phone, current_step, context)

    nlu_result = None
    if input_type == "text" and input_value.strip() and has_lang_init and lang:
        try:
            # 1. Classify message using the new NLU client
            raw_nlu_result = await nlu_client.classify_message(client, input_value)
            logger.info("NLU Result: %s", raw_nlu_result)
            
            # Log the raw interaction to the database
            if hasattr(db, "log_nlu_interaction"):
                brain_name = PRIMARY_NLU["model"]
                if raw_nlu_result.get("intent") == "out_of_scope" and raw_nlu_result.get("confidence") == "low":
                    brain_name = "nlu_hard_fallback"
                
                await db.log_nlu_interaction(
                    phone=phone,
                    session_id=context.get("session_id"),
                    utterance=input_value,
                    nlu_brain=brain_name,
                    intent=raw_nlu_result.get("intent"),
                    confidence=0.9 if raw_nlu_result.get("confidence") in ("high", "medium") else 0.2,
                    doctor_name=raw_nlu_result.get("entities", {}).get("doctor_name"),
                    specialty=raw_nlu_result.get("entities", {}).get("specialty"),
                    symptom=raw_nlu_result.get("entities", {}).get("symptom"),
                    formatted_date=raw_nlu_result.get("entities", {}).get("datetime"),
                    routed_step=current_step
                )
            
            # 2. Route intent using intent_router (slot filling, session merge, duplicate booking check)
            routed = await intent_router.route_intent(phone, raw_nlu_result, input_value, lang)
            logger.info("NLU Router Result: %s", routed)
            
            if routed.action == "ask_followup":
                if routed.intent == "change_selection" and current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
                    await _transition_to(phone, "awaiting_doctor_name", context, current_step)
                    await whatsapp_client.send_text(client, phone, t("doctor_name_ask", lang or "en"))
                else:
                    await whatsapp_client.send_text(client, phone, routed.followup_prompt)
                return
            
            # 3. Flatten NLU result so downstream business logic remains completely untouched
            nlu_result = {
                "intent": routed.intent,
                "confidence": 0.9,
                "doctor_name": routed.entities.get("doctor_name") or routed.entities.get("new_doctor_name"),
                "specialty": routed.entities.get("specialty"),
                "symptom": routed.entities.get("symptom"),
                "formatted_date": routed.entities.get("datetime")
            }
        except Exception as exc:
            logger.warning("NLU client parsing or routing failed: %s", exc)

    # If a doctor is mentioned anywhere in the text while in selection states, hot-swap immediately
    if nlu_result and nlu_result.get("doctor_name"):
        doc_name = nlu_result["doctor_name"]
        if current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
            context["search_doctor_query"] = doc_name
            await _transition_to(phone, "awaiting_doctor_name", context, current_step)
            if await _search_doctors_flow(client, phone, context, "awaiting_doctor_name"):
                return
            else:
                await whatsapp_client.send_text(client, phone, t("search_doctor_not_found", context.get("lang"), query=doc_name))
                return

    # Prioritize NLU global intents / shortcuts if confidence is high
    if nlu_result and nlu_result.get("confidence", 0.0) >= 0.7:
        intent = nlu_result["intent"]
        
        if intent == "cancel_appointment":
            await whatsapp_client.send_text(client, phone, t("cancelled", lang or "en"))
            await db.clear_conversation_state(phone)
            return
            
        elif intent == "navigate_back":
            history = context.get("_history", [])
            if not history:
                await whatsapp_client.send_text(client, phone, t("back_no_history", lang))
                await db.clear_conversation_state(phone)
                await _start(client, phone)
                return
            prev = history.pop()
            prev_step = prev["current_step"]
            prev_context = prev["context"]
            prev_context["_history"] = history
            await db.save_conversation_state(phone, prev_step, prev_context)
            await _trigger_step_prompt(client, phone, prev_step, prev_context)
            return
            
        elif intent == "greeting":
            await db.clear_conversation_state(phone)
            await _start(client, phone)
            return
            
        elif intent in ("book_appointment", "check_availability"):
            doc_name = nlu_result.get("doctor_name")
            spec_name = nlu_result.get("specialty")
            sym_name = nlu_result.get("symptom")
            pref_date = nlu_result.get("formatted_date")
            
            new_context = {**context}
            if pref_date:
                new_context["preferred_date"] = pref_date
                
            if doc_name:
                new_context["search_doctor_query"] = doc_name
                new_context.pop("pending_specialty", None)
                new_context.pop("search_symptom", None)
                
                has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                if new_context.get("lang") and has_loc:
                    await _transition_to(phone, "awaiting_doctor_name", new_context, current_step)
                    if await _search_doctors_flow(client, phone, new_context, "awaiting_doctor_name"):
                        return
                    else:
                        await whatsapp_client.send_text(client, phone, t("search_doctor_not_found", new_context.get("lang"), query=doc_name))
                        return
                else:
                    if not new_context.get("lang"):
                        await _start(client, phone, new_context)
                    else:
                        await _transition_to(phone, "choosing_location", new_context, current_step)
                        await _trigger_step_prompt(client, phone, "choosing_location", new_context)
                    return
            
            elif spec_name:
                categories = await hms_client.list_specialties()
                category_list = [c["category"] for c in categories]
                matched = symptom_client.match_category(spec_name, category_list)
                if matched:
                    new_context["pending_specialty"] = matched
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(client, phone, new_context, matched, current_step)
                        return
                    else:
                        if not new_context.get("lang"):
                            await _start(client, phone, new_context)
                        else:
                            await _transition_to(phone, "choosing_location", new_context, current_step)
                            await _trigger_step_prompt(client, phone, "choosing_location", new_context)
                        return
            
            elif sym_name:
                labels = await symptom_client.route_symptom(sym_name)
                categories = await hms_client.list_specialties()
                category_list = [c["category"] for c in categories]
                matched = next(
                    (m for m in (symptom_client.match_category(label, category_list) for label in labels) if m),
                    None
                )
                if matched:
                    new_context["pending_specialty"] = matched
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(client, phone, new_context, matched, current_step)
                        return
                    else:
                        if not new_context.get("lang"):
                            await _start(client, phone, new_context)
                        else:
                            await _transition_to(phone, "choosing_location", new_context, current_step)
                            await _trigger_step_prompt(client, phone, "choosing_location", new_context)
                        return

        elif intent == "change_selection":
            doc_name = nlu_result.get("doctor_name")
            if doc_name and current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
                context["search_doctor_query"] = doc_name
                await _transition_to(phone, "awaiting_doctor_name", context, current_step)
                if await _search_doctors_flow(client, phone, context, "awaiting_doctor_name"):
                    return
                else:
                    await whatsapp_client.send_text(client, phone, t("search_doctor_not_found", context.get("lang"), query=doc_name))
                    return
            elif current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
                await _transition_to(phone, "awaiting_doctor_name", context, current_step)
                await whatsapp_client.send_text(client, phone, t("doctor_name_ask", context.get("lang")))
                return

    # Fallback to manual exact-match command parsing
    if input_type == "text" and input_value.strip():
        cmd = input_value.strip().lower()
        
        # Check for negation/correction words in user text to flag NLU incorrectness
        negations = {"no", "wrong", "incorrect", "galat", "nahi", "not", "false"}
        if any(re.search(r'\b' + n + r'\b', cmd) for n in negations):
            if hasattr(db, "update_last_nlu_log_correctness"):
                await db.update_last_nlu_log_correctness(phone, 0, "user_negation")
                
        if cmd in ("cancel", "quit"):
            if hasattr(db, "update_last_nlu_log_correctness"):
                await db.update_last_nlu_log_correctness(phone, 0, "cancel_command")
            await whatsapp_client.send_text(client, phone, t("cancelled", lang or "en"))
            await db.clear_conversation_state(phone)
            return
            
        if cmd == "back":
            if hasattr(db, "update_last_nlu_log_correctness"):
                await db.update_last_nlu_log_correctness(phone, 0, "back_navigation")
            history = context.get("_history", [])
            if not history:
                await whatsapp_client.send_text(client, phone, t("back_no_history", lang))
                await db.clear_conversation_state(phone)
                await _start(client, phone)
                return
            prev = history.pop()
            prev_step = prev["current_step"]
            prev_context = prev["context"]
            prev_context["_history"] = history
            await db.save_conversation_state(phone, prev_step, prev_context)
            await _trigger_step_prompt(client, phone, prev_step, prev_context)
            return

    # 1.5 Handle off-topic / out-of-scope casual conversation dynamically via LLM
    if input_type == "text" and input_value.strip() and lang:
        has_entities = nlu_result and any(nlu_result.get(k) for k in ("doctor_name", "specialty", "symptom"))
        if not nlu_result or nlu_result.get("intent") == "out_of_scope" or nlu_result.get("confidence", 0.0) < 0.7:
            if not has_entities:
                if current_step not in ("awaiting_symptom", "awaiting_doctor_name", "awaiting_patient_details"):
                    try:
                        dynamic_reply = await nlu_client.generate_conversational_response(client, "general_chat", context, input_value)
                        if dynamic_reply:
                            await whatsapp_client.send_text(client, phone, dynamic_reply)
                        else:
                            await whatsapp_client.send_text(client, phone, t("error_nlu_fallback", lang))
                    except Exception as exc:
                        logger.warning("Casual chat generation failed: %s", exc)
                        await whatsapp_client.send_text(client, phone, t("error_nlu_fallback", lang))
                    
                    if current_step:
                        await _trigger_step_prompt(client, phone, current_step, context)
                    else:
                        await _start(client, phone)
                    return

    try:
        if current_step == "choosing_language":
            await _handle_choosing_language(client, phone, input_type, input_value, context)
        elif current_step == "confirming_language":
            await _handle_confirming_language(client, phone, input_type, input_value, context)
        elif current_step == "choosing_location":
            await _handle_choosing_location(client, phone, input_type, input_value, context)
        elif current_step == "choosing_search_mode":
            await _handle_choosing_search_mode(client, phone, input_type, input_value, context)
        elif current_step == "awaiting_symptom":
            await _handle_awaiting_symptom(client, phone, input_type, input_value, context)
        elif current_step == "awaiting_doctor_name":
            await _handle_awaiting_doctor_name(client, phone, input_type, input_value, context)
        elif current_step == "choosing_specialty_group":
            await _handle_choosing_specialty_group(client, phone, input_type, input_value, context)
        elif current_step == "choosing_specialty":
            await _handle_choosing_specialty(client, phone, input_type, input_value, context)
        elif current_step == "choosing_sort":
            await _handle_choosing_sort(client, phone, input_type, input_value, context)
        elif current_step == "confirming_wider_search":
            await _handle_confirming_wider_search(client, phone, input_type, input_value, context)
        elif current_step == "choosing_doctor":
            await _handle_choosing_doctor(client, phone, input_type, input_value, context)
        elif current_step == "choosing_slot":
            await _handle_choosing_slot(client, phone, input_type, input_value, context)
        elif current_step == "awaiting_patient_details":
            await _handle_awaiting_patient_details(client, phone, input_type, input_value, context)
        elif current_step == "confirming":
            await _handle_confirming(client, phone, sender_name, input_type, input_value, context)
        else:
            # No state (new/returning user) or an unrecognized step — restart cleanly
            # rather than leave the conversation stuck.
            detected_lang = None
            if input_type == "text" and input_value.strip():
                detected_lang = _detect_language(input_value)

            if detected_lang:
                confirm_context = {
                    "lang": detected_lang,
                }
                if _is_doctor_search_query(input_value):
                    confirm_context["search_doctor_query"] = input_value
                
                await whatsapp_client.send_text(client, phone, t("welcome_banner", detected_lang))
                await _send_location_request(client, phone, confirm_context)
            else:
                init_context = {}
                if input_type == "text" and _is_doctor_search_query(input_value):
                    init_context["search_doctor_query"] = input_value
                await _start(client, phone, init_context)
    except HmsApiError as exc:
        logger.warning("HMS API rejected request for %s: %s", phone, exc)
        await whatsapp_client.send_text(client, phone, t("error_hms", lang))
    except httpx.HTTPError as exc:
        logger.warning("HMS API unreachable for %s: %s", phone, exc)
        await whatsapp_client.send_text(client, phone, t("error_hms_unreachable", lang))


# ---------------------------------------------------------------------------------------
# 1. Language selection — every fresh conversation starts here, before anything else,
# including the greeting itself (which is why GREETING_TEXT isn't used as-is any more —
# the greeting now comes as part of _handle_choosing_language's reply, once we know which
# language to greet in).
# ---------------------------------------------------------------------------------------


async def _start(client: httpx.AsyncClient, phone: str, init_context: dict | None = None) -> None:
    await whatsapp_client.send_text(client, phone, t("welcome_multilang", None))
    await whatsapp_client.send_list(
        client, phone, LANG_PROMPT, "Choose / चुनें",
        [(code, label) for code, label in LANGUAGE_LABELS.items()],
        "Languages",
    )
    from uuid import uuid4
    ctx = init_context or {}
    ctx["session_id"] = str(uuid4())
    await _transition_to(phone, "choosing_language", ctx, None)


async def _handle_choosing_language(client, phone, input_type, input_value, context) -> None:
    lang = _match_choice(input_type, input_value, list(LANGUAGE_LABELS.keys()))
    if lang is None:
        # Note: this hint is unavoidably English-only — we don't know the language yet,
        # that's exactly what's being asked.
        await whatsapp_client.send_text(client, phone, "Please tap one of the language options above.")
        return
    context = {**context, "lang": lang}
    await whatsapp_client.send_text(client, phone, t("greeting", lang))
    await _send_location_request(client, phone, context)


async def _handle_confirming_language(client, phone, input_type, input_value, context) -> None:
    guess_lang = context.get("guess_lang", "en")
    
    # Try direct button reply or exact match ID matching first
    choice = _match_choice(input_type, input_value, ["lang_confirm_yes", "lang_confirm_change"])
    
    # Fallback to colloquial text input matching
    if not choice and input_type == "text" and input_value.strip():
        val = input_value.strip().lower()
        # Yes indicators:
        yes_indicators = {
            "yes", "y", "haan", "ha", "haa", "confirm", "ok", "okay", "haji", "ji", "yes, continue", "continue",
            "হ্যাঁ", "হ্যা", "হ্যাঁ, চালিয়ে যান", "কন্টিনিউ", "হ্যাঁ কন্টিনিউ", "হ্যাঁ, চালিয়ে যাও"
        }
        # Change / No indicators:
        change_indicators = {
            "no", "change", "n", "no, change", "badlo", "badlein", "language change", "change language",
            "না", "না, পরিবর্তন করুন", "ভাষা পরিবর্তন", "ভাষা বদলান"
        }
        
        # Add dynamic localized button label strings to the sets
        for lang_code in LANGUAGE_LABELS.keys():
            yes_indicators.add(t("confirm_yes", lang_code).lower())
            change_indicators.add(t("confirm_change", lang_code).lower())
            
        if val in yes_indicators:
            choice = "lang_confirm_yes"
        elif val in change_indicators:
            choice = "lang_confirm_change"
            
    if choice == "lang_confirm_yes":
        lang = guess_lang
        new_context = {"lang": lang}
        if "search_doctor_query" in context:
            new_context["search_doctor_query"] = context["search_doctor_query"]
        
        await _send_location_request(client, phone, new_context)
    elif choice == "lang_confirm_change":
        init_context = {}
        if "search_doctor_query" in context:
            init_context["search_doctor_query"] = context["search_doctor_query"]
        await db.clear_conversation_state(phone)
        await _start(client, phone, init_context)
    else:
        # Prompt them again
        prompt = t("confirm_lang_prompt", guess_lang)
        buttons = [
            ("lang_confirm_yes", t("confirm_yes", guess_lang)),
            ("lang_confirm_change", t("confirm_change", guess_lang))
        ]
        await whatsapp_client.send_buttons(client, phone, prompt, buttons)


# ---------------------------------------------------------------------------------------
# 3. Location capture (requirement 2). WhatsApp has no server-side "auto-detect without a
# tap" — send_location_request()'s button opens the phone's native location picker, which
# defaults to sharing current GPS in one tap; that's the real-world equivalent of
# "auto-detect" here. A typed city/area name is accepted as a fallback for anyone who
# declines the GPS prompt (handled in _handle_choosing_location below).
# ---------------------------------------------------------------------------------------


async def _send_location_request(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    await whatsapp_client.send_location_request(client, phone, t("location_prompt", lang))
    await whatsapp_client.send_text(client, phone, t("location_manual_hint", lang))
    await _transition_to(phone, "choosing_location", context, "choosing_language")


async def _safe_city_index() -> dict:
    """The city index, or an empty dict if it can't be built. Every caller treats empty as
    "fall back to a plain city-name search" rather than an error — an unreachable index
    should degrade the search, not break the conversation."""
    try:
        return await city_index.get_index()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("City index unavailable: %s", exc)
        return {}


async def _resolve_city(context: dict) -> dict:
    """Works out which city name to hand to /public/doctors?city=.

    Only used for the typed-location path and as a fallback. When the patient shares GPS the
    search is driven by radius instead (see _fetch_doctors_near) — city names are too
    unreliable to decide results on, since the same town name can exist in more than one
    place and some records carry a city that disagrees with their own coordinates."""
    index = await _safe_city_index()
    if not index:
        return context

    if context.get("patient_lat") is not None:
        city, distance_km = city_index.nearest_city(index, context["patient_lat"], context["patient_lng"])
        if city:
            logger.info("Resolved GPS to city %s (%.1f km)", city, distance_km)
            return {**context, "city": city, "city_distance_km": round(distance_km, 1)}
        return context

    typed = context.get("location_text")
    if typed:
        city = city_index.match_typed_city(index, typed)
        if city:
            logger.info("Matched typed location %r to city %s", typed, city)
            return {**context, "city": city}
        logger.info("Typed location %r matches no known city, searching unfiltered", typed)
    return context


async def _handle_choosing_location(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type == "location":
        lat_str, lng_str = input_value.split(",")
        context = {**context, "patient_lat": float(lat_str), "patient_lng": float(lng_str)}
    elif input_type == "text" and input_value.strip():
        context = {**context, "location_text": input_value.strip()}
    else:
        await whatsapp_client.send_text(client, phone, t("location_prompt", lang))
        return
    context = await _resolve_city(context)

    if context.get("search_doctor_query"):
        if await _search_doctors_flow(client, phone, context, "choosing_location"):
            return
        else:
            query = context.get("search_doctor_query")
            await whatsapp_client.send_text(
                client, phone, t("search_doctor_not_found", lang, query=query)
            )
            context.pop("search_doctor_query", None)

    await _send_search_mode_prompt(client, phone, context)


# ---------------------------------------------------------------------------------------
# 4. Symptom vs. specialty entry (requirement 4) — this part already existed pre-redesign;
# kept as-is functionally, just moved behind language/person/location and translated.
# ---------------------------------------------------------------------------------------


async def _send_search_mode_prompt(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    await whatsapp_client.send_buttons(
        client, phone, t("search_mode_prompt", lang),
        [
            ("symptom", t("search_mode_symptom", lang)),
            ("name", t("search_mode_name", lang)),
            ("browse", t("search_mode_browse", lang)),
        ],
    )
    await _transition_to(phone, "choosing_search_mode", context, "choosing_location")


async def _handle_choosing_search_mode(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type == "text" and _is_doctor_search_query(input_value):
        context = {**context, "search_doctor_query": input_value}
        if await _search_doctors_flow(client, phone, context, "choosing_search_mode"):
            return
        else:
            await whatsapp_client.send_text(
                client, phone, t("search_doctor_not_found", lang, query=input_value)
            )
            context.pop("search_doctor_query", None)

    choice = _match_choice(input_type, input_value, ["symptom", "name", "browse"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("search_mode_choose_hint", lang))
        return
    if choice == "symptom":
        await whatsapp_client.send_text(client, phone, t("symptom_ask", lang))
        await _transition_to(phone, "awaiting_symptom", context, "choosing_search_mode")
        return
    if choice == "name":
        await whatsapp_client.send_text(client, phone, t("doctor_name_ask", lang))
        await _transition_to(phone, "awaiting_doctor_name", context, "choosing_search_mode")
        return
    await _send_specialty_list(client, phone, context)


async def _handle_awaiting_symptom(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type == "text" and _is_doctor_search_query(input_value):
        context = {**context, "search_doctor_query": input_value}
        if await _search_doctors_flow(client, phone, context, "awaiting_symptom"):
            return
        else:
            await whatsapp_client.send_text(
                client, phone, t("search_doctor_not_found", lang, query=input_value)
            )
            context.pop("search_doctor_query", None)

    if input_type != "text" or not input_value.strip():
        await whatsapp_client.send_text(client, phone, t("symptom_text_required", lang))
        return

    labels = await symptom_client.route_symptom(input_value)
    categories = [s["category"] for s in await hms_client.list_specialties()]
    matched_category = next(
        (m for m in (symptom_client.match_category(label, categories) for label in labels) if m),
        None,
    )

    if not matched_category:
        await whatsapp_client.send_text(client, phone, t("symptom_no_match", lang))
        await _send_specialty_list(client, phone, context)
        return

    await _send_sort_prompt(client, phone, context, matched_category, "awaiting_symptom")


def _specialty_row(specialty: dict) -> tuple[str, str, str]:
    """(row_id, title, description) for one specialty.

    row_id must stay the raw category string — that's what comes back as list_reply.id and
    gets passed straight to /public/doctors?specialtyCategory=. Title picks whichever of the
    category-without-parenthetical or the API's displayName is shorter, because WhatsApp
    truncates row titles at 24 chars and "Endocrinologist (Hormones/Diabetes)" would become
    "Endocrinologist (Hormone". The full displayName goes in the description line, so the
    longer official wording is still visible either way."""
    category = specialty["category"]
    display = (specialty.get("displayName") or "").strip() or category
    base = category.split("(")[0].strip()
    title = min([base, display], key=len)
    # Still too long ("Sports Medicine Specialist" is 26) — drop the redundant trailing noun
    # rather than let WhatsApp hard-cut mid-word into "Sports Medicine Speciali".
    if len(title) > 24:
        for suffix in (" Specialist", " Surgeon", " Physician"):
            if title.endswith(suffix) and len(title) - len(suffix) >= 4:
                title = title[: -len(suffix)].strip()
                break
    return category, title, display


def _groups_with_live_categories(specialties: list[dict]) -> list[tuple[dict, list[dict]]]:
    """Pairs each configured group with the live specialties actually in it, drops empty
    groups, and sweeps anything unrecognised into Other. Driven by the live API response
    rather than the static config, so a specialty 1HMS adds later still reaches a patient."""
    by_category = {s["category"]: s for s in specialties}
    claimed: set[str] = set()
    paired = []
    for group in i18n.SPECIALTY_GROUPS:
        members = [by_category[c] for c in group["categories"] if c in by_category]
        claimed.update(s["category"] for s in members)
        if members:
            paired.append((group, members))
    leftovers = [s for s in specialties if s["category"] not in claimed]
    if leftovers:
        paired.append((i18n.OTHER_GROUP, leftovers))
    return paired


async def _send_specialty_list(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    """First of two levels: the broad areas. See the comment above SPECIALTY_GROUPS in
    i18n.py for why browsing can't just list all 30 categories in one message."""
    lang = context.get("lang")
    specialties = await hms_client.list_specialties()
    if not specialties:
        await whatsapp_client.send_text(client, phone, t("no_specialties", lang))
        await db.clear_conversation_state(phone)
        return

    paired = _groups_with_live_categories(specialties)
    rows = []
    for group, members in paired:
        title, desc = i18n.group_label(group, lang)
        rows.append((group["id"], title, desc))

    await whatsapp_client.send_list(
        client, phone, t("specialty_group_prompt", lang), t("specialty_group_button", lang),
        rows, t("specialty_group_section", lang),
    )
    # Remember the group -> categories split that was actually shown, so the next step
    # doesn't have to re-fetch and risk showing a group built from a different response.
    group_members = {group["id"]: [s["category"] for s in members] for group, members in paired}
    await _transition_to(
        phone, "choosing_specialty_group", {**context, "specialty_groups": group_members}, "choosing_search_mode"
    )


async def _handle_choosing_specialty_group(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    group_members = context.get("specialty_groups", {})
    if input_type != "list_reply" or input_value not in group_members:
        # A patient who types instead of tapping here is usually describing a symptom
        # ("ghutne mein dard") rather than fumbling the menu — so route them into symptom
        # search instead of scolding them for not tapping. Falls back to the hint only if
        # the NLP can't place it.
        if input_type == "text" and input_value.strip():
            await _handle_awaiting_symptom(client, phone, input_type, input_value, context)
            return
        await whatsapp_client.send_text(client, phone, t("specialty_group_choose_hint", lang))
        return

    categories = group_members[input_value]
    specialties = await hms_client.list_specialties()
    by_category = {s["category"]: s for s in specialties}
    members = [by_category[c] for c in categories if c in by_category]
    if not members:
        await whatsapp_client.send_text(client, phone, t("no_specialties", lang))
        await _send_specialty_list(client, phone, context)
        return

    # Single-specialty group — asking "which of these fits best?" for a list of one is the
    # kind of pointless tap that makes a bot feel bureaucratic. Skip straight to sorting.
    if len(members) == 1:
        await _send_sort_prompt(client, phone, context, members[0]["category"], "choosing_specialty_group")
        return

    rows = [_specialty_row(s) for s in members]
    await whatsapp_client.send_list(
        client, phone, t("specialty_list_prompt", lang), t("specialty_list_button", lang),
        rows, t("specialty_group_section", lang),
    )
    await _transition_to(phone, "choosing_specialty", context, "choosing_specialty_group")


async def _handle_choosing_specialty(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, t("specialty_choose_hint", lang))
        return
    await _send_sort_prompt(client, phone, context, input_value, "choosing_specialty")


# ---------------------------------------------------------------------------------------
# 5. Doctor filtering by rating / distance / experience / lowest fee (requirement 5).
# Confirmed live against 1hms-dev-api.nexeagle.com that GET /public/doctors already returns
# rating, fee, discountedFee, experienceYears, latitude/longitude, hospitalName, address —
# so this is a client-side sort over data the API already returns, no backend change
# needed. Also confirmed that sortBy/latitude/longitude query params are silently ignored by
# that endpoint today (order was identical with or without them) — so the sorting has to
# happen here, not by asking the API to do it.
# ---------------------------------------------------------------------------------------


async def _send_sort_prompt(client: httpx.AsyncClient, phone: str, context: dict, specialty_category: str, current_step: str) -> None:
    lang = context.get("lang")
    context = {**context, "specialty_category": specialty_category}
    rows = [
        ("rating", t("sort_rating", lang)),
        ("experience", t("sort_experience", lang)),
        ("fee", t("sort_fee", lang)),
    ]
    # "Nearest" only makes sense if we actually have something to measure distance from —
    # omitted rather than shown-and-broken when the patient only typed a city name with no
    # coordinates and that name doesn't help either (handled inside _sort_doctors, but no
    # point offering the option here if it can't do anything).
    if context.get("patient_lat") is not None or context.get("location_text"):
        rows.insert(1, ("nearest", t("sort_nearest", lang)))
    await whatsapp_client.send_list(client, phone, t("sort_prompt", lang), t("sort_button", lang), rows, "Sort")
    await _transition_to(phone, "choosing_sort", context, current_step)


async def _handle_choosing_sort(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, _SORT_OPTIONS)
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("sort_choose_hint", lang))
        return
    await _send_doctor_list(client, phone, {**context, "sort_key": choice})


def _clinic_now() -> datetime:
    """Current time where the clinics actually are. Never use datetime.now() bare in this
    file — the container runs on UTC (see settings.clinic_timezone)."""
    return datetime.now(ZoneInfo(settings.clinic_timezone))


def _parse_shift_end(shift: dict) -> time | None:
    """The shift's finish time, e.g. "20:30:00" -> 20:30. Returns None if the API omits or
    malforms it, and callers then treat the shift as still open rather than hiding it —
    better to show a shift that has passed than to hide one that hasn't."""
    raw = shift.get("endTime")
    if not raw:
        return None
    try:
        return time.fromisoformat(str(raw))
    except ValueError:
        logger.warning("Unparseable shift endTime %r", raw)
        return None


def _usable_shifts(availability: dict, preferred_date: date) -> list[str]:
    """Shift names the patient could still turn up for.

    The availability endpoint returns a doctor's standing schedule for a date — it does not
    know what time it is, so for today it happily returns Morning (09:00-12:00) at 7pm.
    Offering that books a patient into a slot that has already passed, so today's shifts are
    filtered against the clock. Future dates pass through untouched."""
    shifts = [s for s in availability.get("shifts", []) if s.get("name")]
    if not shifts:
        return list(_SHIFT_FALLBACK)
    if preferred_date != _clinic_now().date():
        return [s["name"] for s in shifts]

    now = _clinic_now().time()
    usable = []
    for shift in shifts:
        end = _parse_shift_end(shift)
        if end is None or end > now:
            usable.append(shift["name"])
    return usable


def _doctor_fee(doctor: dict) -> float:
    fee = doctor.get("discountedFee")
    return fee if fee is not None else (doctor.get("fee") or float("inf"))


def _doctor_rating(doctor: dict) -> float:
    rating = doctor.get("rating")
    return rating if rating is not None else -1  # unrated doctors sort to the bottom, not the top


def _doctor_distance_km(doctor: dict, patient_lat: float | None, patient_lng: float | None) -> float:
    lat, lng = doctor.get("latitude"), doctor.get("longitude")
    if patient_lat is None or patient_lng is None or lat is None or lng is None:
        return float("inf")
    return haversine_km(patient_lat, patient_lng, lat, lng)


def _sort_doctors(doctors: list[dict], context: dict) -> list[dict]:
    sort_key = context.get("sort_key")
    patient_lat, patient_lng = context.get("patient_lat"), context.get("patient_lng")
    if sort_key == "rating":
        return sorted(doctors, key=lambda d: -_doctor_rating(d))
    if sort_key == "experience":
        return sorted(doctors, key=lambda d: -(d.get("experienceYears") or 0))
    if sort_key == "fee":
        return sorted(doctors, key=_doctor_fee)
    if sort_key == "nearest":
        if patient_lat is not None and patient_lng is not None:
            return sorted(doctors, key=lambda d: _doctor_distance_km(d, patient_lat, patient_lng))
        # No GPS, only a typed city — best-effort: exact city-name matches float to the
        # top, everything else keeps the API's original order. Not true distance sorting,
        # but there's no geocoding service wired up to turn "Kishanganj" into a lat/long
        # (would need a Maps API key + a new dependency — flagged as a possible follow-up,
        # not built here since it's outside what a typed city name alone can support).
        city = (context.get("location_text") or "").strip().lower()
        return sorted(doctors, key=lambda d: 0 if (d.get("city") or "").strip().lower() == city else 1)
    return doctors


def _clean_specialty(spec: str) -> str:
    if not spec:
        return ""
    spec = spec.replace("QA Dev Seed", "").strip()
    if spec.startswith("-"):
        spec = spec[1:].strip()
    spec = spec.split("/")[0].strip()
    spec = spec.split("(")[0].strip()
    spec = spec.split("-")[0].strip()
    return spec.strip()


def _clean_hospital(hosp: str) -> str:
    if not hosp:
        return ""
    hosp = hosp.replace("(QA Dev Seed)", "").replace("QA Dev Seed", "").strip()
    hosp = hosp.split("(")[0].strip()
    if hosp.endswith("-"):
        hosp = hosp[:-1].strip()
    return hosp.strip()


def _doctor_row_description(doctor: dict, context: dict) -> str:
    parts = []
    spec = (
        doctor.get("primaryMedicalSpecialityPatientFacingName")
        or doctor.get("primaryMedicalSpecialityCategory")
        or doctor.get("departmentName")
        or doctor.get("specialtyName")
        or doctor.get("specialtyCategory")
    )
    spec_cleaned = _clean_specialty(spec)
    if spec_cleaned:
        parts.append(spec_cleaned)
    hosp = doctor.get("hospitalName") or doctor.get("city")
    hosp_cleaned = _clean_hospital(hosp)
    if hosp_cleaned:
        parts.append(hosp_cleaned)
    if doctor.get("rating") is not None:
        parts.append(f"⭐{doctor['rating']}")
    fee = _doctor_fee(doctor)
    if fee != float("inf"):
        parts.append(f"₹{fee:.0f}")
    if doctor.get("experienceYears") is not None:
        parts.append(f"{doctor['experienceYears']}yrs")
    distance = _doctor_distance_km(doctor, context.get("patient_lat"), context.get("patient_lng"))
    if distance != float("inf"):
        parts.append(f"{distance:.0f}km")
    desc = " · ".join(parts)
    if len(desc) > 72:
        desc = desc[:69] + "..."
    return desc


async def _fetch_doctors_near(
    specialty_category: str, context: dict, radius_km: float, index: dict, cache: dict
) -> list[dict]:
    """Doctors of this specialty whose OWN coordinates fall within radius_km of the patient.

    Pulls from every index city in range rather than just the patient's own, since the
    nearest doctor may be in the next town, then filters on real per-doctor distance. City
    names are only ever used to ask the API for candidates — they never decide what the
    patient sees, which is what makes this robust to a town name existing in two places and
    to records carrying the wrong city label.

    The index is passed in rather than stashed on `context` because context is serialised
    into conversation_state on every step, and the index is far too big to belong there."""
    lat, lng = context.get("patient_lat"), context.get("patient_lng")
    if lat is None or not index:
        return []

    nearby = city_index.cities_within(index, lat, lng, radius_km, settings.doctor_search_max_cities)
    if not nearby:
        return []

    by_id: dict[str, dict] = {}
    for city, _ in nearby:
        if city not in cache:
            # `cache` lives for one search only. Widening bands overlap — the 75km pass
            # re-covers every city the 10km pass already looked at — so without this the
            # nearest city gets fetched once per band for no benefit.
            cache[city] = await hms_client.list_doctors(
                specialty_category, page_size=50, city=city
            )
        for doctor in cache[city]:
            # Same doctor can surface under more than one city query when a town's records
            # are split; keep one copy.
            by_id.setdefault(doctor["doctorId"], doctor)

    within = []
    for doctor in by_id.values():
        distance = _doctor_distance_km(doctor, lat, lng)
        if distance <= radius_km:
            within.append(doctor)
        elif distance != float("inf"):
            # Came back under a nearby city's name but sits far outside the radius — i.e. the
            # record's city label disagrees with its own coordinates. Logged rather than
            # silently dropped so the bad records can be reported to the 1HMS team; pincode
            # is included because in the data seen so far it agrees with the coordinates, not
            # the city, which makes it the useful identifier when chasing these down.
            logger.info(
                "Excluding doctor %s (%s): labelled city=%r pincode=%s but %.0f km from patient",
                doctor.get("doctorId"), doctor.get("fullName"),
                doctor.get("city"), doctor.get("pincode"), distance,
            )
    return within


async def _send_doctor_list(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    specialty_category = context["specialty_category"]

    doctors: list[dict] = []
    used_radius: float | None = None
    if context.get("patient_lat") is not None:
        index = await _safe_city_index()
        fetch_cache: dict[str, list[dict]] = {}
        # Nearest band first, widening only when it comes up empty. Deliberately stops at the
        # last configured radius instead of quietly searching the whole country.
        for radius in settings.doctor_search_radii_km:
            doctors = await _fetch_doctors_near(
                specialty_category, context, radius, index, fetch_cache
            )
            if doctors:
                used_radius = radius
                break

        if not doctors:
            # Nothing within the furthest band. Ask before going further — travelling
            # several hundred km is the patient's decision, not ours to assume.
            max_radius = settings.doctor_search_radii_km[-1]
            await whatsapp_client.send_buttons(
                client, phone,
                t("no_doctors_in_radius", lang, radius=int(max_radius)),
                [("search_wider", t("search_wider_yes", lang)), ("cancel", t("cancel_btn", lang))],
            )
            await _transition_to(phone, "confirming_wider_search", context, "choosing_sort")
            return
    else:
        # No coordinates (patient typed a place name) — radius filtering isn't possible, so
        # fall back to the city-name filter.
        doctors = await hms_client.list_doctors(
            specialty_category, page_size=50, city=context.get("city")
        )

    if not doctors:
        await whatsapp_client.send_text(client, phone, t("no_doctors", lang))
        await db.clear_conversation_state(phone)
        return

    if used_radius is not None and used_radius != settings.doctor_search_radii_km[0]:
        await whatsapp_client.send_text(
            client, phone, t("doctors_widened_radius", lang, radius=int(used_radius))
        )

    await _render_doctor_list(client, phone, context, doctors, "choosing_sort")


async def _render_doctor_list(
    client: httpx.AsyncClient, phone: str, context: dict, doctors: list[dict], current_step: str | None = None
) -> None:
    """Sorts, trims to WhatsApp's row cap, and sends. Shared by the normal radius search and
    the opted-in wider search so both present results identically."""
    lang = context.get("lang")
    sorted_doctors = _sort_doctors(doctors, context)[:10]
    
    if len(sorted_doctors) == 1:
        d = sorted_doctors[0]
        context = {
            **context,
            "doctor_id": d["doctorId"],
            "doctor_name": d.get("fullName") or "Doctor",
            "doctor_fee": _doctor_fee(d),
            "hospital_name": d.get("hospitalName") or "",
            "hospital_address": d.get("address") or "",
            "hospital_city": d.get("city") or "",
            "hospital_lat": d.get("latitude"),
            "hospital_lng": d.get("longitude"),
        }
        context.pop("doctor_options", None)
        
        info_msg = f"Found matching doctor: {context['doctor_name']} ({context['hospital_name']})."
        if lang == "hi":
            info_msg = f"आपके लिए डॉक्टर मिले: {context['doctor_name']} ({context['hospital_name']})."
        elif lang == "hg":
            info_msg = f"Aapke matching doctor mile: {context['doctor_name']} ({context['hospital_name']})."
        elif lang == "bn":
            info_msg = f"আপনার জন্য ডাক্তার পাওয়া গেছে: {context['doctor_name']} ({context['hospital_name']})."
            
        await whatsapp_client.send_text(client, phone, info_msg)
        await _send_slot_options(client, phone, context)
        return

    rows = [
        (d["doctorId"], d.get("fullName") or "Doctor", _doctor_row_description(d, context))
        for d in sorted_doctors
    ]
    await whatsapp_client.send_list(
        client, phone, t("doctor_list_prompt", lang), t("doctor_list_button", lang), rows, "Doctors"
    )
    # Full doctor dicts (not just IDs) are stashed in context so the next step can pull out
    # fee/hospital name/address/lat-long without a second API round trip — see
    # _handle_choosing_doctor below. Small enough (<=10 doctors) to live in context_json.
    context = {**context, "doctor_options": {d["doctorId"]: d for d in sorted_doctors}}
    if current_step:
        await _transition_to(phone, "choosing_doctor", context, current_step)
    else:
        await db.save_conversation_state(phone, "choosing_doctor", context)


async def _handle_confirming_wider_search(client, phone, input_type, input_value, context) -> None:
    """Nothing was found inside the furthest radius band, and the patient has said whether
    they're willing to look further. Only on an explicit yes do we drop the radius cap."""
    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["search_wider", "cancel"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("confirm_choose_hint", lang))
        return
    if choice == "cancel":
        await whatsapp_client.send_text(client, phone, t("cancelled", lang))
        await db.clear_conversation_state(phone)
        return

    # Patient opted in to travelling further, so search unrestricted by distance. Sorting
    # still puts the closest first, so "further" never means "in a random order".
    doctors = await hms_client.list_doctors(context["specialty_category"], page_size=50)
    if not doctors:
        await whatsapp_client.send_text(client, phone, t("no_doctors", lang))
        await db.clear_conversation_state(phone)
        return
    await _render_doctor_list(client, phone, {**context, "sort_key": "nearest"}, doctors, "confirming_wider_search")


async def _handle_choosing_doctor(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, t("doctor_choose_hint", lang))
        return
    doctor_id = input_value
    doctor = context.get("doctor_options", {}).get(doctor_id)
    if doctor is None:
        # Stale list (e.g. patient tapped an old message) — restart rather than book with
        # data we can no longer vouch for.
        await whatsapp_client.send_text(client, phone, t("doctor_choose_hint", lang))
        return

    context = {
        **context,
        "doctor_id": doctor_id,
        "doctor_name": doctor.get("fullName") or "Doctor",
        "doctor_fee": _doctor_fee(doctor),
        "hospital_name": doctor.get("hospitalName") or "",
        "hospital_address": doctor.get("address") or "",
        "hospital_city": doctor.get("city") or "",
        "hospital_lat": doctor.get("latitude"),
        "hospital_lng": doctor.get("longitude"),
    }
    context.pop("doctor_options", None)  # no longer needed, keep context_json lean

    await _send_slot_options(client, phone, context)


# ---------------------------------------------------------------------------------------
# 6-7. Day + shift ("slot timing") + confirm (requirements 6-7) — day selection already
# existed pre-redesign. One important limitation confirmed live against the HMS API:
# GET /public/doctors/{id}/availability returns only three named shifts (Morning/
# Afternoon/Evening, each with a start/end time), not individual bookable time slots
# (e.g. "10:30"). "Slot timing" in this system means picking a shift, same as it already
# did — true per-slot booking would need a different/extended availability endpoint on the
# 1HMS side; flagging this rather than pretending 15-minute-granularity slot picking is
# already wired up.
# ---------------------------------------------------------------------------------------


def _format_slot_label(shift_name: str, is_today: bool, lang: str | None) -> str:
    # Get localized shift name
    shift_key = shift_name.lower()
    if shift_key == "afternoon":
        shift_key = "noon"

    localized_shift = t(f"shift_{shift_key}", lang) or shift_name

    # Get localized date label
    date_key = "date_today" if is_today else "date_tomorrow"
    localized_date = t(date_key, lang)

    return f"{localized_shift} ({localized_date})"


async def _get_offered_slots(doctor_id: str, lang: str | None) -> list[dict]:
    today = _clinic_now().date()
    tomorrow = today + timedelta(days=1)

    today_avail, tomorrow_avail = await asyncio.gather(
        hms_client.get_doctor_availability(doctor_id, today),
        hms_client.get_doctor_availability(doctor_id, tomorrow)
    )

    slots = []
    if today_avail.get("isAvailable"):
        today_shifts = _usable_shifts(today_avail, today)
        for shift in today_shifts:
            slots.append({
                "date": today,
                "is_today": True,
                "shift_name": shift,
                "button_id": f"slot_today_{shift.lower()}",
                "label": _format_slot_label(shift, True, lang)
            })

    if tomorrow_avail.get("isAvailable"):
        tomorrow_shifts = _usable_shifts(tomorrow_avail, tomorrow)
        for shift in tomorrow_shifts:
            slots.append({
                "date": tomorrow,
                "is_today": False,
                "shift_name": shift,
                "button_id": f"slot_tomorrow_{shift.lower()}",
                "label": _format_slot_label(shift, False, lang)
            })

    return slots[:3]


async def _send_slot_options(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    doctor_id = context["doctor_id"]

    slots = await _get_offered_slots(doctor_id, lang)
    if not slots:
        await whatsapp_client.send_buttons(
            client, phone, t("today_shifts_over", lang),
            [("change_doctor", t("change_doctor_btn", lang))],
        )
        await _transition_to(phone, "choosing_slot", context, "choosing_doctor")
        return

    buttons = [(s["button_id"], s["label"]) for s in slots]

    offered_slots_data = [
        {
            "button_id": s["button_id"],
            "shift_name": s["shift_name"],
            "date": s["date"].isoformat(),
            "is_today": s["is_today"],
            "label": s["label"]
        }
        for s in slots
    ]

    await whatsapp_client.send_buttons(
        client, phone, t("shift_prompt", lang),
        buttons,
    )

    await _transition_to(
        phone, "choosing_slot",
        {
            **context,
            "offered_slots": offered_slots_data,
        },
        "choosing_doctor"
    )


async def _handle_choosing_slot(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    offered_slots = context.get("offered_slots") or []

    valid_ids = []
    label_to_slot = {}
    shift_name_to_slot = {}

    for s in offered_slots:
        b_id = s["button_id"]
        valid_ids.append(b_id)
        label_to_slot[s["label"].lower()] = s

        sh_name = s["shift_name"].lower()
        if sh_name not in shift_name_to_slot:
            shift_name_to_slot[sh_name] = s

    valid_ids.append("change_doctor")

    choice = _match_choice(input_type, input_value, valid_ids)
    selected_slot = None

    if choice and choice != "change_doctor":
        selected_slot = next(s for s in offered_slots if s["button_id"] == choice)
    elif choice == "change_doctor":
        await _send_doctor_list(client, phone, context)
        return
    else:
        normalized = input_value.strip().lower()
        if normalized in label_to_slot:
            selected_slot = label_to_slot[normalized]
        elif normalized in shift_name_to_slot:
            selected_slot = shift_name_to_slot[normalized]

    if selected_slot is None:
        options_str = ", ".join(s["label"] for s in offered_slots)
        await whatsapp_client.send_text(
            client, phone, t("shift_choose_hint", lang, options=options_str)
        )
        return

    date_label = t("date_today", lang) if selected_slot["is_today"] else t("date_tomorrow", lang)
    context = {
        **context,
        "preferred_date": selected_slot["date"],
        "date_label": date_label,
        "shift_label": selected_slot["shift_name"],
    }
    context.pop("offered_slots", None)

    await _send_patient_details_flow(client, phone, context)


def _clinic_line(context: dict, lang: str | None) -> str:
    """Where the patient actually has to travel to, as one line.

    Worth spelling out at confirm time: the search now reaches up to 75km, so the chosen
    doctor may well be in a different town from the patient. Previously this only became
    apparent after confirming, when the map pin arrived."""
    parts = [p for p in (context.get("hospital_name"), context.get("hospital_city")) if p]
    where = ", ".join(parts) or t("clinic_unknown", lang)
    distance = _doctor_distance_km(
        {"latitude": context.get("hospital_lat"), "longitude": context.get("hospital_lng")},
        context.get("patient_lat"), context.get("patient_lng"),
    )
    if distance != float("inf"):
        where += f" · {distance:.0f} km"
    return where


def _patient_line(context: dict, lang: str | None) -> str:
    name = context.get("patient_display_name") or t("you", lang)
    age = context.get("patient_age")
    gender = context.get("patient_gender")
    guardian = context.get("patient_guardian")

    parts = [name]
    if age:
        parts.append(str(age))
    if gender:
        parts.append(gender)
    line = ", ".join(parts)
    if guardian:
        line += f" (Guardian: {guardian})"
    return line


async def _send_patient_details_flow(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    flow_id = settings.whatsapp_flow_id
    success = False
    if flow_id:
        success = await whatsapp_client.send_flow(
            client,
            to=phone,
            body_text=t("patient_details_prompt_flow", lang),
            flow_id=flow_id,
            flow_cta=t("patient_details_flow_cta", lang),
            screen_id=settings.whatsapp_flow_screen_id,
            flow_token=f"token-{phone}",
        )
    if not success:
        await whatsapp_client.send_text(client, phone, t("patient_details_prompt_text", lang))
    await _transition_to(phone, "awaiting_patient_details", context, "choosing_slot")


async def _handle_awaiting_patient_details(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    name, age, gender, guardian = None, None, None, None

    if input_type == "nfm_reply":
        try:
            data = json.loads(input_value)
            name = (data.get("name") or "").strip()
            age = str(data.get("age") or "").strip()
            gender = (data.get("gender") or "").strip()
            guardian = (data.get("guardian") or "").strip()
        except Exception:
            pass
    elif input_type == "text":
        parsed = _parse_details(input_value, 4)
        if parsed:
            name, age, gender, guardian = parsed

    if not name or not age or not gender or not guardian:
        await whatsapp_client.send_text(client, phone, t("patient_details_invalid", lang))
        await _send_patient_details_flow(client, phone, context)
        return

    if not _looks_like_age(age):
        await whatsapp_client.send_text(client, phone, t("age_invalid", lang))
        await _send_patient_details_flow(client, phone, context)
        return

    context = {
        **context,
        "patient_display_name": name,
        "patient_age": age,
        "patient_gender": gender,
        "patient_guardian": guardian,
    }

    fee = context.get("doctor_fee")
    await whatsapp_client.send_buttons(
        client, phone,
        t(
            "confirm_prompt", lang,
            patient=_patient_line(context, lang),
            doctor=context.get("doctor_name", "-"),
            where=_clinic_line(context, lang),
            when=f"{context.get('date_label', '')}, {context.get('shift_label', '')}",
            fee=f"{fee:.0f}" if fee is not None else "-",
        ),
        [
            ("confirm", t("confirm_btn", lang)),
            ("update_details", t("update_details_btn", lang)),
            ("cancel", t("cancel_btn", lang)),
        ],
    )
    await _transition_to(phone, "confirming", context, "awaiting_patient_details")


async def _handle_confirming(client, phone, sender_name, input_type, input_value, context) -> None:
    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["confirm", "cancel", "update_details"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("confirm_choose_hint", lang))
        return
    if choice == "cancel":
        await whatsapp_client.send_text(client, phone, t("cancelled", lang))
        await db.clear_conversation_state(phone)
        return
    if choice == "update_details":
        await _send_patient_details_flow(client, phone, context)
        return

    preferred_date = date.fromisoformat(context["preferred_date"])
    doctor_id = context["doctor_id"]
    shift_label = context.get("shift_label", "any time")
    booking_for = context.get("booking_for", "self")
    patient_name = context.get("patient_display_name") or sender_name or phone
    patient_age = context.get("patient_age")
    patient_gender = context.get("patient_gender")
    patient_guardian = context.get("patient_guardian")

    if await db.has_pending_appointment(phone, preferred_date):
        await whatsapp_client.send_text(client, phone, t("already_pending", lang))
        await db.clear_conversation_state(phone)
        return

    row_id = await db.create_pending_appointment(
        phone, preferred_date,
        preferred_language=lang, booking_for=booking_for, patient_display_name=patient_name,
        patient_age=int(patient_age) if patient_age else None,
        patient_gender=patient_gender,
        patient_guardian=patient_guardian,
    )
    note_bits = []
    if patient_age:
        note_bits.append(f"age {patient_age}")
    if patient_gender:
        note_bits.append(f"gender {patient_gender}")
    if patient_guardian:
        note_bits.append(f"guardian {patient_guardian}")
    extra_note = "; ".join(note_bits) or None

    try:
        result = await hms_client.book_appointment(
            patient_name, phone, doctor_id, preferred_date, shift_label, extra_note=extra_note
        )
    except (HmsApiError, httpx.HTTPError):
        await db.mark_appointment_failed(row_id)
        raise

    hms_appointment_id = result.get("appointmentId") or ""
    await db.mark_appointment_booked(row_id, hms_appointment_id)

    await whatsapp_client.send_text(client, phone, t("booked_success", lang, patient_name=patient_name))

    hospital_lat, hospital_lng = context.get("hospital_lat"), context.get("hospital_lng")
    if hospital_lat is not None and hospital_lng is not None:
        await whatsapp_client.send_location(
            client, phone, hospital_lat, hospital_lng,
            name=context.get("hospital_name", ""), address=context.get("hospital_address", ""),
        )

    await whatsapp_client.send_text(client, phone, t("booked_queue_note", lang))

    # Mark NLU correctness based on final booked doctor matching extracted NLU name
    final_doctor_name = context.get("doctor_name")
    if final_doctor_name and hasattr(db, "mark_session_nlu_correctness_on_booking"):
        await db.mark_session_nlu_correctness_on_booking(phone, final_doctor_name)

    await db.clear_conversation_state(phone)


def _is_doctor_search_query(text: str) -> bool:
    normalized = text.strip().lower()
    if re.search(r'\b(dr|doctor)\b', normalized):
        return True
    return False


def _match_doctor_by_query(query: str, doctors: list[dict]) -> list[dict]:
    normalized = query.lower()
    normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)

    for greeting in ["hi", "hello", "hey", "hlo"]:
        normalized = re.sub(r'\b' + greeting + r'\b', ' ', normalized)

    for prefix in [
        "book appointment at", "book appointment with", "appointment at", "appointment with",
        "book appointment", "appointment", "want to book", "book", "dr", "doctor"
    ]:
        normalized = normalized.replace(prefix, " ")

    normalized = re.sub(r'\s+', ' ', normalized).strip()
    if not normalized:
        return []

    matches = []
    for doc in doctors:
        name = (doc.get("fullName") or "").lower()
        name_clean = re.sub(r'[^a-z0-9\s]', ' ', name)
        name_clean = re.sub(r'\s+', ' ', name_clean).strip()

        if normalized in name_clean or name_clean in normalized:
            matches.append(doc)
            continue

        name_parts = name_clean.split()
        query_parts = normalized.split()
        matched_parts = 0
        for qp in query_parts:
            if len(qp) >= 3 and any(qp in np for np in name_parts):
                matched_parts += 1
        if matched_parts > 0:
            matches.append(doc)

    return matches


async def _search_doctors_flow(client: httpx.AsyncClient, phone: str, context: dict, current_step: str) -> bool:
    """Performs a direct doctor search by name. If matches are found, it lists them.
    Returns True if we handled the flow by finding and showing matches, False otherwise."""
    query = context.get("search_doctor_query")
    if not query:
        return False

    lang = context.get("lang")
    try:
        all_docs = await city_index.get_all_doctors()
    except Exception as exc:
        logger.error("Failed to fetch all doctors for search: %s", exc)
        return False

    matches = _match_doctor_by_query(query, all_docs)
    if not matches:
        return False

    # Filter matches by location first (GPS radius or city name)
    local_matches = []
    lat, lng = context.get("patient_lat"), context.get("patient_lng")
    city = context.get("city")

    if lat is not None and lng is not None:
        max_radius = settings.doctor_search_radii_km[-1]
        local_matches = [
            d for d in matches
            if _doctor_distance_km(d, lat, lng) <= max_radius
        ]
    elif city:
        city_clean = city.strip().lower()
        local_matches = [
            d for d in matches
            if (d.get("city") or "").strip().lower() == city_clean
        ]

    if local_matches:
        matches = local_matches

    await _render_doctor_list(client, phone, context, matches, current_step)
    return True


async def _transition_to(phone: str, next_step: str, context: dict, current_step: str | None) -> None:
    history = context.get("_history", [])
    if current_step:
        clean_context = {k: v for k, v in context.items() if k != "_history"}
        history = list(history) + [{"current_step": current_step, "context": clean_context}]
        if len(history) > 10:
            history.pop(0)
    new_context = {**context, "_history": history}
    await db.save_conversation_state(phone, next_step, new_context)


async def _trigger_step_prompt(client: httpx.AsyncClient, phone: str, step: str, context: dict) -> None:
    lang = context.get("lang")
    if step == "choosing_language":
        await _start(client, phone)
    elif step == "confirming_language":
        guess_lang = context.get("guess_lang", "en")
        prompt = t("confirm_lang_prompt", guess_lang)
        buttons = [
            ("lang_confirm_yes", t("confirm_yes", guess_lang)),
            ("lang_confirm_change", t("confirm_change", guess_lang))
        ]
        await whatsapp_client.send_buttons(client, phone, prompt, buttons)
    elif step == "choosing_location":
        await _send_location_request(client, phone, context)
    elif step == "choosing_search_mode":
        await _send_search_mode_prompt(client, phone, context)
    elif step == "awaiting_symptom":
        await whatsapp_client.send_text(client, phone, t("symptom_ask", lang))
    elif step == "awaiting_doctor_name":
        await whatsapp_client.send_text(client, phone, t("doctor_name_ask", lang))
    elif step == "choosing_specialty_group":
        await _send_specialty_list(client, phone, context)
    elif step == "choosing_specialty":
        group_members = context.get("specialty_groups", {})
        rows = [_specialty_row(s) for s in group_members.values()]
        await whatsapp_client.send_list(
            client, phone, t("specialty_list_prompt", lang), t("specialty_list_button", lang),
            rows, t("specialty_group_section", lang),
        )
    elif step == "choosing_sort":
        await _send_sort_prompt(client, phone, context, context.get("specialty_category"))
    elif step == "choosing_doctor":
        docs = list(context.get("doctor_options", {}).values())
        if docs:
            await _render_doctor_list(client, phone, context, docs)
        else:
            await _send_search_mode_prompt(client, phone, context)
    elif step == "choosing_slot":
        await _send_slot_options(client, phone, context)
    elif step == "awaiting_patient_details":
        await _send_patient_details_flow(client, phone, context)
    elif step == "confirming":
        fee = context.get("doctor_fee")
        await whatsapp_client.send_buttons(
            client, phone,
            t(
                "confirm_prompt", lang,
                patient=_patient_line(context, lang),
                doctor=context.get("doctor_name", "-"),
                where=_clinic_line(context, lang),
                when=f"{context.get('date_label', '')}, {context.get('shift_label', '')}",
                fee=f"{fee:.0f}" if fee is not None else "-",
            ),
            [
                ("confirm", t("confirm_btn", lang)),
                ("update_details", t("update_details_btn", lang)),
                ("cancel", t("cancel_btn", lang)),
            ],
        )
    else:
        await _start(client, phone)


async def _handle_awaiting_doctor_name(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "text" or not input_value.strip():
        await whatsapp_client.send_text(client, phone, t("doctor_name_text_required", lang))
        return

    context = {**context, "search_doctor_query": input_value.strip()}
    if await _search_doctors_flow(client, phone, context, "awaiting_doctor_name"):
        return
    else:
        await whatsapp_client.send_text(
            client, phone, t("search_doctor_not_found", lang, query=input_value)
        )
        context.pop("search_doctor_query", None)
        await _send_search_mode_prompt(client, phone, context)
