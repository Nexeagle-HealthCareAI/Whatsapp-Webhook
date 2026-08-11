import asyncio
import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import booking_slots, city_index, db, hms_client, i18n, symptom_client, whatsapp_client
from app.resolver import match_doctor_by_query as _match_doctor_by_query
from app.resolver import resolve_doctor, extract_location_from_query
from app.config import settings
from app.geo import haversine_km
from app.hms_client import HmsApiError
from app import nlu_client, intent_router, safety
from app.model_config import PRIMARY_NLU
from app.i18n import LANGUAGE_LABELS, LANG_PROMPT, t

logger = logging.getLogger("conversation")

_SHIFT_FALLBACK = ["Morning", "Afternoon", "Evening"]

# Kept for anything importing the old constant name (e.g. tests) — superseded by
# i18n.LANG_PROMPT as the very first message now, since language is asked before anything
# else. See _start() below.
GREETING_TEXT = "Hi! I can help you book a doctor's appointment."

_SORT_OPTIONS = ["rating", "nearest", "experience", "fee"]

# OPD QR check-in trigger — matches the "CHECKIN <code>" text GET /c/{hospital_code}
# (app/webhook.py) prefills into the wa.me deep link. See the interceptor in handle_message.
_CHECKIN_TRIGGER_PATTERN = re.compile(r"^checkin\s+(\S+)$", re.IGNORECASE)


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


def _detect_language(text: str) -> tuple[str | None, bool]:
    if not text:
        return None, False

    # Normalize: lowercase, strip, and strip common punctuation
    normalized = text.strip().lower()
    normalized = re.sub(r'[^\w\s\u0900-\u097F\u0980-\u09FF]', '', normalized)

    # Generic greetings: return None to present open language selection prompt
    greetings = {"hi", "hello", "hey", "hola", "namaste", "pranam", "helo", "hlo"}
    if normalized in greetings:
        return None, False

    # Devanagari Hindi check
    if re.search(r'[\u0900-\u097F]', text):
        return "hi", True

    # Bengali check
    if re.search(r'[\u0980-\u09FF]', text):
        return "bn", True

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
        return None, False

    # Calculate scores: unambiguous keywords (Hinglish/Benglish) get higher weight
    # than English keywords, which are frequently used as loanwords in other languages.
    hi_score = sum(2 for w in words if w in hinglish_keywords)
    bn_score = sum(2 for w in words if w in benglish_keywords)
    en_score = sum(1 for w in words if w in english_keywords)

    if hi_score == 0 and bn_score == 0 and en_score == 0:
        return None, False

    max_score = max(hi_score, bn_score, en_score)

    if hi_score == max_score and hi_score > 0 and hi_score > bn_score:
        return "hg", False
    elif bn_score == max_score and bn_score > 0 and bn_score > hi_score:
        return "bn", False
    elif en_score == max_score and en_score > hi_score and en_score > bn_score:
        return "en", False
    elif hi_score == en_score and hi_score > bn_score and hi_score > 0:
        return "hg", False
    elif bn_score == en_score and bn_score > hi_score and bn_score > 0:
        return "bn", False
    else:
        return None, False

# ---------------------------------------------------------------------------------------
# Clipboard Initialization from Legacy Context
#
# Helper function to initialize the new clipboard data structure from existing legacy
# session parameters for backwards compatibility.
# ---------------------------------------------------------------------------------------

