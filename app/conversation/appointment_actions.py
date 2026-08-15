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

from datetime import date

from app.messengers import hms_client
from app.messengers.hms_client import HmsApiError
from app.i18n import t
from app.conversation.shared import _match_choice


async def _start_appointment_action_flow(
    client, phone, context, current_step, action: str, new_date_str: str | None = None
) -> None:
    from app import conversation

    lang = context.get("lang")

    local_candidates = await conversation.db.get_booked_appointments_for_phone(phone)
    live_candidates: dict[str, dict] = {}
    for c in local_candidates:
        appt_id = c["hms_appointment_id"]
        try:
            detail = await hms_client.get_appointment(appt_id)
        except HmsApiError:
            continue
        appt = detail.get("appointment") if detail.get("success") else None
        if appt and appt.get("statusCode") not in ("CANCELLED", "COMPLETED"):
            live_candidates[appt_id] = appt

    if not live_candidates:
        await conversation.whatsapp_client.send_text(client, phone, t("no_active_appointment", lang))
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
    step = "choosing_appointment_to_cancel" if action == "cancel" else "choosing_appointment_to_reschedule"
    await conversation._transition_to(phone, step, new_context, current_step)
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
        client, phone, context, "choosing_appointment_to_cancel" if action == "cancel" else "choosing_appointment_to_reschedule",
        action, appt_id, appt, context.get("appt_action_new_date"),
    )


async def _confirm_appointment_action(
    client, phone, context, current_step, action: str, appt_id: str, appt: dict, new_date_str: str | None
) -> None:
    from app import conversation

    lang = context.get("lang")
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
