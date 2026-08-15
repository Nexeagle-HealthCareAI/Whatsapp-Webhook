import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx

from app import db, i18n
from app.messengers import city_index, conversation_log_queue, hms_client, symptom_client, whatsapp_client
from app.messengers.redis_client import get_redis
from app.decision_maker import booking_slots
from app.decision_maker.resolver import match_doctor_by_query as _match_doctor_by_query
from app.decision_maker.resolver import resolve_doctor
from app.config import settings
from app.messengers.hms_client import HmsApiError
from app import nlu_client, safety
from app.referee import intent_router, flow_policy
from app.model_config import PRIMARY_NLU
from app.i18n import LANGUAGE_LABELS, LANG_PROMPT, t
from app.types import ConversationContext

# Re-exported from sibling modules purely so `conversation.<name>` keeps resolving --
# the test suite reaches every one of these through the package object, never via
# `from app.conversation.x import y`. Never monkeypatched (only ever called), so a plain
# static import here is safe -- see docs/architecture.md and the approved plan at
# ~/.claude/plans/expressive-seeking-lemon.md for which names DO get monkeypatched and
# why those must stay defined directly in this file instead.
from app.conversation.shared import _match_choice
from app.conversation.language import (
    _detect_language, _confirm_or_start_language, _start,
    _handle_choosing_language, _handle_confirming_language,
)
from app.conversation.location import _send_location_request, _resolve_city, _handle_choosing_location
from app.conversation.specialty_browsing import (
    _specialty_row, _groups_with_live_categories,
    _send_search_mode_prompt, _handle_choosing_search_mode, _handle_awaiting_symptom,
    _send_specialty_list, _handle_choosing_specialty_group, _handle_choosing_specialty,
    _send_sort_prompt, _handle_choosing_sort, resolve_specialty_category,
)
from app.conversation.doctor_list import (
    _doctor_fee, _doctor_rating, _doctor_distance_km, _sort_doctors,
    _clean_specialty, _clean_hospital, _doctor_row_description,
)
from app.conversation.slot_selection import (
    _parse_shift_end, _format_slot_label, _pick_matching_slot, _usable_shifts,
    _finalize_slot_selection, _send_slot_options, _handle_choosing_slot,
)
from app.conversation.patient_details import _parse_details, _looks_like_age
from app.conversation.booking_confirmation import _clinic_line, _patient_line
from app.conversation.doctor_search import (
    _is_doctor_search_query, _DOCTOR_SELECTION_KEYS,
    _handle_doctor_search_miss, _search_doctors_flow, _search_hospitals_flow,
    _resolve_hospital_search_match, _handle_choosing_hospital_from_search,
    _handle_awaiting_doctor_name,
)
from app.conversation.checkin import (
    _CHECKIN_TRIGGER_PATTERN, _DOCTOR_BOOKING_TRIGGER_PATTERN, _DOCUMENT_TRIGGERS,
    _DISCHARGE_TRIGGER_PATTERN, _PRESCRIPTION_TRIGGER_PATTERN, _VISIT_SUMMARY_TRIGGER_PATTERN,
    _handle_document_trigger, _handle_doctor_booking_trigger, _handle_checkin_trigger,
    _handle_checkin_awaiting_location, _handle_checkin_choosing_appointment, _finish_checkin,
)
from app.conversation.appointment_actions import (
    _start_appointment_action_flow,
    _handle_choosing_appointment_to_cancel, _handle_choosing_appointment_to_reschedule,
    _handle_confirming_appointment_cancel, _handle_confirming_appointment_reschedule,
    _prompt_appointment_choice, _prompt_appointment_confirm,
)

logger = logging.getLogger("conversation")


# Kept for anything importing the old constant name (e.g. tests) — superseded by
# i18n.LANG_PROMPT as the very first message now, since language is asked before anything
# else. See _start() below.
GREETING_TEXT = "Hi! I can help you book a doctor's appointment."

_SORT_OPTIONS = ["rating", "nearest", "experience", "fee"]

# ---------------------------------------------------------------------------------------
# Clipboard Initialization from Legacy Context
#
# Helper function to initialize the new clipboard data structure from existing legacy
# session parameters for backwards compatibility.
# ---------------------------------------------------------------------------------------

def _init_clipboard_from_legacy(context: ConversationContext) -> dict:
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


