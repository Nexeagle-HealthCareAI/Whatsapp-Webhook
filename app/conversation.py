import json
import logging
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from app import city_index, db, hms_client, i18n, symptom_client, whatsapp_client
from app.config import settings
from app.geo import haversine_km
from app.hms_client import HmsApiError
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
        "hai", "he", "h", "bhejo", "dikhao", "dikho", "dikhayein", "dikhaye",
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

    words = re.findall(r'\b\w+\b', normalized)
    if not words:
        return None

    has_hinglish = any(w in hinglish_keywords for w in words)
    has_english = any(w in english_keywords for w in words)

    if has_hinglish:
        return "hg"
    elif has_english:
        return "en"

    return None


async def handle_message(
    client: httpx.AsyncClient,
    phone: str,
    sender_name: str | None,
    input_type: str,
    input_value: str,
) -> None:
    state = await db.get_conversation_state(phone)
    current_step = state["current_step"] if state else None
    context = state["context"] if state else {}
    lang = context.get("lang")

    try:
        if current_step == "choosing_language":
            await _handle_choosing_language(client, phone, input_type, input_value, context)
        elif current_step == "choosing_location":
            await _handle_choosing_location(client, phone, input_type, input_value, context)
        elif current_step == "choosing_search_mode":
            await _handle_choosing_search_mode(client, phone, input_type, input_value, context)
        elif current_step == "awaiting_symptom":
            await _handle_awaiting_symptom(client, phone, input_type, input_value, context)
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
        elif current_step == "choosing_date":
            await _handle_choosing_date(client, phone, input_type, input_value, context)
        elif current_step == "choosing_shift":
            await _handle_choosing_shift(client, phone, input_type, input_value, context)
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
                context = {**context, "lang": detected_lang}
                await whatsapp_client.send_text(client, phone, t("greeting", detected_lang))
                await _send_location_request(client, phone, context)
            else:
                await _start(client, phone)
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


async def _start(client: httpx.AsyncClient, phone: str) -> None:
    await whatsapp_client.send_buttons(
        client, phone, LANG_PROMPT,
        [(code, label) for code, label in LANGUAGE_LABELS.items()],
    )
    await db.save_conversation_state(phone, "choosing_language", {})


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
    await db.save_conversation_state(phone, "choosing_location", context)


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
    await _send_search_mode_prompt(client, phone, context)


# ---------------------------------------------------------------------------------------
# 4. Symptom vs. specialty entry (requirement 4) — this part already existed pre-redesign;
# kept as-is functionally, just moved behind language/person/location and translated.
# ---------------------------------------------------------------------------------------


async def _send_search_mode_prompt(client: httpx.AsyncClient, phone: str, context: dict) -> None:
    lang = context.get("lang")
    await whatsapp_client.send_buttons(
        client, phone, t("search_mode_prompt", lang),
        [("symptom", t("search_mode_symptom", lang)), ("browse", t("search_mode_browse", lang))],
    )
    await db.save_conversation_state(phone, "choosing_search_mode", context)


