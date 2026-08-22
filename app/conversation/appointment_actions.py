"""
app/conversation/appointment_actions.py
-----------------------------------------
Cancel / reschedule an EXISTING, already-booked appointment (as opposed to
_handle_confirming's "cancel" choice in __init__.py, which only ever abandons an
in-progress NEW booking that hasn't been created yet). Entry point is
_start_appointment_action_flow, triggered by the cancel_appointment / reschedule_appointment
intents in handle_message. Real, hard-to-reverse, billing-affecting actions once they reach
hms_client -- always confirmed via a Yes/No button before the actual API call, never fired
straight off a single NLU-classified message.

Extracted out of app/conversation/__init__.py (previously the file that grew back toward
the size it was originally split to fix -- see docs/solid-rebuild-plan.md and the Layer 4
review this extraction followed). Nothing in this file is monkeypatched directly by the
test suite; tests drive it via conversation.handle_message and patch
conversation.db / conversation.whatsapp_client.* the same way every other sibling file's
tests do. hms_client is safe to import statically here (only ever attribute-patched on the
shared module object, never reassigned wholesale -- same as every other sibling file).
db, whatsapp_client, and _transition_to are NOT safe to import statically: db and
whatsapp_client are monkeypatched by whole-name reassignment on the `conversation` module
in the test suite, and _transition_to is a core-orchestration function that still lives in
__init__.py -- a module-level `from app.conversation import X` for any of these would
capture a stale reference at import time and also risk a circular-import ordering hazard,
since __init__.py itself imports this file to re-export these functions. Every such call
is therefore made via a function-body-local `from app import conversation` +
`conversation.<name>(...)` -- see app/conversation/checkin.py's module docstring for the
same reasoning applied consistently across every sibling file.
"""

from datetime import date, timedelta

from app.messengers import hms_client
from app.messengers.hms_client import HmsApiError
from app.i18n import t
from app.conversation.shared import _match_choice

# NLU intent -> the `action` value _start_appointment_action_flow understands. The single
# source of truth for that mapping, so the Conductor (__init__.py) never has to know the
# action vocabulary itself -- it just looks up whatever intent it saw.
_INTENT_TO_APPT_ACTION = {
    "cancel_appointment": "cancel",
    "reschedule_appointment": "reschedule",
    "check_my_appointment": "status",
}

# action -> the step name to disambiguate under when more than one live appointment is
# found. "status" (check_my_appointment) gets its own step even though its final message is
# read-only, so a stale/duplicate list tap re-dispatches through the right handler.
_CHOOSING_STEP = {
    "cancel": "choosing_appointment_to_cancel",
    "reschedule": "choosing_appointment_to_reschedule",
    "status": "choosing_appointment_to_view",
}


def _is_appointment_stale(appt: dict, clinic_today: date) -> bool:
    """Defensive safety net alongside the statusCode check below: statusCode is 1HMS's own
    live/cancellable signal and is trusted as the primary one, but if it's ever stale (not
    yet flipped to COMPLETED even though the appointment's date has clearly passed), this
    stops something from days ago still being offered for cancellation. A 1-day grace period
    past the scheduled date, not an hours-precision cutoff -- this bot never learns an
    appointment's real scheduled TIME (see hms_client.book_appointment's own docstring:
    PreferredTime is deliberately left unset, the front desk picks the actual time), so
    apptDate is date-only (always midnight on the wire) and an hour-level comparison against
    it wouldn't be meaningful. Unparseable dates fail open (kept, not excluded) -- 1HMS's own
    cancel endpoint is still the final, authoritative check once the patient actually confirms."""
    raw = appt.get("apptDate")
    if not raw:
        return False
    try:
        appt_date = date.fromisoformat(raw[:10])
    except (ValueError, TypeError):
        return False
    return appt_date < clinic_today - timedelta(days=1)


def _has_in_progress_booking(context) -> bool:
    """True if the clipboard has any real progress on a NEW, not-yet-booked appointment
    (location/doctor/date/shift/patient already chosen) -- used only to decide what a bare
    "cancel" should abandon when there's no real appointment to check against instead. This
    is deliberately private to this file: cancel/reschedule decision-making for an existing
    appointment belongs entirely to this Specialist, not the Conductor (__init__.py), which
    should only ever need to call _start_appointment_action_flow and nothing more."""
    booking = context.get("booking") or {}
    return any(
        booking.get(slot, {}).get("status") == "filled"
        for slot in ("location", "doctor", "date", "shift", "patient")
    )


