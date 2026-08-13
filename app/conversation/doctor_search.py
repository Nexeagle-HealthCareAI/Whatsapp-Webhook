"""
app/conversation/doctor_search.py
------------------------------------
Doctor/hospital-name search domain: the query classifier, the direct doctor-
name search flow, its hospital-name-search fallback (Lead Generation
requirement 01), and the awaiting-doctor-name step handler.

Cross-references back into app/conversation/__init__.py (whatsapp_client, one
of the 9 mutated names; _get_or_create_clipboard, _advance_booking_flow,
_transition_to, _phrase, _render_doctor_list, still defined there) go through
a function-body-local `from app import conversation` + `conversation.<name>(...)`
-- see docs/architecture.md and app/conversation/checkin.py's module docstring.
Calls between functions that all live in this file
(_search_hospitals_flow <-> _resolve_hospital_search_match <->
_handle_choosing_hospital_from_search, _handle_awaiting_doctor_name ->
_search_doctors_flow/_handle_doctor_search_miss) stay as plain same-module
calls.
"""
import re

from app import city_index, hms_client
from app.i18n import t
from app.resolver import resolve_doctor, extract_location_from_query, match_hospital_by_query
from app import booking_slots


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


# Fields _handle_choosing_doctor / _render_doctor_list write when a doctor is selected.
# All of them describe ONE doctor, so they have to be cleared together — leaving any behind
# after a failed re-search means context describes a doctor the patient is no longer
# choosing.
_DOCTOR_SELECTION_KEYS = (
    "doctor_id", "doctor_name", "doctor_fee",
    "hospital_name", "hospital_address", "hospital_city", "hospital_lat", "hospital_lng",
)


async def _handle_doctor_search_miss(client, phone: str, context: dict, query: str) -> None:
    from app import conversation

    # Before giving up: this same free-text field is also where a patient might have typed a
    # HOSPITAL's name instead of a doctor's -- there's no separate "search by hospital" entry
    # point in the menu (see the Lead Generation requirement this satisfies). Only tried on a
    # doctor-search miss, never proactively on every message, so it can't second-guess an
    # already-successful doctor match.
    if await _search_hospitals_flow(client, phone, context, query, context.get("current_step")):
        return

    await conversation.whatsapp_client.send_text(
        client, phone,
        await conversation._phrase(client, "search_doctor_miss", context, "search_doctor_not_found", query=query),
    )
    context.pop("search_doctor_query", None)
    for key in _DOCTOR_SELECTION_KEYS:
        context.pop(key, None)

    booking = conversation._get_or_create_clipboard(context)
    booking_slots.mark_notfound(booking, "doctor", raw=query)

    await conversation._advance_booking_flow(client, phone, context, booking)


async def _search_doctors_flow(client, phone: str, context: dict, current_step: str) -> bool:
    """Performs a direct doctor search by name. If matches are found, it lists them.
    Returns True if we handled the flow by finding and showing matches, False otherwise."""
    from app import conversation

    query = context.get("search_doctor_query")
    if not query:
        return False

    lang = context.get("lang")
    try:
        all_docs = await city_index.get_all_doctors()
        index = await city_index.get_index()
    except Exception as exc:
        conversation.logger.error("Failed to fetch data for search: %s", exc)
        return False

    extracted_city, clean_query = extract_location_from_query(query, index)
    if extracted_city:
        conversation.logger.info("Extracted city %r from doctor query %r. Clean query: %r", extracted_city, query, clean_query)
        context["city"] = extracted_city
        lat_lng = index[extracted_city][0] if index.get(extracted_city) else [None, None]
        if lat_lng[0] is not None:
            context["patient_lat"] = lat_lng[0]
            context["patient_lng"] = lat_lng[1]
        else:
            context.pop("patient_lat", None)
            context.pop("patient_lng", None)
        booking = conversation._get_or_create_clipboard(context)
        booking_slots.fill(booking, "location", extracted_city, raw=extracted_city, source="user")
        query = clean_query

    lat, lng = context.get("patient_lat"), context.get("patient_lng")
    city = context.get("city")

    resolution = resolve_doctor(query, all_docs, city=city, patient_lat=lat, patient_lng=lng)
    if resolution.status == "zero":
        return False

    # Lead Generation (easyHMSWeb) -- a non-zero doctor-name-search result is a lead,
    # attributed to the resolved doctor's hospital. Logged HERE (this function is the
    # exclusive doctor-name-search entry point) rather than inside the shared
    # _render_doctor_list below, which _search_hospitals_flow also calls (via
    # _resolve_hospital_search_match) -- logging there would double/mis-attribute leads
    # that actually originated from a hospital-name search, not a doctor-name one.
    if resolution.status == "one":
        d = resolution.value
        if d.get("hospitalId"):
            await hms_client.record_lead(
                hospital_id=d["hospitalId"], doctor_id=d.get("doctorId"),
                lead_type="DoctorNameSearch", search_query=query, mobile=phone,
            )
    else:
        # Ambiguous match can span multiple hospitals (e.g. "Sharma" matching a Dr. Sharma at
        # each of two hospitals) -- one lead per distinct hospital represented, capped at 5,
        # same "don't flood on a broad query" reasoning as the NexEagleWebsite-side search lead.
        seen_hospital_ids: set[str] = set()
        for d in resolution.candidates:
            hospital_id = d.get("hospitalId")
            if not hospital_id or hospital_id in seen_hospital_ids:
                continue
            if len(seen_hospital_ids) >= 5:
                break
            seen_hospital_ids.add(hospital_id)
            await hms_client.record_lead(
                hospital_id=hospital_id, lead_type="DoctorNameSearch", search_query=query, mobile=phone,
            )

    await conversation._render_doctor_list(client, phone, context, resolution.candidates, current_step)
    return True