async def _handle_choosing_search_mode(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["symptom", "browse"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("person_choose_hint", lang))
        return
    if choice == "symptom":
        await whatsapp_client.send_text(client, phone, t("symptom_ask", lang))
        await db.save_conversation_state(phone, "awaiting_symptom", context)
        return
    await _send_specialty_list(client, phone, context)


async def _handle_awaiting_symptom(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
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

    await whatsapp_client.send_text(client, phone, t("symptom_matched", lang, category=matched_category))
    await _send_sort_prompt(client, phone, context, matched_category)


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
    await db.save_conversation_state(
        phone, "choosing_specialty_group", {**context, "specialty_groups": group_members}
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
        await _send_sort_prompt(client, phone, context, members[0]["category"])
        return

    rows = [_specialty_row(s) for s in members]
    await whatsapp_client.send_list(
        client, phone, t("specialty_list_prompt", lang), t("specialty_list_button", lang),
        rows, t("specialty_group_section", lang),
    )
    await db.save_conversation_state(phone, "choosing_specialty", context)


async def _handle_choosing_specialty(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, t("specialty_choose_hint", lang))
        return
    await _send_sort_prompt(client, phone, context, input_value)


# ---------------------------------------------------------------------------------------
# 5. Doctor filtering by rating / distance / experience / lowest fee (requirement 5).
# Confirmed live against 1hms-dev-api.nexeagle.com that GET /public/doctors already returns
# rating, fee, discountedFee, experienceYears, latitude/longitude, hospitalName, address —
# so this is a client-side sort over data the API already returns, no backend change
# needed. Also confirmed that sortBy/latitude/longitude query params are silently ignored by
# that endpoint today (order was identical with or without them) — so the sorting has to
# happen here, not by asking the API to do it.
# ---------------------------------------------------------------------------------------


async def _send_sort_prompt(client: httpx.AsyncClient, phone: str, context: dict, specialty_category: str) -> None:
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
    await db.save_conversation_state(phone, "choosing_sort", context)


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


def _doctor_row_description(doctor: dict, context: dict) -> str:
    parts = []
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
    return " · ".join(parts)


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
            await db.save_conversation_state(phone, "confirming_wider_search", context)
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

    await _render_doctor_list(client, phone, context, doctors)


async def _render_doctor_list(
    client: httpx.AsyncClient, phone: str, context: dict, doctors: list[dict]
) -> None:
    """Sorts, trims to WhatsApp's row cap, and sends. Shared by the normal radius search and
    the opted-in wider search so both present results identically."""
    lang = context.get("lang")
    sorted_doctors = _sort_doctors(doctors, context)[:10]
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
    await _render_doctor_list(client, phone, {**context, "sort_key": "nearest"}, doctors)


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

    await whatsapp_client.send_buttons(
        client, phone, t("date_prompt", lang),
        [("today", t("date_today", lang)), ("tomorrow", t("date_tomorrow", lang))],
    )
    await db.save_conversation_state(phone, "choosing_date", context)


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


async def _offer_other_day(client, phone, context, tried: str, reason_key: str) -> None:
    """Dead-end recovery. Whatever went wrong with the day the patient picked, keep the
    conversation (and everything they've already chosen) and offer the obvious next moves:
    the other day, or a different doctor. Previously this wiped the whole session and told
    them to type 'hi', which meant redoing language, location, specialty and doctor just
    because a doctor was busy."""
    lang = context.get("lang")
    other = "tomorrow" if tried == "today" else "today"
    await whatsapp_client.send_buttons(
        client, phone, t(reason_key, lang),
        [
            (other, t(f"date_{other}", lang)),
            ("change_doctor", t("change_doctor_btn", lang)),
        ],
    )
    await db.save_conversation_state(phone, "choosing_date", context)


async def _handle_choosing_date(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["today", "tomorrow", "change_doctor"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, t("date_choose_hint", lang))
        return

    if choice == "change_doctor":
        # Straight back to the doctor list, keeping specialty/location/sort — the patient
        # only wants a different doctor, not a different everything.
        await _send_doctor_list(client, phone, context)
        return

    # Clinic-local, not container-local: at 1am IST the container's UTC date is still
    # yesterday, and "today" would resolve to a date that has already passed.
    today = _clinic_now().date()
    preferred_date = today if choice == "today" else today + timedelta(days=1)
    doctor_id = context["doctor_id"]

    availability = await hms_client.get_doctor_availability(doctor_id, preferred_date)
    if not availability.get("isAvailable"):
        await _offer_other_day(client, phone, context, choice, "not_available")
        return

    shift_names = _usable_shifts(availability, preferred_date)
    if not shift_names:
        # Doctor works today, but every one of their shifts has already finished.
        await _offer_other_day(client, phone, context, choice, "today_shifts_over")
        return

    offered = shift_names[:3]
    await whatsapp_client.send_buttons(
        client, phone, t("shift_prompt", lang),
        [(name.lower(), name) for name in offered],
    )
    date_label = t("date_today", lang) if choice == "today" else t("date_tomorrow", lang)
    await db.save_conversation_state(
        phone, "choosing_shift",
        {
            **context,
            "preferred_date": preferred_date.isoformat(),
            "date_label": date_label,
            # Remembered so the next step can reject anything else. Without this, a patient
            # who TYPES "morning" at 7pm gets booked into a shift that ended hours ago —
            # the button correctly hides it, but typing bypassed the filter entirely.
            "offered_shifts": offered,
        },
    )


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


async def _handle_choosing_shift(client, phone, input_type, input_value, context) -> None:
    lang = context.get("lang")
    offered = context.get("offered_shifts") or list(_SHIFT_FALLBACK)
    choice = _match_choice(input_type, input_value, [name.lower() for name in offered])
    if choice is None:
        await whatsapp_client.send_text(
            client, phone, t("shift_choose_hint", lang, options=", ".join(offered))
        )
        return
    shift_label = next(name for name in offered if name.lower() == choice)

    context = {**context, "shift_label": shift_label}
    await _send_patient_details_flow(client, phone, context)


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
    await db.save_conversation_state(phone, "awaiting_patient_details", context)


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
    await db.save_conversation_state(phone, "confirming", context)


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

    await db.clear_conversation_state(phone)
