"""
app/conversation/slot_selection.py
-------------------------------------
Date & slot selection domain: usable-shift filtering, slot-label formatting,
auto-matching an implied slot, finalizing a selection, and the offer/choose
step handlers.

_clinic_now and _get_offered_slots are 2 of the 9 mutated names the test suite
reassigns directly, so they stay defined in app/conversation/__init__.py, not
here. _usable_shifts's call to _clinic_now, and every other cross-reference
back into __init__.py (whatsapp_client, _get_or_create_clipboard,
_advance_booking_flow, _transition_to, _send_doctor_list), goes through a
function-body-local `from app import conversation` + `conversation.<name>(...)`
-- see docs/architecture.md and app/conversation/checkin.py's module docstring.
Calls between functions that all live in this file (_usable_shifts ->
_parse_shift_end, _send_slot_options -> _pick_matching_slot/
_finalize_slot_selection, _handle_choosing_slot -> _finalize_slot_selection)
stay as plain same-module calls.
"""
import logging
from datetime import time

from app.decision_maker import booking_slots
from app.i18n import t
from app.conversation.shared import _match_choice

logger = logging.getLogger("conversation")

_SHIFT_FALLBACK = ["Morning", "Afternoon", "Evening"]


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


def _usable_shifts(availability: dict, preferred_date) -> list[str]:
    """Shift names the patient could still turn up for.

    The availability endpoint returns a doctor's standing schedule for a date — it does not
    know what time it is, so for today it happily returns Morning (09:00-12:00) at 7pm.
    Offering that books a patient into a slot that has already passed, so today's shifts are
    filtered against the clock. Future dates pass through untouched."""
    from app import conversation

    shifts = [s for s in availability.get("shifts", []) if s.get("name")]
    if not shifts:
        return list(_SHIFT_FALLBACK)
    if preferred_date != conversation._clinic_now().date():
        return [s["name"] for s in shifts]

    now = conversation._clinic_now().time()
    usable = []
    for shift in shifts:
        end = _parse_shift_end(shift)
        if end is None or end > now:
            usable.append(shift["name"])
    return usable


async def _finalize_slot_selection(client, phone: str, context: dict, selected_slot: dict) -> None:
    from app import conversation

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

    booking = conversation._get_or_create_clipboard(context)
    booking_slots.fill(booking, "date", preferred_date, raw=date_label, source="user")
    booking_slots.fill(booking, "shift", selected_slot["shift_name"], raw=selected_slot["shift_name"], source="user")

    await conversation._advance_booking_flow(client, phone, context, booking)


async def _send_slot_options(client, phone: str, context: dict) -> None:
    from app import conversation

    lang = context.get("lang")
    doctor_id = context["doctor_id"]

    slots = await conversation._get_offered_slots(doctor_id, lang)
    if not slots:
        await conversation.whatsapp_client.send_buttons(
            client, phone, t("today_shifts_over", lang),
            [("change_doctor", t("change_doctor_btn", lang))],
        )
        await conversation._transition_to(phone, "choosing_slot", context, "choosing_doctor")
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

    await conversation.whatsapp_client.send_buttons(
        client, phone, t("shift_prompt", lang),
        buttons,
    )

    next_context = {**context, "offered_slots": offered_slots_data}
    # A hint that didn't produce a unique match (ambiguous, or no offered slot matched it)
    # shouldn't linger and silently auto-pick on some later, unrelated re-render.
    next_context.pop("time_of_day_hint", None)
    await conversation._transition_to(phone, "choosing_slot", next_context, "choosing_doctor")


async def _handle_choosing_slot(client, phone, input_type, input_value, context) -> None:
    from app import conversation

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
        await conversation._send_doctor_list(client, phone, context)
        return
    else:
        normalized = input_value.strip().lower()
        if normalized in label_to_slot:
            selected_slot = label_to_slot[normalized]
        elif normalized in shift_name_to_slot:
            selected_slot = shift_name_to_slot[normalized]

    if selected_slot is None:
        options_str = ", ".join(s["label"] for s in offered_slots)
        await conversation.whatsapp_client.send_text(
            client, phone, t("shift_choose_hint", lang, options=options_str)
        )
        return

    await _finalize_slot_selection(client, phone, context, selected_slot)
