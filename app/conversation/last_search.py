"""
app/conversation/last_search.py
----------------------------------
Reuse-last-location-and-specialty flow: offered on a "Book Appointment" tap when both were
given within the last 24 hours (see app/db/patient_last_search.py). Deliberately
confirm-first, never silent -- a shared family WhatsApp number means the last search on
record might not even be the same person, so this always asks "still looking for the same
thing?" before acting on it.

Cross-references back into app/conversation/__init__.py (whatsapp_client and db, two of the
9 names the test suite reassigns directly; _advance_booking_flow, _send_doctor_list,
_transition_to, core-orchestration functions that still live there) go through a
function-body-local `from app import conversation` + `conversation.<name>(...)` -- see
docs/architecture.md and app/conversation/checkin.py's module docstring. booking_slots is
safe to import directly at module level -- never reassigned wholesale in tests, same as
hms_client.
"""
from app.decision_maker import booking_slots
from app.conversation.shared import _match_choice
from app.i18n import t
from app.types import ConversationContext

_REUSE_CHOICES = ("reuse_yes", "reuse_change_location", "reuse_change_specialty")


async def _prompt_confirming_last_search(client, phone, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    specialty = context.get("last_search_specialty")
    city = context.get("last_search_city") or context.get("last_search_location_text") or ""
    prompt = t("reuse_last_search_prompt", lang, specialty=specialty, city=city)
    buttons = [
        ("reuse_yes", t("reuse_last_search_yes_btn", lang)),
        ("reuse_change_location", t("reuse_last_search_change_location_btn", lang)),
        ("reuse_change_specialty", t("reuse_last_search_change_specialty_btn", lang)),
    ]
    await conversation.whatsapp_client.send_buttons(client, phone, prompt, buttons)


async def _handle_confirming_last_search(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, list(_REUSE_CHOICES))
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("reuse_last_search_choose_hint", lang))
        return

    specialty = context.get("last_search_specialty")
    city = context.get("last_search_city")
    location_text = context.get("last_search_location_text")
    lat = context.get("last_search_lat")
    lng = context.get("last_search_lng")

    if choice == "reuse_yes":
        # Skips choosing_location AND choosing_sort entirely -- that's the whole point of
        # confirming reuse in one tap. _send_doctor_list re-fetches live (fee/availability/
        # roster can have changed in 24h), it never replays a cached list.
        new_context: ConversationContext = {
            "lang": lang, "session_id": context.get("session_id"),
            "specialty_category": specialty,
            "sort_key": "nearest" if lat is not None else "rating",
        }
        if lat is not None:
            new_context["patient_lat"] = lat
            new_context["patient_lng"] = lng
        if city:
            new_context["city"] = city
        if location_text:
            new_context["location_text"] = location_text
        await conversation._send_doctor_list(client, phone, new_context)
        return

    if choice == "reuse_change_location":
        # Reuses the exact mechanism _advance_booking_flow already has for "specialty known
        # before location" on a first message (booking["location"]["status"] == "blank" and
        # context["pending_specialty"] set) -- no new step/handler needed for this branch.
        booking = booking_slots.empty()
        booking_slots.fill(booking, "lang", lang, source="user")
        new_context = {
            "lang": lang, "booking": booking, "session_id": context.get("session_id"),
            "pending_specialty": specialty,
        }
        await conversation._advance_booking_flow(client, phone, new_context, booking)
        return

    # reuse_change_specialty: location pre-filled, no pending_specialty -- next_action()
    # lands on "doctor" with nothing more specific to match, which _step_for_action defaults
    # to choosing_search_mode -- the same symptom/name/browse menu used everywhere else, just
    # with location already known.
    booking = booking_slots.empty()
    booking_slots.fill(booking, "lang", lang, source="user")
    location_val = {"lat": lat, "lng": lng, "city": city} if lat is not None else city
    booking_slots.fill(booking, "location", location_val, raw=location_text, source="user")
    new_context = {
        "lang": lang, "booking": booking, "session_id": context.get("session_id"),
        "city": city, "location_text": location_text,
    }
    if lat is not None:
        new_context["patient_lat"] = lat
        new_context["patient_lng"] = lng
    await conversation._advance_booking_flow(client, phone, new_context, booking)
