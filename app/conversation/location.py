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

Typed text is resolved via app.messengers.location_client (a real geocoder, up to 5
candidates) rather than the old city_index-only match, which could only ever match a
city that already has a doctor registered in 1HMS and only ever returned one guess or
nothing. 0 matches -> not found (same as before). 1 match -> resolved directly. 2+ ->
booking_slots marks the "location" slot ambiguous and _prompt_choosing_location (in
__init__.py) shows a list instead of the GPS/type-city prompt, same disambiguation shape
already used for doctor/hospital name matches. If the API call itself fails (network,
timeout, non-2xx), _resolve_city falls back to the old local city_index match rather than
breaking location capture entirely.

_safe_city_index is one of the 9 names the test suite reassigns directly, so it
stays defined in app/conversation/__init__.py, not here -- _resolve_city's call
to it, and every other cross-reference back into __init__.py (whatsapp_client/db,
_get_or_create_clipboard, _advance_booking_flow, _transition_to), goes through a
function-body-local `from app import conversation` + `conversation.<name>(...)`.
See docs/architecture.md and app/conversation/checkin.py's module docstring.
"""
from app.messengers import city_index, location_client
from app.decision_maker import booking_slots
from app.i18n import t
from app.types import ConversationContext


async def _send_location_request(client, phone: str, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    await conversation.whatsapp_client.send_location_request(client, phone, t("location_prompt", lang))
    await conversation.whatsapp_client.send_text(client, phone, t("location_manual_hint", lang))
    await conversation._transition_to(phone, "choosing_location", context, "choosing_language")


def _location_row(idx: str, match: dict) -> tuple[str, str, str]:
    name = match.get("name") or "Unknown"
    parts = [p for p in (match.get("type", "").capitalize(), match.get("state")) if p]
    return (idx, name[:24], " · ".join(parts)[:72])


async def _send_location_match_list(client, phone: str, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    booking = conversation._get_or_create_clipboard(context)
    query = booking["location"]["raw"] or ""
    options: dict = context.get("location_options", {})
    rows = [_location_row(idx, m) for idx, m in options.items()]
    await conversation.whatsapp_client.send_list(
        client, phone, t("location_match_list_prompt", lang, query=query),
        t("location_match_list_button", lang), rows, "Places",
    )


def _apply_single_match(context: ConversationContext, match: dict) -> ConversationContext:
    new_ctx = {**context, "city": match.get("name")}
    coords = match.get("coordinates")
    if coords and coords.get("latitude") is not None:
        new_ctx["patient_lat"] = coords["latitude"]
        new_ctx["patient_lng"] = coords["longitude"]
    return new_ctx


async def _resolve_city(context: ConversationContext) -> ConversationContext:
    """Works out which city name (and, where possible, coordinates) to use for the doctor
    search. GPS stays purely local (nearest_city against the doctor-derived index) — no
    reason to call an external API when we already have exact coordinates. Typed text goes
    through the real geocoder first; see the module docstring for the 0/1/N handling and the
    local-index fallback on API failure.

    A result with `location_options` set means: ambiguous, don't fill the slot yet — the
    caller must show the list and wait for a pick, same as an ambiguous doctor/hospital name."""
    from app import conversation

    if context.get("patient_lat") is not None:
        index = await conversation._safe_city_index()
        if not index:
            return context
        city, distance_km = city_index.nearest_city(index, context["patient_lat"], context["patient_lng"])
        if city:
            conversation.logger.info("Resolved GPS to city %s (%.1f km)", city, distance_km)
            return {**context, "city": city, "city_distance_km": round(distance_km, 1)}
        return context

    typed = context.get("location_text")
    if not typed:
        return context

    try:
        matches = await location_client.search_locations(typed, limit=5)
    except Exception as exc:
        conversation.logger.warning(
            "location_client.search_locations failed for %r, falling back to local city index: %s", typed, exc
        )
        matches = None

    if matches:
        if len(matches) == 1:
            conversation.logger.info("Matched typed location %r to a single place: %s", typed, matches[0].get("name"))
            return _apply_single_match(context, matches[0])
        conversation.logger.info("Typed location %r matched %d places, asking patient to pick", typed, len(matches))
        return {**context, "location_options": {str(i): m for i, m in enumerate(matches)}}

    if matches == []:
        # The geocoder ran and genuinely found nothing -- it's a superset of the local
        # doctor-derived index, so falling back to that wouldn't find anything either.
        conversation.logger.info("Typed location %r matched no known place", typed)
        return context

    # matches is None here -- the API call itself failed (network/timeout/non-2xx), not "no
    # results". Degrade to the old local match rather than losing location capture entirely.
    index = await conversation._safe_city_index()
    if not index:
        return context
    city = city_index.match_typed_city(index, typed)
    if city:
        conversation.logger.info("Matched typed location %r to city %s via local index fallback", typed, city)
        new_ctx = {**context, "city": city}
        lat_lng = index[city][0] if index.get(city) else [None, None]
        if lat_lng[0] is not None:
            new_ctx["patient_lat"] = lat_lng[0]
            new_ctx["patient_lng"] = lat_lng[1]
        return new_ctx
    return context


async def _handle_choosing_location(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = context.get("lang")
    booking = conversation._get_or_create_clipboard(context)

    if input_type == "list_reply":
        options: dict = context.get("location_options", {})
        match = options.get(input_value)
        if not match:
            await _send_location_match_list(client, phone, context)
            return
        context = _apply_single_match(context, match)
        context.pop("location_options", None)
    elif input_type == "location":
        lat_str, lng_str = input_value.split(",")
        context["patient_lat"] = float(lat_str)
        context["patient_lng"] = float(lng_str)
        context = await _resolve_city(context)
    elif input_type == "text" and input_value.strip():
        context["location_text"] = input_value.strip()
        context = await _resolve_city(context)
    else:
        await conversation.whatsapp_client.send_text(client, phone, t("location_prompt", lang))
        return

    if context.get("location_options"):
        booking_slots.mark_ambiguous(booking, "location", list(context["location_options"].keys()), raw=context.get("location_text"))
        context["booking"] = booking
        await conversation._transition_to(phone, "choosing_location", context, "choosing_location")
        await _send_location_match_list(client, phone, context)
        return

    if context.get("city"):
        location_val = context["city"]
        if context.get("patient_lat") is not None:
            location_val = {"lat": context["patient_lat"], "lng": context["patient_lng"], "city": context.get("city")}
        booking_slots.fill(booking, "location", location_val, raw=context.get("location_text"), source="user")
    else:
        booking_slots.mark_notfound(booking, "location", raw=context.get("location_text"))

    await conversation._advance_booking_flow(client, phone, context, booking)