def _init_clipboard_from_legacy(context: dict) -> dict:
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

    if context.get("doctor_options"):
        booking_slots.mark_ambiguous(
            slots, "doctor", context["doctor_options"], raw=context.get("search_doctor_query")
        )
    elif context.get("search_doctor_query"):
        pass
    elif context.get("doctor_id"):
        booking_slots.fill(
            slots, "doctor",
            {"id": context["doctor_id"], "fullName": context.get("doctor_name")},
            source="legacy",
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


def _get_or_create_clipboard(context: dict) -> dict:
    if "booking" in context and isinstance(context["booking"], dict):
        return context["booking"]
    slots = _init_clipboard_from_legacy(context)
    context["booking"] = slots
    return slots


def _step_for_action(action: str, slot_name: str | None, context: dict) -> str:
    if action == "ask" and slot_name == "lang":
        return "choosing_language"
    elif action == "disambiguate" and slot_name == "lang":
        return "confirming_language"
    elif action == "ask" and slot_name == "location":
        return "choosing_location"
    elif action == "disambiguate" and slot_name == "location":
        return "choosing_location"
    elif action == "ask" and slot_name == "doctor":
        current_step = context.get("current_step")
        if current_step in {
            "choosing_search_mode", "awaiting_symptom", "awaiting_doctor_name",
            "choosing_specialty_group", "choosing_specialty", "choosing_sort",
            "confirming_wider_search", "choosing_doctor"
        }:
            return current_step
        return "choosing_search_mode"
    elif action == "disambiguate" and slot_name == "doctor":
        return "choosing_doctor"
    elif action in ("ask", "retry") and slot_name in ("date", "shift"):
        return "awaiting_patient_details"
    elif action == "ask" and slot_name == "patient":
        return "awaiting_patient_details"
    elif action == "confirm":
        return "confirming"
    return "choosing_search_mode"


async def _advance_booking_flow(client: httpx.AsyncClient, phone: str, context: dict, booking: dict) -> None:
    # A pending doctor-name search takes priority over whatever next_action()'s own slot
    # order would ask for next. SLOT_ORDER puts location before doctor, so waiting for
    # next_action() to say "doctor" (i.e. waiting for location to already be filled) means a
    # patient who named a doctor on their very first message — before location was ever
    # known — gets asked for location first with no mention of the doctor they named.
    # _search_doctors_flow (via _render_doctor_list) already asks for location ITSELF, and
    # only when actually needed to narrow a 10+ match — see Task 3. A pending specialty is
    # different: the symptom/specialty design always needs location before searching (Tasks
    # 4/5), so that still waits its turn in next_action()'s order below.
    if booking["doctor"]["status"] == "blank" and context.get("search_doctor_query"):
        if await _search_doctors_flow(client, phone, context, context.get("current_step")):
            return
        # Zero matches nationwide — say so and clear the abandoned query, same handling
        # the regular (non-first-message) hot-swap path already uses on a miss.
        await _handle_doctor_search_miss(client, phone, context, context["search_doctor_query"])
        return

    # A pending specialty/symptom match (set on the first message -- see handle_message's
    # NLU block) DOES need location before it can search, so it's correct for it to wait its
    # turn. But once its turn comes, next_action() below only knows the slot name is
    # "location" -- routing that through _step_for_action/_trigger_step_prompt would show the
    # generic location prompt, silently dropping the specialty/symptom the patient just named.
    # The has-lang-already version of this same flow (the spec_name/sym_name elif block in
    # handle_message) avoids this by sending the combined concern/enthusiasm + location-ask
    # message itself instead of a generic prompt -- do the same here before deferring to
    # next_action()'s generic slot walk.
    if booking["location"]["status"] == "blank" and context.get("pending_specialty"):
        specialty = context["pending_specialty"]
        template = (
            "symptom_concern_and_location_ask" if context.get("pending_specialty_is_symptom")
            else "specialty_enthusiasm_and_location_ask"
        )
        await _transition_to(phone, "choosing_location", context, context.get("current_step"))
        await whatsapp_client.send_location_request(
            client, phone, t(template, context.get("lang"), specialty=specialty),
        )
        return

    # Once a doctor is resolved, location no longer matters for anything downstream (slot
    # picking, patient details, confirm) — either it was already needed and filled to FIND
    # the doctor (the specialty/symptom path), or a name search resolved without it (the
    # branch above). SLOT_ORDER still lists location before doctor though, so without this,
    # next_action() would ask for it purely because of list position, on a name search that
    # never needed it. Set directly rather than via booking_slots.fill(), which would
    # cascade-invalidate the doctor just resolved — location "becoming known" is not really
    # what's happening here, it's becoming moot.
    if booking["doctor"]["status"] == "filled" and booking["location"]["status"] == "blank":
        booking["location"] = {"value": None, "raw": None, "candidates": [], "status": "filled", "source": "inferred"}

    action, slot_name = booking_slots.next_action(booking)
    if slot_name == "doctor" and booking["doctor"]["status"] == "blank":
        if context.get("pending_specialty"):
            specialty = context["pending_specialty"]
            # Clear pending_specialty from context to avoid double-processing,
            # _send_sort_prompt saves it in context["specialty_category"]
            context.pop("pending_specialty", None)
            await _send_sort_prompt(client, phone, context, specialty, context.get("current_step"))
            return
            
    next_step = _step_for_action(action, slot_name, context)
    current_step = context.get("current_step")
    context["booking"] = booking
    await _transition_to(phone, next_step, context, current_step)
    await _trigger_step_prompt(client, phone, next_step, context)


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
    # OPD QR check-in: a deterministic command from a QR scan (GET /c/{hospital_code} in
    # webhook.py), not natural language — intercepted before language detection/NLU/the
    # clipboard even initialize, same priority as "cancel"/"back" below but earlier, so it
    # never burns an NLU call or risks the casual-chat fallback swallowing it. Existing
    # conversation state (e.g. a booking in progress) is intentionally left untouched unless
    # the code is valid.
    if input_type == "text" and input_value.strip():
        checkin_match = _CHECKIN_TRIGGER_PATTERN.match(input_value.strip())
        if checkin_match:
            await _handle_checkin_trigger(client, phone, checkin_match.group(1), context, current_step)
            return

    booking = _get_or_create_clipboard(context)
    lang = context.get("lang")
    has_lang_init = lang is not None
    if input_type == "text" and input_value.strip() and has_lang_init:
        detected_lang, _ = _detect_language(input_value)
        if detected_lang and detected_lang != lang:
            logger.info("Auto-swapping language from %s to %s for user %s", lang, detected_lang, phone)
            lang = detected_lang
            context["lang"] = lang
            booking_slots.fill(booking, "lang", lang, source="user")
            if current_step:
                await db.save_conversation_state(phone, current_step, context)

    # Run safety interceptor triage immediately before any NLU or slot filling
    if input_type == "text" and input_value.strip():
        safety_alert = safety.check_safety_triage(input_value, lang or "en")
        if safety_alert and safety_alert.get("is_emergency"):
            await whatsapp_client.send_text(client, phone, safety_alert["alert_message"])
            return

    nlu_result = None
    raw_nlu_result = None
    if input_type == "text" and input_value.strip():
        try:
            # 1. Classify message using the new NLU client
            raw_nlu_result = await nlu_client.classify_message(client, input_value)
            logger.info("NLU Result: %s", raw_nlu_result)

            if not has_lang_init and raw_nlu_result and raw_nlu_result.get("intent") in (
                "book_appointment", "check_availability", "describe_symptom", "ask_pricing",
                "cancel_appointment", "reschedule_appointment", "change_selection"
            ):
                entities = raw_nlu_result.get("entities", {}) or {}
                doc_name = entities.get("doctor_name")
                spec_name = entities.get("specialty")
                sym_name = entities.get("symptom")
                pref_date = entities.get("datetime")
                time_of_day = entities.get("time_of_day")

                new_context = {**context}
                if pref_date:
                    new_context["preferred_date"] = pref_date
                if time_of_day:
                    new_context["time_of_day_hint"] = time_of_day

                if doc_name:
                    new_context["search_doctor_query"] = doc_name
                
                if spec_name:
                    categories = await hms_client.list_specialties()
                    category_list = [c["category"] for c in categories]
                    matched = symptom_client.match_category(spec_name, category_list)
                    if matched:
                        new_context["pending_specialty"] = matched
                        new_context["pending_specialty_is_symptom"] = False

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
                        new_context["pending_specialty_is_symptom"] = True

                await _confirm_or_start_language(client, phone, new_context, input_value, nlu_hint=raw_nlu_result)
                return
            
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
            routed = await intent_router.route_intent(phone, raw_nlu_result, input_value, lang, current_step)
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
                "confidence": routed.confidence,
                "doctor_name": routed.entities.get("doctor_name") or routed.entities.get("new_doctor_name"),
                "specialty": routed.entities.get("specialty"),
                "symptom": routed.entities.get("symptom"),
                "formatted_date": routed.entities.get("datetime"),
                "time_of_day": routed.entities.get("time_of_day"),
                "location": routed.entities.get("location"),
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
            await _handle_doctor_search_miss(client, phone, context, doc_name)
            return

    # Prioritize NLU global intents / shortcuts if confidence is high
    if nlu_result and nlu_result.get("confidence", 0.0) >= settings.nlu_confidence_threshold:
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
            
        elif intent in ("book_appointment", "check_availability", "describe_symptom"):
            # describe_symptom reuses this block's sym_name branch unchanged (below) — a
            # patient just describing a symptom gets the same "here are relevant doctors"
            # response book_appointment/check_availability already give when a symptom is
            # mentioned, rather than being silently dropped.
            doc_name = nlu_result.get("doctor_name")
            spec_name = nlu_result.get("specialty")
            sym_name = nlu_result.get("symptom")
            pref_date = nlu_result.get("formatted_date")
            time_of_day = nlu_result.get("time_of_day")

            new_context = {**context}
            if pref_date:
                new_context["preferred_date"] = pref_date
            if time_of_day:
                # Consumed once by _send_slot_options as an auto-select hint, then dropped —
                # see _pick_matching_slot below. Only useful alongside a date the offered-
                # slots fetch actually covers (today/tomorrow); harmless no-op otherwise.
                new_context["time_of_day_hint"] = time_of_day

            if doc_name:
                new_context["search_doctor_query"] = doc_name
                new_context.pop("pending_specialty", None)
                new_context.pop("pending_specialty_is_symptom", None)
                new_context.pop("search_symptom", None)
                
                has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                if new_context.get("lang") and has_loc:
                    await _transition_to(phone, "awaiting_doctor_name", new_context, current_step)
                    if await _search_doctors_flow(client, phone, new_context, "awaiting_doctor_name"):
                        return
                    await _handle_doctor_search_miss(client, phone, new_context, doc_name)
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
                    new_context["pending_specialty_is_symptom"] = False
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(client, phone, new_context, matched, current_step)
                        return
                    else:
                        if not new_context.get("lang"):
                            await _start(client, phone, new_context)
                        else:
                            # One personalised message (enthusiasm + the specialty named +
                            # the location ask) instead of a generic location prompt that
                            # never mentioned what the patient just said.
                            await _transition_to(phone, "choosing_location", new_context, current_step)
                            await whatsapp_client.send_location_request(
                                client, phone,
                                t("specialty_enthusiasm_and_location_ask", new_context.get("lang"), specialty=matched),
                            )
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
                    new_context["pending_specialty_is_symptom"] = True
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(client, phone, new_context, matched, current_step)
                        return
                    else:
                        if not new_context.get("lang"):
                            await _start(client, phone, new_context)
                        else:
                            # Same idea as the specialty branch above: concern + the
                            # specialty this looks like + the location ask, one message.
                            await _transition_to(phone, "choosing_location", new_context, current_step)
                            await whatsapp_client.send_location_request(
                                client, phone,
                                t("symptom_concern_and_location_ask", new_context.get("lang"), specialty=matched),
                            )
                        return

        elif intent == "provide_location":
            location_text = nlu_result.get("location")
            if location_text:
                new_context = {**context, "location_text": location_text}
                new_context = await _resolve_city(new_context)
                if "booking" in new_context:
                    booking = _get_or_create_clipboard(new_context)
                    if new_context.get("city"):
                        location_val = new_context["city"]
                        if new_context.get("patient_lat") is not None:
                            location_val = {"lat": new_context["patient_lat"], "lng": new_context["patient_lng"], "city": new_context.get("city")}
                        booking_slots.fill(booking, "location", location_val, raw=location_text, source="user")
                    else:
                        booking_slots.mark_notfound(booking, "location", raw=location_text)
                    await _advance_booking_flow(client, phone, new_context, booking)
                    return

                if new_context.get("search_doctor_query"):
                    if await _search_doctors_flow(client, phone, new_context, current_step):
                        return
                    query = new_context.get("search_doctor_query")
                    await whatsapp_client.send_text(client, phone, t("search_doctor_not_found", lang, query=query))
                    new_context.pop("search_doctor_query", None)
                await _send_search_mode_prompt(client, phone, new_context)
                return

        elif intent == "ask_pricing":
            doc_name = nlu_result.get("doctor_name")
            spec_name = nlu_result.get("specialty")

            if doc_name:
                all_docs = await city_index.get_all_doctors()
                resolution = resolve_doctor(
                    doc_name, all_docs,
                    city=context.get("city"),
                    patient_lat=context.get("patient_lat"), patient_lng=context.get("patient_lng"),
                )
                if resolution.status == "one":
                    fee = _doctor_fee(resolution.value)
                    name = resolution.value.get("fullName") or "Doctor"
                    if fee == float("inf"):
                        await whatsapp_client.send_text(client, phone, t("pricing_not_available", lang))
                    else:
                        await whatsapp_client.send_text(client, phone, t("pricing_doctor_fee", lang, doctor=name, fee=f"{fee:.0f}"))
                elif resolution.status == "many":
                    priced = [d for d in resolution.candidates if _doctor_fee(d) != float("inf")]
                    lines = "\n".join(
                        f"- {d.get('fullName') or 'Doctor'}: ₹{_doctor_fee(d):.0f}" for d in priced[:5]
                    )
                    if lines:
                        await whatsapp_client.send_text(client, phone, t("pricing_multiple_doctors", lang, query=doc_name, list=lines))
                    else:
                        await whatsapp_client.send_text(client, phone, t("pricing_not_available", lang))
                else:
                    await whatsapp_client.send_text(client, phone, t("search_doctor_not_found", lang, query=doc_name))
                return

            elif spec_name:
                categories = await hms_client.list_specialties()
                category_list = [c["category"] for c in categories]
                matched = symptom_client.match_category(spec_name, category_list)
                doctors = await hms_client.list_doctors(matched, page_size=50, city=context.get("city")) if matched else []
                fees = sorted({_doctor_fee(d) for d in doctors if _doctor_fee(d) != float("inf")})
                if fees:
                    await whatsapp_client.send_text(
                        client, phone,
                        t("pricing_specialty_range", lang, specialty=matched, min_fee=f"{fees[0]:.0f}", max_fee=f"{fees[-1]:.0f}"),
                    )
                else:
                    await whatsapp_client.send_text(client, phone, t("pricing_not_available", lang))
                return

            else:
                await whatsapp_client.send_text(client, phone, t("pricing_ask_which", lang))
                return

        elif intent == "change_selection":
            doc_name = nlu_result.get("doctor_name")
            if doc_name and current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
                context["search_doctor_query"] = doc_name
                await _transition_to(phone, "awaiting_doctor_name", context, current_step)
                if await _search_doctors_flow(client, phone, context, "awaiting_doctor_name"):
                    return
                await _handle_doctor_search_miss(client, phone, context, doc_name)
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
        if not nlu_result or nlu_result.get("intent") == "out_of_scope" or nlu_result.get("confidence", 0.0) < settings.nlu_confidence_threshold:
            if not has_entities:
                if current_step not in (
                    "awaiting_symptom", "awaiting_doctor_name", "awaiting_patient_details",
                    "checkin_awaiting_location", "checkin_choosing_appointment",
                ):
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
        elif current_step == "checkin_awaiting_location":
            await _handle_checkin_awaiting_location(client, phone, input_type, input_value, context)
        elif current_step == "checkin_choosing_appointment":
            await _handle_checkin_choosing_appointment(client, phone, input_type, input_value, context)
        else:
            # No state (new/returning user) or an unrecognized step — restart cleanly
            # rather than leave the conversation stuck.
            init_context = {}
            if input_type == "text" and input_value.strip():
                if _is_doctor_search_query(input_value):
                    init_context["search_doctor_query"] = input_value
                await _confirm_or_start_language(client, phone, init_context, input_value, nlu_hint=raw_nlu_result)
            else:
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


async def _confirm_or_start_language(
    client: httpx.AsyncClient, phone: str, context: dict, input_value: str, nlu_hint: dict | None = None,
) -> None:
    detected_lang, is_high_confidence = _detect_language(input_value) if input_value else (None, False)

    # Script-based detection (Devanagari/Bengali) above is already deterministic and free --
    # never second-guessed. Everything else is a keyword-overlap guess that's capped at
    # low-confidence by design (a handful of shared words is a weak signal on its own). Sarvam
    # already read this exact message for intent/entity extraction, so if it also offered a
    # confident language opinion, that's a free upgrade -- no extra API call, and it judges the
    # sentence as a whole rather than word-by-word.
    if not is_high_confidence and nlu_hint:
        sarvam_lang = nlu_hint.get("detected_language")
        sarvam_confidence = nlu_hint.get("language_confidence")
        if sarvam_lang:
            if sarvam_confidence == "high":
                detected_lang, is_high_confidence = sarvam_lang, True
            elif detected_lang is None:
                detected_lang = sarvam_lang

    if detected_lang:
        if is_high_confidence:
            context["lang"] = detected_lang
            booking = _get_or_create_clipboard(context)
            booking_slots.fill(booking, "lang", detected_lang, source="user")
            await whatsapp_client.send_text(client, phone, t("welcome_banner", detected_lang))
            await _advance_booking_flow(client, phone, context, booking)
        else:
            confirm_context = {
                **context,
                "guess_lang": detected_lang,
            }
            await _transition_to(phone, "confirming_language", confirm_context, None)
            await _trigger_step_prompt(client, phone, "confirming_language", confirm_context)
    else:
        await _start(client, phone, context)


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
    context["lang"] = lang
    booking = _get_or_create_clipboard(context)
    booking_slots.fill(booking, "lang", lang, source="user")
    await whatsapp_client.send_text(client, phone, t("greeting", lang))
    await _advance_booking_flow(client, phone, context, booking)


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
        context["lang"] = lang
        booking = _get_or_create_clipboard(context)
        booking_slots.fill(booking, "lang", lang, source="user")
        await _advance_booking_flow(client, phone, context, booking)
    elif choice == "lang_confirm_change":
        init_context = {}
        if "search_doctor_query" in context:
            init_context["search_doctor_query"] = context["search_doctor_query"]
        await db.clear_conversation_state(phone)
        init_context.pop("booking", None)
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
            new_ctx = {**context, "city": city}
            lat_lng = index[city][0] if index.get(city) else [None, None]
            if lat_lng[0] is not None:
                new_ctx["patient_lat"] = lat_lng[0]
                new_ctx["patient_lng"] = lat_lng[1]
            return new_ctx
        logger.info("Typed location %r matches no known city, searching unfiltered", typed)
    return context


async def _handle_choosing_location(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type == "location":
        lat_str, lng_str = input_value.split(",")
        context["patient_lat"] = float(lat_str)
        context["patient_lng"] = float(lng_str)
    elif input_type == "text" and input_value.strip():
        context["location_text"] = input_value.strip()
    else:
        await whatsapp_client.send_text(client, phone, t("location_prompt", lang))
        return
    context = await _resolve_city(context)

    booking = _get_or_create_clipboard(context)
    if context.get("city"):
        location_val = context["city"]
        if context.get("patient_lat") is not None:
            location_val = {"lat": context["patient_lat"], "lng": context["patient_lng"], "city": context.get("city")}
        booking_slots.fill(booking, "location", location_val, raw=context.get("location_text"), source="user")
    else:
        booking_slots.mark_notfound(booking, "location", raw=context.get("location_text"))

    await _advance_booking_flow(client, phone, context, booking)


# ---------------------------------------------------------------------------------------
# 4. Symptom vs. specialty entry (requirement 4) — this part already existed pre-redesign;
# kept as-is functionally, just moved behind language/person/location and translated.
# ---------------------------------------------------------------------------------------


async def _phrase(
    client: httpx.AsyncClient, step: str, context: dict, fallback_key: str, **fallback_kwargs
) -> str:
    """Model-written wording for a step's prompt, falling back to the i18n template.

    The template is always computed first, so an unavailable, slow, or failing model costs
    the patient nothing — they get the same message the bot has always sent. Only the
    *wording* is ever model-written: doctor names, fees, dates, slot labels and the
    confirmation summary are rendered separately from real data and never pass through
    here (nlu_client.STEP_GOALS deliberately has no entry for those steps).

    The clipboard's known_summary is what stops the model re-asking for something the
    patient already gave — it sees the collected details, not the raw context dict."""
    lang = context.get("lang")
    fallback = t(fallback_key, lang, **fallback_kwargs)
    try:
        booking = _get_or_create_clipboard(context)
        known = booking_slots.known_summary(booking)
        phrased = await nlu_client.generate_step_prompt(client, step, lang, known)
    except Exception:
        logger.warning("Step-prompt phrasing failed for %s, using template", step, exc_info=True)
        return fallback
    return phrased or fallback


async def _send_search_mode_prompt(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    # Only the question is phrased — the three button labels stay templated, since they
    # have to match the ids _handle_choosing_search_mode matches against.
    body = await _phrase(client, "choosing_search_mode", context, "search_mode_prompt")
    await whatsapp_client.send_buttons(
        client, phone, body,
        [
            ("symptom", t("search_mode_symptom", lang)),
            ("name", t("search_mode_name", lang)),
            ("browse", t("search_mode_browse", lang)),
        ],
    )
    await _transition_to(phone, "choosing_search_mode", context, "choosing_location")


# Fields _handle_choosing_doctor / _render_doctor_list write when a doctor is selected.
# All of them describe ONE doctor, so they have to be cleared together — leaving any behind
# after a failed re-search means context describes a doctor the patient is no longer
# choosing.
_DOCTOR_SELECTION_KEYS = (
    "doctor_id", "doctor_name", "doctor_fee",
    "hospital_name", "hospital_address", "hospital_city", "hospital_lat", "hospital_lng",
)


async def _handle_doctor_search_miss(
    client: httpx.AsyncClient, phone: str, context: dict, query: str
) -> None:
    await whatsapp_client.send_text(
        client, phone,
        await _phrase(client, "search_doctor_miss", context, "search_doctor_not_found", query=query),
    )
    context.pop("search_doctor_query", None)
    for key in _DOCTOR_SELECTION_KEYS:
        context.pop(key, None)

    booking = _get_or_create_clipboard(context)
    booking_slots.mark_notfound(booking, "doctor", raw=query)

    await _advance_booking_flow(client, phone, context, booking)


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
        await whatsapp_client.send_text(
            client, phone, await _phrase(client, "awaiting_symptom", context, "symptom_ask")
        )
        await _transition_to(phone, "awaiting_symptom", context, "choosing_search_mode")
        return
    if choice == "name":
        await whatsapp_client.send_text(
            client, phone, await _phrase(client, "awaiting_doctor_name", context, "doctor_name_ask")
        )
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
    distance = _doctor_distance_km(doctor, context.get("patient_lat"), context.get("patient_lng"))
    hosp = doctor.get("hospitalName") or doctor.get("city")
    hosp_cleaned = _clean_hospital(hosp)
    if hosp_cleaned:
        if distance != float("inf"):
            hosp_cleaned = f"{hosp_cleaned} ({distance:.0f}km)"
        parts.append(hosp_cleaned)
    elif distance != float("inf"):
        parts.append(f"{distance:.0f}km")

    if doctor.get("rating") is not None:
        parts.append(f"⭐{doctor['rating']}")
    fee = _doctor_fee(doctor)
    if fee != float("inf"):
        parts.append(f"₹{fee:.0f}")
    if doctor.get("experienceYears") is not None:
        parts.append(f"{doctor['experienceYears']}yrs")
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
        # Progressively wider bands, nearest first, stopping at the first non-empty result.
        for radius in settings.doctor_search_radii_km:
            doctors = await _fetch_doctors_near(
                specialty_category, context, radius, index, fetch_cache
            )
            if doctors:
                used_radius = radius
                break

        if not doctors:
            # Nothing within the widest configured band (50km by default) — tell the patient
            # and widen automatically to an unrestricted search, rather than asking
            # permission first. Product decision, not a technical default: see the design
            # flowchart's auto-widen branch, confirmed over the previous ask-first behaviour
            # (which still exists as _handle_confirming_wider_search / confirming_wider_search
            # but is no longer reachable from here).
            max_radius = settings.doctor_search_radii_km[-1]
            await whatsapp_client.send_text(
                client, phone,
                t("no_doctors_in_radius_widening", lang, specialty=specialty_category, radius=int(max_radius)),
            )
            doctors = await hms_client.list_doctors(specialty_category, page_size=50)
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

    # More matches than WhatsApp's list can show (10 rows) and no location to narrow by yet
    # — say so and ask, rather than silently showing only the first 10 with no indication
    # more exist. Only meaningful for the name-search path: the specialty/symptom path
    # already requires a location before it ever reaches this function.
    has_loc = context.get("patient_lat") is not None or context.get("city")
    if len(doctors) > 10 and not has_loc:
        query = context.get("search_doctor_query", "")
        await whatsapp_client.send_location_request(
            client, phone,
            t("doctor_too_many_ask_location", lang, count=len(doctors), query=query),
        )
        await _transition_to(phone, "choosing_location", context, current_step)
        return

    sorted_doctors = _sort_doctors(doctors, context)[:10]
    booking = _get_or_create_clipboard(context)
    
    if len(sorted_doctors) == 1:
        d = sorted_doctors[0]
        context["doctor_id"] = d["doctorId"]
        context["doctor_name"] = d.get("fullName") or "Doctor"
        context["doctor_fee"] = _doctor_fee(d)
        context["hospital_name"] = d.get("hospitalName") or ""
        context["hospital_address"] = d.get("address") or ""
        context["hospital_city"] = d.get("city") or ""
        context["hospital_lat"] = d.get("latitude")
        context["hospital_lng"] = d.get("longitude")
        context.pop("doctor_options", None)
        
        info_msg = t(
            "doctor_match_found_detailed", lang,
            doctor=context["doctor_name"], details=_doctor_row_description(d, context),
        )
        await whatsapp_client.send_text(client, phone, info_msg)
        booking_slots.fill(booking, "doctor", {"id": d["doctorId"], "fullName": context["doctor_name"]}, raw=context["doctor_name"], source="user")
        await _advance_booking_flow(client, phone, context, booking)
        return

    rows = [
        (d["doctorId"], d.get("fullName") or "Doctor", _doctor_row_description(d, context))
        for d in sorted_doctors
    ]
    await whatsapp_client.send_list(
        client, phone, t("doctor_list_prompt", lang), t("doctor_list_button", lang), rows, "Doctors"
    )
    context["doctor_options"] = {d["doctorId"]: d for d in sorted_doctors}
    booking_slots.mark_ambiguous(booking, "doctor", [d["doctorId"] for d in sorted_doctors], raw=context.get("search_doctor_query"))
    context["booking"] = booking
    
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

    context["doctor_id"] = doctor_id
    context["doctor_name"] = doctor.get("fullName") or "Doctor"
    context["doctor_fee"] = _doctor_fee(doctor)
    context["hospital_name"] = doctor.get("hospitalName") or ""
    context["hospital_address"] = doctor.get("address") or ""
    context["hospital_city"] = doctor.get("city") or ""
    context["hospital_lat"] = doctor.get("latitude")
    context["hospital_lng"] = doctor.get("longitude")
    context.pop("doctor_options", None)  # no longer needed, keep context_json lean

    booking = _get_or_create_clipboard(context)
    booking_slots.fill(booking, "doctor", {"id": doctor_id, "fullName": context["doctor_name"]}, raw=context["doctor_name"], source="user")

    await _advance_booking_flow(client, phone, context, booking)


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


def _pick_matching_slot(slots: list[dict], preferred_date: str | None, time_of_day: str | None) -> dict | None:
    """Auto-select a slot already implied by what the patient said (e.g. "kal subah") so
    they aren't asked to re-pick a shift they already named. Deliberately conservative:
    with no time_of_day there's nothing to match on, and any ambiguity (more than one
    surviving candidate) falls through to the normal button prompt rather than guessing —
    this only fires when there's exactly one slot consistent with what was said."""
    if not time_of_day:
        return None
    candidates = [s for s in slots if s["shift_name"] == time_of_day]
    if preferred_date:
        candidates = [s for s in candidates if s["date"].isoformat() == preferred_date]
    return candidates[0] if len(candidates) == 1 else None


async def _finalize_slot_selection(client: httpx.AsyncClient, phone: str, context: dict, selected_slot: dict) -> None:
    lang = context.get("lang")
    date_label = t("date_today", lang) if selected_slot["is_today"] else t("date_tomorrow", lang)
    # selected_slot["date"] is a real date object when this comes straight from
    # _get_offered_slots (the auto-match path in _send_slot_options), but an ISO string
    # when it comes from context["offered_slots"] (the manual-tap path in
    # _handle_choosing_slot, where it was already serialised for context_json). context
    # itself must only ever hold JSON-safe values, so normalise to a string here rather
    # than at each call site.
    raw_date = selected_slot["date"]
    preferred_date = raw_date.isoformat() if hasattr(raw_date, "isoformat") else raw_date
    context["preferred_date"] = preferred_date
    context["date_label"] = date_label
    context["shift_label"] = selected_slot["shift_name"]
    context.pop("offered_slots", None)
    context.pop("time_of_day_hint", None)

    booking = _get_or_create_clipboard(context)
    booking_slots.fill(booking, "date", preferred_date, raw=date_label, source="user")
    booking_slots.fill(booking, "shift", selected_slot["shift_name"], raw=selected_slot["shift_name"], source="user")

    await _advance_booking_flow(client, phone, context, booking)


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

    time_of_day_hint = context.get("time_of_day_hint")
    matched = _pick_matching_slot(slots, context.get("preferred_date"), time_of_day_hint)
    if matched:
        await _finalize_slot_selection(client, phone, context, matched)
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

    next_context = {**context, "offered_slots": offered_slots_data}
    # A hint that didn't produce a unique match (ambiguous, or no offered slot matched it)
    # shouldn't linger and silently auto-pick on some later, unrelated re-render.
    next_context.pop("time_of_day_hint", None)
    await _transition_to(phone, "choosing_slot", next_context, "choosing_doctor")


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

    await _finalize_slot_selection(client, phone, context, selected_slot)


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

    doctor_id = context.get("doctor_id")
    slots = []
    if doctor_id:
        try:
            slots = await _get_offered_slots(doctor_id, lang)
        except Exception as exc:
            logger.error("Failed to load offered slots for Flow: %s", exc)

    if not slots:
        await whatsapp_client.send_buttons(
            client, phone, t("today_shifts_over", lang),
            [("change_doctor", t("change_doctor_btn", lang))],
        )
        await _transition_to(phone, "choosing_doctor", context, context.get("current_step"))
        return

    # Reorder slots if a matching slot is resolved by time_of_day_hint
    time_of_day_hint = context.get("time_of_day_hint")
    matched = _pick_matching_slot(slots, context.get("preferred_date"), time_of_day_hint)
    if matched:
        slots = [matched] + [s for s in slots if s["button_id"] != matched["button_id"]]

    initial_data = {}
    choices = [
        {"id": s["button_id"], "title": s["label"]}
        for s in slots
    ]
    initial_data = {"slots": choices}

    offered_slots_data = [
        {
            "button_id": s["button_id"],
            "shift_name": s["shift_name"],
            "date": s["date"].isoformat() if hasattr(s["date"], "isoformat") else s["date"],
            "is_today": s["is_today"],
            "label": s["label"]
        }
        for s in slots
    ]
    context["offered_slots"] = offered_slots_data

    if flow_id:
        success = await whatsapp_client.send_flow(
            client,
            to=phone,
            body_text=t("patient_details_prompt_flow", lang),
            flow_id=flow_id,
            flow_cta=t("patient_details_flow_cta", lang),
            screen_id=settings.whatsapp_flow_screen_id,
            flow_token=f"token-{phone}",
            initial_data=initial_data,
        )
    if not success:
        await whatsapp_client.send_text(client, phone, t("patient_details_prompt_text", lang))
    await _transition_to(phone, "awaiting_patient_details", context, context.get("current_step"))


async def _handle_awaiting_patient_details(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    name, age, gender, guardian = None, None, None, None
    selected_slot = None

    if input_type == "nfm_reply":
        try:
            data = json.loads(input_value)
            name = (data.get("name") or "").strip()
            age = str(data.get("age") or "").strip()
            gender = (data.get("gender") or "").strip()
            guardian = (data.get("guardian") or "").strip()

            slot_id = data.get("slot_id") or data.get("slot")
            if slot_id:
                offered_slots = context.get("offered_slots") or []
                selected_slot = next((s for s in offered_slots if s["button_id"] == slot_id), None)
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

    context["patient_display_name"] = name
    context["patient_age"] = age
    context["patient_gender"] = gender
    context["patient_guardian"] = guardian

    booking = _get_or_create_clipboard(context)
    booking_slots.fill(booking, "patient", {
        "name": name,
        "age": age,
        "gender": gender,
        "guardian": guardian
    }, source="user")

    if selected_slot:
        preferred_date = selected_slot["date"]
        date_label = t("date_today", lang) if selected_slot["is_today"] else t("date_tomorrow", lang)
        context["preferred_date"] = preferred_date
        context["date_label"] = date_label
        context["shift_label"] = selected_slot["shift_name"]
        booking_slots.fill(booking, "date", preferred_date, raw=date_label, source="user")
        booking_slots.fill(booking, "shift", selected_slot["shift_name"], raw=selected_slot["shift_name"], source="user")

    await _advance_booking_flow(client, phone, context, booking)


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
    match = re.search(r'\b(?:dr\.?|doctor)[.,\s]*\s*([a-zA-Z]+)', normalized)
    if match:
        next_word = match.group(1)
        forbidden = {
            "btao", "chahiye", "dikhao", "dikhayein", "hai", "ho", "kr", "raha", "se", "milna",
            "ko", "me", "ka", "ki", "ke", "kya", "kuch", "appoint", "appointment", "book",
            "booking", "list", "search", "find", "with", "for", "an", "to", "please", "pls",
            "help", "consult", "hai", "tha", "thi", "hu", "hua", "gaya", "gayi", "liye"
        }
        if next_word not in forbidden:
            return True
    return False


async def _search_doctors_flow(client: httpx.AsyncClient, phone: str, context: dict, current_step: str) -> bool:
    """Performs a direct doctor search by name. If matches are found, it lists them.
    Returns True if we handled the flow by finding and showing matches, False otherwise."""
    query = context.get("search_doctor_query")
    if not query:
        return False

    lang = context.get("lang")
    try:
        all_docs = await city_index.get_all_doctors()
        index = await city_index.get_index()
    except Exception as exc:
        logger.error("Failed to fetch data for search: %s", exc)
        return False

    extracted_city, clean_query = extract_location_from_query(query, index)
    if extracted_city:
        logger.info("Extracted city %r from doctor query %r. Clean query: %r", extracted_city, query, clean_query)
        context["city"] = extracted_city
        lat_lng = index[extracted_city][0] if index.get(extracted_city) else [None, None]
        if lat_lng[0] is not None:
            context["patient_lat"] = lat_lng[0]
            context["patient_lng"] = lat_lng[1]
        else:
            context.pop("patient_lat", None)
            context.pop("patient_lng", None)
        booking = _get_or_create_clipboard(context)
        booking_slots.fill(booking, "location", extracted_city, raw=extracted_city, source="user")
        query = clean_query

    lat, lng = context.get("patient_lat"), context.get("patient_lng")
    city = context.get("city")

    resolution = resolve_doctor(query, all_docs, city=city, patient_lat=lat, patient_lng=lng)
    if resolution.status == "zero":
        return False

    await _render_doctor_list(client, phone, context, resolution.candidates, current_step)
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
        booking = _get_or_create_clipboard(context)
        if booking["location"]["status"] == "notfound":
            query = booking["location"]["raw"] or ""
            await whatsapp_client.send_text(client, phone, t("location_not_found", lang, query=query))
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
    elif step == "checkin_awaiting_location":
        await whatsapp_client.send_location_request(
            client, phone, t("checkin_location_prompt", lang, hospital_name=context.get("checkin_hospital_name"))
        )
    elif step == "checkin_choosing_appointment":
        rows = [
            (appt_id, c.get("doctorName") or "Doctor", c.get("startAt") or "")
            for appt_id, c in context.get("checkin_options", {}).items()
        ]
        if rows:
            await whatsapp_client.send_list(
                client, phone, t("checkin_choose_appointment", lang), t("checkin_choose_button", lang), rows,
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
    await _handle_doctor_search_miss(client, phone, context, input_value)


# ---------------------------------------------------------------------------------------
# OPD QR check-in — a separate, self-contained flow from booking above, entered only via the
# CHECKIN trigger in handle_message. Deliberately kept on its own current_step values rather
# than folded into booking_slots.py's clipboard model: the clipboard drives the *booking*
# flow's slot-filling, and check-in isn't a slot-filling problem (it's a fixed two-step
# location-share -> resolve sequence) -- reusing it here would mean teaching the clipboard
# about slots that only exist for a completely different flow.
# ---------------------------------------------------------------------------------------


async def _handle_checkin_trigger(
    client: httpx.AsyncClient, phone: str, hospital_code: str, context: dict, current_step: str | None
) -> None:
    # Re-resolves independently of GET /c/{hospital_code}'s own validation (app/webhook.py) —
    # that redirect is a UX nicety, never the authoritative check, since the prefilled text is
    # just a string the patient's WhatsApp client could technically let them edit before send.
    try:
        hospital = await hms_client.get_hospital_by_code(hospital_code)
    except HmsApiError:
        lang = context.get("lang")
        await whatsapp_client.send_text(client, phone, t("checkin_invalid_code", lang))
        return

    # Fresh context, not merged with whatever the patient was doing before (e.g. an
    # in-progress booking search) — check-in is an independent flow, and reusing stale keys
    # like doctor_options here would only risk cross-contamination.
    checkin_context = {
        "lang": context.get("lang"),
        "checkin_hospital_id": hospital["hospitalId"],
        "checkin_hospital_name": hospital.get("name") or "the hospital",
    }
    await _transition_to(phone, "checkin_awaiting_location", checkin_context, current_step)
    lang = checkin_context.get("lang")
    await whatsapp_client.send_location_request(
        client, phone, t("checkin_location_prompt", lang, hospital_name=checkin_context["checkin_hospital_name"])
    )


async def _handle_checkin_awaiting_location(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "location":
        await whatsapp_client.send_text(
            client, phone, t("checkin_location_prompt", lang, hospital_name=context.get("checkin_hospital_name"))
        )
        return

    lat_str, lng_str = input_value.split(",")
    latitude, longitude = float(lat_str), float(lng_str)
    hospital_id = context["checkin_hospital_id"]

    result = await hms_client.resolve_checkin(hospital_id, mobile=phone, latitude=latitude, longitude=longitude)

    if result.get("success") and result.get("appointmentId"):
        await _finish_checkin(client, phone, lang, result["appointmentId"], result.get("tokenNo"))
        return

    candidates = result.get("candidates")
    if candidates:
        checkin_options = {c["appointmentId"]: c for c in candidates}
        new_context = {
            **context,
            "checkin_latitude": latitude,
            "checkin_longitude": longitude,
            "checkin_options": checkin_options,
        }
        new_context.pop("booking", None)  # _get_or_create_clipboard's side effect, not needed here
        await _transition_to(phone, "checkin_choosing_appointment", new_context, "checkin_awaiting_location")
        rows = [
            (c["appointmentId"], c.get("doctorName") or "Doctor", c.get("startAt") or "")
            for c in candidates
        ]
        await whatsapp_client.send_list(
            client, phone, t("checkin_choose_appointment", lang), t("checkin_choose_button", lang), rows,
        )
        return

    # No candidates: either "too far" (geofence) or "nothing found today" — both are
    # success:false with no candidates, distinguished only by message text server-side.
    # There's no reliable machine-checkable signal to tell them apart here, so re-prompting
    # for location covers the geofence case (the more recoverable one) while the message
    # itself (not surfaced verbatim to avoid leaking backend wording) covers the other.
    message = result.get("message") or ""
    if "appointment" in message.lower():
        await whatsapp_client.send_text(client, phone, t("checkin_no_appointment", lang))
        await db.clear_conversation_state(phone)
    else:
        await whatsapp_client.send_text(client, phone, t("checkin_too_far", lang))


async def _handle_checkin_choosing_appointment(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, t("checkin_choose_appointment", lang))
        return

    appointment_id = input_value
    if appointment_id not in context.get("checkin_options", {}):
        # Stale list (e.g. patient tapped an old message) — same guard as _handle_choosing_doctor.
        await whatsapp_client.send_text(client, phone, t("checkin_choose_appointment", lang))
        return

    result = await hms_client.issue_queue_token(
        appointment_id, context["checkin_latitude"], context["checkin_longitude"]
    )

    if result.get("success"):
        await _finish_checkin(client, phone, lang, appointment_id, result.get("tokenNo"))
    else:
        await whatsapp_client.send_text(client, phone, t("checkin_failed", lang))
        await db.clear_conversation_state(phone)


async def _finish_checkin(client, phone, lang: str | None, appointment_id: str, token_no: int | None) -> None:
    # Registers this appointment into the same table POST /events/token-called reads from
    # (app/db.py:get_appointment_by_hms_id) — without this, a walk-in whose appointment wasn't
    # booked through this bot would check in successfully but never receive a queue-update
    # push, since that lookup only ever found bot-booked appointments before this call existed.
    await db.upsert_checkin_notification(appointment_id, phone, lang)
    await whatsapp_client.send_text(client, phone, t("checkin_success", lang, token_no=token_no))
    await db.clear_conversation_state(phone)
