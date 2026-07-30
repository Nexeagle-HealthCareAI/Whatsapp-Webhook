import logging
from datetime import date, timedelta

import httpx

from app import db, hms_client, whatsapp_client
from app.hms_client import HmsApiError

logger = logging.getLogger("conversation")

_SHIFT_FALLBACK = ["Morning", "Afternoon", "Evening"]

GREETING_TEXT = (
    "Hi! I can help you book a doctor's appointment. "
    "Let's start — which specialty are you looking for?"
)


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

    try:
        if current_step == "choosing_specialty":
            await _handle_choosing_specialty(client, phone, input_type, input_value, context)
        elif current_step == "choosing_doctor":
            await _handle_choosing_doctor(client, phone, input_type, input_value, context)
        elif current_step == "choosing_date":
            await _handle_choosing_date(client, phone, input_type, input_value, context)
        elif current_step == "choosing_shift":
            await _handle_choosing_shift(client, phone, input_type, input_value, context)
        elif current_step == "confirming":
            await _handle_confirming(client, phone, sender_name, input_type, input_value, context)
        else:
            # No state (new/returning user) or an unrecognized step — restart cleanly
            # rather than leave the conversation stuck.
            await _start(client, phone)
    except HmsApiError as exc:
        logger.warning("HMS API rejected request for %s: %s", phone, exc)
        await whatsapp_client.send_text(
            client, phone, "Sorry, something went wrong on our end. Please try again in a moment."
        )
    except httpx.HTTPError as exc:
        logger.warning("HMS API unreachable for %s: %s", phone, exc)
        await whatsapp_client.send_text(
            client, phone, "Our booking system is temporarily unavailable. Please try again shortly."
        )


async def _start(client: httpx.AsyncClient, phone: str) -> None:
    specialties = await hms_client.list_specialties()
    if not specialties:
        await whatsapp_client.send_text(
            client, phone, "Sorry, no doctors are available for booking right now. Please try later."
        )
        return
    rows = [(s["category"], s.get("displayName") or s["category"]) for s in specialties]
    await whatsapp_client.send_list(
        client, phone, GREETING_TEXT, "Choose specialty", rows, "Specialties"
    )
    await db.save_conversation_state(phone, "choosing_specialty", {})


async def _handle_choosing_specialty(client, phone, input_type, input_value, context) -> None:
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, "Please choose a specialty from the list above.")
        return
    specialty_category = input_value
    doctors = await hms_client.list_doctors(specialty_category)
    if not doctors:
        await whatsapp_client.send_text(
            client, phone,
            "Sorry, no doctors are currently available in that specialty. Please try 'hi' to start over.",
        )
        return
    rows = [(d["doctorId"], d.get("fullName") or "Doctor") for d in doctors]
    await whatsapp_client.send_list(
        client, phone, "Great — here are the doctors available:", "Choose doctor", rows, "Doctors"
    )
    await db.save_conversation_state(
        phone, "choosing_doctor", {**context, "specialty_category": specialty_category}
    )


async def _handle_choosing_doctor(client, phone, input_type, input_value, context) -> None:
    if input_type != "list_reply":
        await whatsapp_client.send_text(client, phone, "Please choose a doctor from the list above.")
        return
    doctor_id = input_value
    await whatsapp_client.send_buttons(
        client, phone, "When would you like to visit?",
        [("today", "Today"), ("tomorrow", "Tomorrow")],
    )
    await db.save_conversation_state(phone, "choosing_date", {**context, "doctor_id": doctor_id})


async def _handle_choosing_date(client, phone, input_type, input_value, context) -> None:
    choice = _match_choice(input_type, input_value, ["today", "tomorrow"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, "Please choose Today or Tomorrow above.")
        return
    preferred_date = date.today() if choice == "today" else date.today() + timedelta(days=1)
    doctor_id = context["doctor_id"]

    availability = await hms_client.get_doctor_availability(doctor_id, preferred_date)
    if not availability.get("isAvailable"):
        await whatsapp_client.send_text(
            client, phone,
            "That doctor isn't available on that day. Please type 'hi' to start over and pick another day.",
        )
        await db.clear_conversation_state(phone)
        return

    shift_names = [s["name"] for s in availability.get("shifts", []) if s.get("name")] or _SHIFT_FALLBACK
    await whatsapp_client.send_buttons(
        client, phone, "Which time of day works best?",
        [(name.lower(), name) for name in shift_names[:3]],
    )
    await db.save_conversation_state(
        phone, "choosing_shift",
        {**context, "preferred_date": preferred_date.isoformat(), "date_label": choice.capitalize()},
    )


async def _handle_choosing_shift(client, phone, input_type, input_value, context) -> None:
    if input_type not in ("button_reply", "text"):
        await whatsapp_client.send_text(client, phone, "Please choose a time of day above.")
        return
    shift_label = input_value.strip().capitalize()
    date_label = context.get("date_label", "the selected day")
    await whatsapp_client.send_buttons(
        client, phone,
        f"Confirm appointment request for {date_label}, {shift_label}?",
        [("confirm", "Confirm"), ("cancel", "Cancel")],
    )
    await db.save_conversation_state(phone, "confirming", {**context, "shift_label": shift_label})


async def _handle_confirming(client, phone, sender_name, input_type, input_value, context) -> None:
    choice = _match_choice(input_type, input_value, ["confirm", "cancel"])
    if choice is None:
        await whatsapp_client.send_text(client, phone, "Please tap Confirm or Cancel above.")
        return
    if choice == "cancel":
        await whatsapp_client.send_text(client, phone, "No problem — booking cancelled.")
        await db.clear_conversation_state(phone)
        return

    preferred_date = date.fromisoformat(context["preferred_date"])
    doctor_id = context["doctor_id"]
    shift_label = context.get("shift_label", "any time")
    patient_name = sender_name or phone

    if await db.has_pending_appointment(phone, preferred_date):
        await whatsapp_client.send_text(
            client, phone,
            "You already have a pending request for that day — our team will reach out shortly.",
        )
        await db.clear_conversation_state(phone)
        return

    row_id = await db.create_pending_appointment(phone, preferred_date)
    try:
        result = await hms_client.book_appointment(
            patient_name, phone, doctor_id, preferred_date, shift_label
        )
    except (HmsApiError, httpx.HTTPError):
        await db.mark_appointment_failed(row_id)
        raise

    await db.mark_appointment_booked(row_id, result.get("appointmentId") or "")
    await whatsapp_client.send_text(
        client, phone,
        "Your appointment request has been submitted! Our front desk will confirm the exact time shortly.",
    )
    await db.clear_conversation_state(phone)