async def _search_hospitals_flow(
    client, phone: str, context: dict, query: str, current_step: str | None
) -> bool:
    """Fallback for a doctor-name-search miss (see _handle_doctor_search_miss): tries the same
    free text against hospital names instead -- there's no separate "search by hospital" menu
    entry point, this is the only way a patient reaches it. Returns True if it handled the flow
    (single match: records a lead and shows that hospital's doctors via the existing
    _render_doctor_list; multiple matches: sends a disambiguation list), False if nothing
    matched either (caller falls through to its own "not found" message)."""
    from app import conversation

    try:
        hospitals = await hms_client.list_hospitals()
    except Exception as exc:
        conversation.logger.error("Failed to fetch hospitals for search: %s", exc)
        return False

    matches = match_hospital_by_query(query, hospitals)
    if not matches:
        return False

    if len(matches) == 1:
        await _resolve_hospital_search_match(client, phone, context, matches[0], query, current_step)
        return True

    lang = context.get("lang")
    hospital_rows = matches[:10]
    context["hospital_options"] = {h["hospitalId"]: h for h in hospital_rows}
    context["hospital_search_query"] = query
    await conversation._transition_to(phone, "choosing_hospital_from_search", context, current_step)
    rows = [(h["hospitalId"], (h.get("name") or "Hospital")[:24], h.get("city") or "") for h in hospital_rows]
    await conversation.whatsapp_client.send_list(
        client, phone, t("choose_hospital_prompt", lang, query=query), t("choose_hospital_button", lang), rows,
    )
    return True


async def _resolve_hospital_search_match(
    client, phone: str, context: dict, hospital: dict, query: str, current_step: str | None,
) -> None:
    """A hospital-name search has resolved to exactly one hospital (either directly, or after
    disambiguation via _handle_choosing_hospital_from_search) -- record the lead, then show
    that hospital's doctors through the same _render_doctor_list a doctor-name search uses, so
    picking one continues into completely unchanged existing booking code."""
    from app import conversation

    lang = context.get("lang")
    hospital_id = hospital.get("hospitalId")
    await hms_client.record_lead(
        hospital_id=hospital_id, lead_type="HospitalNameSearch", search_query=query, mobile=phone,
    )

    try:
        doctors = await hms_client.list_doctors_at_hospital(hospital_id)
    except Exception as exc:
        conversation.logger.error("Failed to fetch doctors for hospital %s: %s", hospital_id, exc)
        await conversation.whatsapp_client.send_text(client, phone, t("search_doctor_not_found", lang, query=query))
        return

    if not doctors:
        await conversation.whatsapp_client.send_text(
            client, phone, t("hospital_no_doctors", lang, hospital=hospital.get("name") or "this hospital"),
        )
        return

    await conversation._render_doctor_list(client, phone, context, doctors, current_step)


async def _handle_choosing_hospital_from_search(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type != "list_reply":
        await conversation.whatsapp_client.send_text(client, phone, t("choose_hospital_hint", lang))
        return

    hospital_id = input_value
    hospital = context.get("hospital_options", {}).get(hospital_id)
    if hospital is None:
        # Stale list (e.g. patient tapped an old message) -- same "restart rather than book
        # with data we can no longer vouch for" posture as _handle_choosing_doctor.
        await conversation.whatsapp_client.send_text(client, phone, t("choose_hospital_hint", lang))
        return

    context.pop("hospital_options", None)
    query = context.pop("hospital_search_query", "")
    await _resolve_hospital_search_match(client, phone, context, hospital, query, "choosing_hospital_from_search")


async def _handle_awaiting_doctor_name(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type != "text" or not input_value.strip():
        await conversation.whatsapp_client.send_text(client, phone, t("doctor_name_text_required", lang))
        return

    context = {**context, "search_doctor_query": input_value.strip()}
    if await _search_doctors_flow(client, phone, context, "awaiting_doctor_name"):
        return
    await _handle_doctor_search_miss(client, phone, context, input_value)