def _get_or_create_clipboard(context: ConversationContext) -> dict:
    if "booking" in context and isinstance(context["booking"], dict):
        return context["booking"]
    slots = _init_clipboard_from_legacy(context)
    context["booking"] = slots
    return slots


def _step_for_action(action: str, slot_name: str | None, context: ConversationContext) -> str:
    if action == "ask" and slot_name == "lang":
        return "choosing_language"
    elif action == "disambiguate" and slot_name == "lang":
        return "confirming_language"
    elif action == "ask" and slot_name == "location":
        return "choosing_location"
    elif action == "disambiguate" and slot_name == "location":
        return "choosing_location"
    elif action == "retry" and slot_name == "location":
        # Falling through to the default ("choosing_search_mode") below would silently
        # accept the unresolved location and move on -- the patient never learns their city
        # didn't match, and whatever later needs a location (a symptom/specialty search)
        # ends up asking for it again out of nowhere, reading as a duplicate ask. Routing
        # back to choosing_location lets _trigger_step_prompt's own "notfound" branch send
        # the "couldn't find that city" message before re-asking, which it already knows
        # how to do -- it just wasn't being reached.
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


async def _advance_booking_flow(client: httpx.AsyncClient, phone: str, context: ConversationContext, booking: dict) -> None:
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
    # Assigned here (not only in language.py's _start) so even a brand-new patient's very
    # first message -- before language selection has run at all -- has a session_id to log
    # under. See app/messengers/conversation_log_queue.py for what this feeds.
    context.setdefault("session_id", str(uuid4()))
    await conversation_log_queue.log_event(
        get_redis(), context["session_id"], phone, "in", input_type, input_value, current_step
    )
    # OPD QR check-in / discharge-summary & prescription QR pull / per-doctor booking QR:
    # deterministic commands from a QR scan (GET /c, /d, /rx, /rxv, /doc in webhook.py), not
    # natural language — intercepted before language detection/NLU/the clipboard even
    # initialize, same priority as "cancel"/"back" below but earlier, so it never burns an
    # NLU call or risks the casual-chat fallback swallowing it. Existing conversation state
    # (e.g. a booking in progress) is intentionally left untouched unless the code is valid.
    if input_type == "text" and input_value.strip():
        stripped_input = input_value.strip()
        checkin_match = _CHECKIN_TRIGGER_PATTERN.match(stripped_input)
        if checkin_match:
            await _handle_checkin_trigger(client, phone, checkin_match.group(1), context, current_step)
            return

        doctor_booking_match = _DOCTOR_BOOKING_TRIGGER_PATTERN.match(stripped_input)
        if doctor_booking_match:
            await _handle_doctor_booking_trigger(client, phone, doctor_booking_match.group(1), context)
            return

        for pattern, resolver_name, filename, not_available_key, delivered_key in _DOCUMENT_TRIGGERS:
            document_match = pattern.match(stripped_input)
            if document_match:
                await _handle_document_trigger(
                    client, phone, document_match.group(1), context,
                    resolver_name, filename, not_available_key, delivered_key,
                )
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
                    matched = await resolve_specialty_category(client, spec_name, category_list)
                    if matched:
                        new_context["pending_specialty"] = matched
                        new_context["pending_specialty_is_symptom"] = False

                elif sym_name:
                    labels = await symptom_client.route_symptom(sym_name)
                    categories = await hms_client.list_specialties()
                    category_list = [c["category"] for c in categories]
                    matched = None
                    for label in labels:
                        matched = await resolve_specialty_category(client, label, category_list)
                        if matched:
                            break
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
            #
            # A global intent (cancel/back/greeting) on THIS message must always be honoured,
            # even if intent_router has an in-progress multi-turn session (e.g. it's mid-way
            # through asking "book new or reschedule?") -- see app/flow_policy.py for why: that
            # session's own local logic has no notion of "these intents always work", so a bare
            # "hi" or "cancel" arriving while it's active used to get silently swallowed and
            # replayed the same follow-up question forever. Clearing the session first means
            # route_intent() finds nothing to merge against and routes this intent normally,
            # straight through to the "Prioritize NLU global intents" handling below.
            if flow_policy.is_global_override(raw_nlu_result.get("intent"), raw_nlu_result.get("confidence")):
                await intent_router.clear_session(phone)

            routed = await intent_router.route_intent(phone, raw_nlu_result, input_value, lang, current_step)
            logger.info("NLU Router Result: %s", routed)
            
            if routed.action == "ask_followup":
                if routed.intent == "change_selection" and current_step in ("choosing_doctor", "choosing_slot", "awaiting_doctor_name"):
                    await _transition_to(phone, "awaiting_doctor_name", context, current_step)
                    await whatsapp_client.send_text(client, phone, t("doctor_name_ask", lang or "en"))
                else:
                    await whatsapp_client.send_text(client, phone, routed.followup_prompt)
                return

            if routed.action == "error":
                # intent_router couldn't safely verify this patient doesn't already have an
                # active appointment (see its own comment) -- rather than silently letting a
                # booking through unchecked, tell the patient and stop here.
                await whatsapp_client.send_text(client, phone, t("error_hms", lang or "en"))
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
            # Resolved via DB lookup, not slot-filling (see intent_router.REQUIRED_ENTITIES) --
            # finds this phone's live booked appointment(s) and asks for confirmation before
            # actually cancelling anything real. Previously this just cleared local chat state
            # and said "cancelled" without touching a real appointment at all.
            await _start_appointment_action_flow(client, phone, context, current_step, action="cancel")
            return

        elif intent == "reschedule_appointment":
            # datetime is a required slot (intent_router.REQUIRED_ENTITIES) -- by the time this
            # branch is reached, formatted_date is already a resolved "YYYY-MM-DD" string, same
            # as book_appointment's preferred_date.
            await _start_appointment_action_flow(
                client, phone, context, current_step, action="reschedule",
                new_date_str=nlu_result.get("formatted_date"),
            )
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
                matched = await resolve_specialty_category(client, spec_name, category_list)
                if matched:
                    new_context["pending_specialty"] = matched
                    new_context["pending_specialty_is_symptom"] = False
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(
                            client, phone, new_context, matched, current_step,
                            concern_prefix=t("specialty_enthusiasm_only", new_context.get("lang"), specialty=matched),
                        )
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
                matched = None
                for label in labels:
                    matched = await resolve_specialty_category(client, label, category_list)
                    if matched:
                        break
                if matched:
                    new_context["pending_specialty"] = matched
                    new_context["pending_specialty_is_symptom"] = True
                    has_loc = new_context.get("city") or (new_context.get("patient_lat") is not None and new_context.get("patient_lng") is not None)
                    if new_context.get("lang") and has_loc:
                        await _send_sort_prompt(
                            client, phone, new_context, matched, current_step,
                            concern_prefix=t("symptom_concern_only", new_context.get("lang"), specialty=matched),
                        )
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
                matched = await resolve_specialty_category(client, spec_name, category_list)
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
        step_config = STEP_REGISTRY.get(current_step)
        if step_config:
            handler = step_config["handler"]
            if current_step == "confirming":
                await handler(client, phone, sender_name, input_type, input_value, context)
            else:
                await handler(client, phone, input_type, input_value, context)
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


