"""
app/conversation/location.py
------------------------------
Location capture: the location-request prompt, typed/GPS city resolution, and
the choosing_location step handler.

WhatsApp has no server-side "auto-detect without a tap" — send_location_request()'s
button opens the phone's native location picker, which defaults to sharing
current GPS in one tap; that's the real-world equivalent of "auto-detect" here.
A typed city/area name is accepted as a fallback for anyone who declines the
GPS prompt (handled in _handle_choosing_location below).

_safe_city_index is one of the 9 names the test suite reassigns directly, so it
stays defined in app/conversation/__init__.py, not here -- _resolve_city's call
to it, and every other cross-reference back into __init__.py (whatsapp_client/db,
_get_or_create_clipboard, _advance_booking_flow, _transition_to), goes through a
function-body-local `from app import conversation` + `conversation.<name>(...)`.
See docs/architecture.md and app/conversation/checkin.py's module docstring.
"""
from app import booking_slots, city_index
from app.i18n import t


async def _send_location_request(client, phone: str, context: dict) -> None:
    from app import conversation

    lang = context.get("lang")
    await conversation.whatsapp_client.send_location_request(client, phone, t("location_prompt", lang))
    await conversation.whatsapp_client.send_text(client, phone, t("location_manual_hint", lang))
    await conversation._transition_to(phone, "choosing_location", context, "choosing_language")


async def _resolve_city(context: dict) -> dict:
    """Works out which city name to hand to /public/doctors?city=.

    Only used for the typed-location path and as a fallback. When the patient shares GPS the
    search is driven by radius instead (see _fetch_doctors_near) — city names are too
    unreliable to decide results on, since the same town name can exist in more than one
    place and some records carry a city that disagrees with their own coordinates."""
    from app import conversation

    index = await conversation._safe_city_index()
    if not index:
        return context

    if context.get("patient_lat") is not None:
        city, distance_km = city_index.nearest_city(index, context["patient_lat"], context["patient_lng"])
        if city:
            conversation.logger.info("Resolved GPS to city %s (%.1f km)", city, distance_km)
            return {**context, "city": city, "city_distance_km": round(distance_km, 1)}
        return context

    typed = context.get("location_text")
    if typed:
        city = city_index.match_typed_city(index, typed)
        if city:
            conversation.logger.info("Matched typed location %r to city %s", typed, city)
            new_ctx = {**context, "city": city}
            lat_lng = index[city][0] if index.get(city) else [None, None]
            if lat_lng[0] is not None:
                new_ctx["patient_lat"] = lat_lng[0]
                new_ctx["patient_lng"] = lat_lng[1]
            return new_ctx
        conversation.logger.info("Typed location %r matches no known city, searching unfiltered", typed)
    return context


async def _handle_choosing_location(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type == "location":
        lat_str, lng_str = input_value.split(",")
        context["patient_lat"] = float(lat_str)
        context["patient_lng"] = float(lng_str)
    elif input_type == "text" and input_value.strip():
        context["location_text"] = input_value.strip()
    else:
        await conversation.whatsapp_client.send_text(client, phone, t("location_prompt", lang))
        return
    context = await _resolve_city(context)

    booking = conversation._get_or_create_clipboard(context)
    if context.get("city"):
        location_val = context["city"]
        if context.get("patient_lat") is not None:
            location_val = {"lat": context["patient_lat"], "lng": context["patient_lng"], "city": context.get("city")}
        booking_slots.fill(booking, "location", location_val, raw=context.get("location_text"), source="user")
    else:
        booking_slots.mark_notfound(booking, "location", raw=context.get("location_text"))

    await conversation._advance_booking_flow(client, phone, context, booking)