async def _start_appointment_action_flow(
    client, phone, context, current_step, action: str, new_date_str: str | None = None,
) -> None:
    """Entry point for the cancel_appointment / reschedule_appointment intents -- the
    Conductor (handle_message in __init__.py) only ever routes the intent here, every actual
    decision happens in this Specialist: whether a real appointment exists, whether to
    disambiguate between several, and (for action=="cancel" specifically) whether a bare
    "cancel" with no real appointment should instead abandon an in-progress draft booking.
    Keeping all of that here, not split between here and the Conductor, is deliberate --
    see _has_in_progress_booking above."""
    from app import conversation

    lang = context.get("lang")

    # A real, already-booked appointment always takes priority over any leftover
    # in-progress-booking clipboard data -- a patient asking to cancel almost always means
    # "my real appointment", and a locally-recorded row is a strong, cheap (no HMS call yet)
    # signal one might exist. Checking this FIRST fixed a live-reported bug: a stale,
    # long-abandoned draft booking (started once, never finished, never cleared) was being
    # mistaken for something actively "in progress right now", so "cancel my appointment"
    # silently answered with a generic "cancelled, start over" message instead of ever
    # looking up the patient's real appointment.
    # _clinic_now, not a naive UTC now() -- the Docker image sets no TZ, so date.today()
    # there is the UTC date, which runs behind IST; see _clinic_now's own docstring.
    clinic_today = conversation._clinic_now().date()

    local_candidates = await conversation.db.get_booked_appointments_for_phone(phone)
    live_candidates: dict[str, dict] = {}
    for c in local_candidates:
        appt_id = c["hms_appointment_id"]
        try:
            detail = await hms_client.get_appointment(appt_id)
        except HmsApiError:
            continue
        appt = detail.get("appointment") if detail.get("success") else None
        if appt and appt.get("statusCode") not in ("CANCELLED", "COMPLETED") and not _is_appointment_stale(appt, clinic_today):
            live_candidates[appt_id] = appt

    if not live_candidates:
        # No real appointment at all. A patient typing "cancel" while mid-way through
        # building a NEW, not-yet-booked appointment means "abandon what I'm doing right
        # now" -- same as the Cancel button on the final confirm screen (_handle_confirming
        # in __init__.py). Reschedule has no equivalent "abandon a draft" meaning, so this
        # only applies to action == "cancel".
        if action == "cancel" and _has_in_progress_booking(context):
            await conversation.whatsapp_client.send_text(client, phone, t("cancelled", lang))
            await conversation.db.clear_conversation_state(phone)
            return

        # A tappable button, not just the typed "book appointment" fallback mentioned in the
        # same message -- tapping is more reliable than typing for most patients. The button
        # id (start_booking) is handled in __init__.py's handle_message regardless of
        # conversation state, since clear_conversation_state right below wipes it before any
        # tap on this button could arrive.
        await conversation.whatsapp_client.send_buttons(
            client, phone, t("no_active_appointment", lang),
            [("start_booking", t("book_appointment_btn", lang))],
        )
        await conversation.db.clear_conversation_state(phone)
        return

    if len(live_candidates) == 1:
        appt_id, appt = next(iter(live_candidates.items()))
        await _confirm_appointment_action(client, phone, context, current_step, action, appt_id, appt, new_date_str)
        return

    # More than one live appointment booked through this bot — disambiguate first, same UX as
    # _handle_checkin_awaiting_location's "candidates" case.
    new_context = {
        **context,
        "appt_action": action,
        "appt_action_options": live_candidates,
        "appt_action_new_date": new_date_str,
    }
    await conversation._transition_to(phone, _CHOOSING_STEP[action], new_context, current_step)
    await _send_appointment_choice_list(client, phone, lang, live_candidates)


async def _send_appointment_choice_list(client, phone, lang: str | None, options: dict[str, dict]) -> None:
    from app import conversation

    rows = [
        (appt_id, appt.get("doctorName") or "Doctor", appt.get("apptDate") or "")
        for appt_id, appt in options.items()
    ]
    await conversation.whatsapp_client.send_list(
        client, phone, t("choose_appointment_prompt", lang), t("choose_appointment_button", lang), rows,
    )


async def _handle_choosing_appointment_to_cancel(client, phone, input_type, input_value, context) -> None:
    await _handle_choosing_appointment_candidate(client, phone, input_type, input_value, context, action="cancel")


async def _handle_choosing_appointment_to_reschedule(client, phone, input_type, input_value, context) -> None:
    await _handle_choosing_appointment_candidate(client, phone, input_type, input_value, context, action="reschedule")


async def _handle_choosing_appointment_to_view(client, phone, input_type, input_value, context) -> None:
    await _handle_choosing_appointment_candidate(client, phone, input_type, input_value, context, action="status")


async def _handle_choosing_appointment_candidate(client, phone, input_type, input_value, context, action: str) -> None:
    lang = context.get("lang")
    options = context.get("appt_action_options", {})
    if input_type != "list_reply" or input_value not in options:
        # Stale list (e.g. patient tapped an old message) — same guard as
        # _handle_checkin_choosing_appointment.
        await _send_appointment_choice_list(client, phone, lang, options)
        return

    appt_id = input_value
    appt = options[appt_id]
    await _confirm_appointment_action(
        client, phone, context, _CHOOSING_STEP[action],
        action, appt_id, appt, context.get("appt_action_new_date"),
    )