async def _safe_city_index() -> dict:
    """The city index, or an empty dict if it can't be built. Every caller treats empty as
    "fall back to a plain city-name search" rather than an error — an unreachable index
    should degrade the search, not break the conversation."""
    try:
        return await city_index.get_index()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("City index unavailable: %s", exc)
        return {}


# ---------------------------------------------------------------------------------------
# 4. Symptom vs. specialty entry (requirement 4) — this part already existed pre-redesign;
# kept as-is functionally, just moved behind language/person/location and translated.
# ---------------------------------------------------------------------------------------


async def _phrase(
    client: httpx.AsyncClient, step: str, context: ConversationContext, fallback_key: str, **fallback_kwargs
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


def _clinic_now() -> datetime:
    """Current time where the clinics actually are. Never use datetime.now() bare in this
    file — the container runs on UTC (see settings.clinic_timezone)."""
    return datetime.now(ZoneInfo(settings.clinic_timezone))


async def _fetch_doctors_near(
    specialty_category: str, context: ConversationContext, radius_km: float, index: dict, cache: dict
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


async def _send_doctor_list(client: httpx.AsyncClient, phone: str, context: ConversationContext) -> None:
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
    client: httpx.AsyncClient, phone: str, context: ConversationContext, doctors: list[dict], current_step: str | None = None
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
        # Direct send, not queued: _advance_booking_flow below sends the patient-details
        # Flow prompt directly too (send_flow bypasses the queue), so a queued send here
        # can lose the race and arrive after it — see send_text_direct's docstring.
        await whatsapp_client.send_text_direct(client, phone, info_msg)
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


async def _send_patient_details_flow(client: httpx.AsyncClient, phone: str, context: ConversationContext) -> None:
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
        else:
            # Guardian is optional (matches the Flow form, where it's an optional field) --
            # accept "Name, Age, Gender" with no 4th part too.
            parsed = _parse_details(input_value, 3)
            if parsed:
                name, age, gender = parsed
                guardian = ""

    if not name or not age or not gender:
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
    await conversation_log_queue.log_conversion(get_redis(), context.get("session_id"), str(row_id))

    await whatsapp_client.send_text(client, phone, t("booked_success", lang, patient_name=patient_name))

    # Mark NLU correctness based on final booked doctor matching extracted NLU name
    final_doctor_name = context.get("doctor_name")
    if final_doctor_name and hasattr(db, "mark_session_nlu_correctness_on_booking"):
        await db.mark_session_nlu_correctness_on_booking(phone, final_doctor_name)

    await db.clear_conversation_state(phone)


async def _transition_to(phone: str, next_step: str, context: ConversationContext, current_step: str | None) -> None:
    history = context.get("_history", [])
    if current_step:
        clean_context = {k: v for k, v in context.items() if k != "_history"}
        history = list(history) + [{"current_step": current_step, "context": clean_context}]
        if len(history) > 10:
            history.pop(0)
    new_context = {**context, "_history": history}
    await db.save_conversation_state(phone, next_step, new_context)
    # Every step change in the whole conversation goes through this one function, which is
    # what makes it the single hook needed to capture the bot's side of the journey -- see
    # app/messengers/conversation_log_queue.py's module docstring for why step-level is
    # logged here instead of every individual whatsapp_client send call.
    await conversation_log_queue.log_event(
        get_redis(), context.get("session_id"), phone, "out", "step", next_step, next_step
    )


async def _trigger_step_prompt(client: httpx.AsyncClient, phone: str, step: str, context: ConversationContext) -> None:
    step_config = STEP_REGISTRY.get(step)
    if step_config and "prompt" in step_config:
        try:
            await step_config["prompt"](client, phone, context)
        except Exception as exc:
            logger.error("Failed to trigger step prompt for %s: %s", step, exc)
            await _start(client, phone)
    else:
        logger.warning("Unrecognized step prompt %s, falling back to _start", step)
        await _start(client, phone)


# ─────────────────────────────────────────────────────────────────────────────
# Cancel / reschedule an EXISTING, already-booked appointment now lives in
# app/conversation/appointment_actions.py -- extracted out of this file (SOLID rebuild,
# Layer 4 review). _start_appointment_action_flow is still called directly from
# handle_message above; the rest is only reached via STEP_REGISTRY below.
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Central Step Prompt Helpers and Registry (OCP Dispatch Consolidation)
# ─────────────────────────────────────────────────────────────────────────────

async def _prompt_confirming_language(client, phone, context):
    guess_lang = context.get("guess_lang", "en")
    prompt = t("confirm_lang_prompt", guess_lang)
    buttons = [
        ("lang_confirm_yes", t("confirm_yes", guess_lang)),
        ("lang_confirm_change", t("confirm_change", guess_lang))
    ]
    await whatsapp_client.send_buttons(client, phone, prompt, buttons)


async def _prompt_choosing_location(client, phone, context):
    booking = _get_or_create_clipboard(context)
    if booking["location"]["status"] == "notfound":
        query = booking["location"]["raw"] or ""
        await whatsapp_client.send_text(client, phone, t("location_not_found", context.get("lang"), query=query))
    await _send_location_request(client, phone, context)


async def _prompt_awaiting_symptom(client, phone, context):
    await whatsapp_client.send_text(client, phone, t("symptom_ask", context.get("lang")))


async def _prompt_awaiting_doctor_name(client, phone, context):
    await whatsapp_client.send_text(client, phone, t("doctor_name_ask", context.get("lang")))


async def _prompt_choosing_specialty(client, phone, context):
    lang = context.get("lang")
    group_members = context.get("specialty_groups", {})
    rows = [_specialty_row(s) for s in group_members.values()]
    await whatsapp_client.send_list(
        client, phone, t("specialty_list_prompt", lang), t("specialty_list_button", lang),
        rows, t("specialty_group_section", lang),
    )


async def _prompt_choosing_sort(client, phone, context):
    await _send_sort_prompt(client, phone, context, context.get("specialty_category"))


async def _prompt_choosing_doctor(client, phone, context):
    docs = list(context.get("doctor_options", {}).values())
    if docs:
        await _render_doctor_list(client, phone, context, docs)
    else:
        await _send_search_mode_prompt(client, phone, context)


async def _prompt_confirming(client, phone, context):
    lang = context.get("lang")
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


async def _prompt_checkin_location(client, phone, context):
    await whatsapp_client.send_location_request(
        client, phone, t("checkin_location_prompt", context.get("lang"), hospital_name=context.get("checkin_hospital_name"))
    )


async def _prompt_checkin_appointment(client, phone, context):
    lang = context.get("lang")
    rows = [
        (appt_id, c.get("doctorName") or "Doctor", c.get("startAt") or "")
        for appt_id, c in context.get("checkin_options", {}).items()
    ]
    if rows:
        await whatsapp_client.send_list(
            client, phone, t("checkin_choose_appointment", lang), t("checkin_choose_button", lang), rows,
        )


STEP_REGISTRY = {
    "choosing_language": {
        "handler": _handle_choosing_language,
        "prompt": lambda client, phone, context: _start(client, phone),
    },
    "confirming_language": {
        "handler": _handle_confirming_language,
        "prompt": _prompt_confirming_language,
    },
    "choosing_location": {
        "handler": _handle_choosing_location,
        "prompt": _prompt_choosing_location,
    },
    "choosing_search_mode": {
        "handler": _handle_choosing_search_mode,
        "prompt": _send_search_mode_prompt,
    },
    "awaiting_symptom": {
        "handler": _handle_awaiting_symptom,
        "prompt": _prompt_awaiting_symptom,
    },
    "awaiting_doctor_name": {
        "handler": _handle_awaiting_doctor_name,
        "prompt": _prompt_awaiting_doctor_name,
    },
    "choosing_specialty_group": {
        "handler": _handle_choosing_specialty_group,
        "prompt": _send_specialty_list,
    },
    "choosing_specialty": {
        "handler": _handle_choosing_specialty,
        "prompt": _prompt_choosing_specialty,
    },
    "choosing_sort": {
        "handler": _handle_choosing_sort,
        "prompt": _prompt_choosing_sort,
    },
    "confirming_wider_search": {
        "handler": _handle_confirming_wider_search,
        "prompt": lambda client, phone, context: None,
    },
    "choosing_doctor": {
        "handler": _handle_choosing_doctor,
        "prompt": _prompt_choosing_doctor,
    },
    "choosing_hospital_from_search": {
        "handler": _handle_choosing_hospital_from_search,
        "prompt": lambda client, phone, context: _start(client, phone),
    },
    "choosing_slot": {
        "handler": _handle_choosing_slot,
        "prompt": _send_slot_options,
    },
    "awaiting_patient_details": {
        "handler": _handle_awaiting_patient_details,
        "prompt": _send_patient_details_flow,
    },
    "confirming": {
        "handler": _handle_confirming,
        "prompt": _prompt_confirming,
    },
    "choosing_appointment_to_cancel": {
        "handler": _handle_choosing_appointment_to_cancel,
        "prompt": _prompt_appointment_choice,
    },
    "choosing_appointment_to_reschedule": {
        "handler": _handle_choosing_appointment_to_reschedule,
        "prompt": _prompt_appointment_choice,
    },
    "confirming_appointment_cancel": {
        "handler": _handle_confirming_appointment_cancel,
        "prompt": _prompt_appointment_confirm,
    },
    "confirming_appointment_reschedule": {
        "handler": _handle_confirming_appointment_reschedule,
        "prompt": _prompt_appointment_confirm,
    },
    "checkin_awaiting_location": {
        "handler": _handle_checkin_awaiting_location,
        "prompt": _prompt_checkin_location,
    },
    "checkin_choosing_appointment": {
        "handler": _handle_checkin_choosing_appointment,
        "prompt": _prompt_checkin_appointment,
    },
}