async def _confirm_appointment_action(
    client, phone, context, current_step, action: str, appt_id: str, appt: dict, new_date_str: str | None
) -> None:
    from app import conversation

    lang = context.get("lang")

    if action == "status":
        # Read-only -- there's nothing to confirm, so no Yes/No button implying an action is
        # about to happen. A plain "cancel my appointment?" Confirm/Cancel prompt here would
        # be misleading for a patient who only asked whether they have a booking at all
        # (check_my_appointment) -- live-reported.
        await conversation.whatsapp_client.send_text(
            client, phone, t("appointment_status_message", lang, doctor=appt.get("doctorName", "-"), date=appt.get("apptDate", "-")),
        )
        await conversation.db.clear_conversation_state(phone)
        return

    new_context = {
        **context,
        "appt_action": action,
        "appt_action_id": appt_id,
        "appt_action_detail": appt,
        "appt_action_new_date": new_date_str,
    }
    new_context.pop("appt_action_options", None)
    step = "confirming_appointment_cancel" if action == "cancel" else "confirming_appointment_reschedule"
    await conversation._transition_to(phone, step, new_context, current_step)
    await _send_appointment_confirm_prompt(client, phone, lang, action, appt, new_date_str)


async def _send_appointment_confirm_prompt(client, phone, lang: str | None, action: str, appt: dict, new_date_str: str | None) -> None:
    from app import conversation

    if action == "cancel":
        prompt = t(
            "confirm_cancel_appointment_prompt", lang,
            doctor=appt.get("doctorName", "-"), date=appt.get("apptDate", "-"),
        )
    else:
        prompt = t(
            "confirm_reschedule_appointment_prompt", lang,
            doctor=appt.get("doctorName", "-"), old_date=appt.get("apptDate", "-"), new_date=new_date_str or "-",
        )
    await conversation.whatsapp_client.send_buttons(
        client, phone, prompt,
        [("confirm", t("confirm_btn", lang)), ("cancel", t("cancel_btn", lang))],
    )


async def _handle_confirming_appointment_cancel(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["confirm", "cancel"])
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("confirm_choose_hint", lang))
        return
    if choice == "cancel":
        await conversation.whatsapp_client.send_text(client, phone, t("appointment_action_aborted", lang))
        await conversation.db.clear_conversation_state(phone)
        return

    appt_id = context["appt_action_id"]
    result = await hms_client.cancel_appointment(appt_id, mobile=phone)
    if result.get("success"):
        await conversation.db.mark_appointment_cancelled_locally(appt_id)
        await conversation.whatsapp_client.send_text(client, phone, result.get("message") or t("cancel_appointment_success", lang))
    else:
        await conversation.whatsapp_client.send_text(client, phone, result.get("message") or t("cancel_appointment_failed", lang))
    await conversation.db.clear_conversation_state(phone)


async def _handle_confirming_appointment_reschedule(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    choice = _match_choice(input_type, input_value, ["confirm", "cancel"])
    if choice is None:
        await conversation.whatsapp_client.send_text(client, phone, t("confirm_choose_hint", lang))
        return
    if choice == "cancel":
        await conversation.whatsapp_client.send_text(client, phone, t("appointment_action_aborted", lang))
        await conversation.db.clear_conversation_state(phone)
        return

    appt_id = context["appt_action_id"]
    new_date_str = context.get("appt_action_new_date")
    if not new_date_str:
        await conversation.whatsapp_client.send_text(client, phone, t("reschedule_appointment_failed", lang))
        await conversation.db.clear_conversation_state(phone)
        return

    result = await hms_client.reschedule_appointment(appt_id, mobile=phone, to_date=date.fromisoformat(new_date_str))
    if result.get("success"):
        await conversation.db.mark_appointment_rescheduled_locally(appt_id, date.fromisoformat(new_date_str))
        await conversation.whatsapp_client.send_text(client, phone, result.get("message") or t("reschedule_appointment_success", lang))
    else:
        await conversation.whatsapp_client.send_text(client, phone, result.get("message") or t("reschedule_appointment_failed", lang))
    await conversation.db.clear_conversation_state(phone)


async def _prompt_appointment_choice(client, phone, context):
    options = context.get("appt_action_options", {})
    if options:
        await _send_appointment_choice_list(client, phone, context.get("lang"), options)


async def _prompt_appointment_confirm(client, phone, context):
    lang = context.get("lang")
    action = context.get("appt_action", "cancel")
    appt = context.get("appt_action_detail", {})
    await _send_appointment_confirm_prompt(client, phone, lang, action, appt, context.get("appt_action_new_date"))
