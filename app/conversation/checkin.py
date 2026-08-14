"""
app/conversation/checkin.py
------------------------------
QR-triggered flows: discharge-summary/prescription document pull, per-doctor
booking QR, and OPD check-in. Nothing in this file is monkeypatched directly
by the test suite (tests drive it via conversation.handle_message and patch
conversation.db / conversation.whatsapp_client / conversation.hms_client.*),
but several of these functions call back into names that stay defined in
app/conversation/__init__.py -- whatsapp_client and db are two of the 9 names
tests DO reassign directly there, and _advance_booking_flow/_start/_transition_to
are core-orchestration functions that live in __init__.py and would otherwise
be captured as a stale reference at import time (an ordering hazard too, since
__init__.py itself imports this file to re-export these functions).

Every such call is therefore made via a function-body-local
`from app import conversation` + `conversation.<name>(...)`, never a
module-level import -- the same "look it up by name at call time, don't bind it
at import time" reasoning already documented for _DOCUMENT_TRIGGERS's resolver
lookup below (this file inherits that comment's logic, now applied
consistently to every cross-reference back into __init__.py). See
docs/architecture.md for the full explanation.
"""
import re

from app.messengers import hms_client
from app.decision_maker import booking_slots
from app.messengers.hms_client import HmsApiError
from app.i18n import t
from app.conversation.doctor_list import _doctor_fee

# OPD QR check-in trigger — matches the "CHECKIN <code>" text GET /c/{hospital_code}
# (app/webhook.py) prefills into the wa.me deep link. See the interceptor in handle_message.
_CHECKIN_TRIGGER_PATTERN = re.compile(r"^checkin\s+(\S+)$", re.IGNORECASE)

# Discharge-summary / prescription QR pull triggers — matches the "DISCHARGE <token>",
# "RX <attachmentId>", "RXV <appointmentId>" text the GET /d, /rx, /rxv routes (webhook.py)
# prefill into their wa.me deep links. RXV is checked before RX would otherwise be
# ambiguous, but isn't actually: "RXV abc" never matches ^rx\s+ since the 'v' sits where
# _PRESCRIPTION_TRIGGER_PATTERN requires whitespace right after "rx". See
# _DOCUMENT_TRIGGERS / _handle_document_trigger below.
_DISCHARGE_TRIGGER_PATTERN = re.compile(r"^discharge\s+(\S+)$", re.IGNORECASE)
_PRESCRIPTION_TRIGGER_PATTERN = re.compile(r"^rx\s+(\S+)$", re.IGNORECASE)
_VISIT_SUMMARY_TRIGGER_PATTERN = re.compile(r"^rxv\s+(\S+)$", re.IGNORECASE)

# Doctor Dekho per-doctor QR (GET /doc/{doctorId} in webhook.py) -- deliberately NOT
# "DOCTOR <id>"/"BOOK <id>": both are plausible things a patient types organically ("doctor
# sharma", "book appointment"), which this interceptor would then wrongly hijack before NLU
# ever sees it. "DRBOOK" reads as a machine code, same as CHECKIN/DISCHARGE/RX/RXV, with no
# realistic organic-message collision. See _handle_doctor_booking_trigger below.
_DOCTOR_BOOKING_TRIGGER_PATTERN = re.compile(r"^drbook\s+(\S+)$", re.IGNORECASE)

# (pattern, resolver attribute name, filename, "not available" i18n key, "delivered" caption
# i18n key) — the resolver is looked up on hms_client by name at call time (see
# _handle_document_trigger), not bound to the function object here, so it stays patchable
# the same way _handle_checkin_trigger's direct hms_client.get_hospital_by_code(...) call is
# (a name bound here at import time would freeze in the pre-patch function, same class of bug
# as capturing `from x import y` instead of `import x`). RX (InkRx/manual
# PrescriptionAttachment uploads) and RXV (structured EPrescriptionPad e-prescriptions) are
# two different backend storage paths (see hms_client.py) but read as the same thing to a
# patient, so they share both i18n keys.
_DOCUMENT_TRIGGERS = (
    (
        _DISCHARGE_TRIGGER_PATTERN, "get_discharge_summary_url", "Discharge_Summary.pdf",
        "discharge_not_available", "discharge_delivered",
    ),
    (
        _PRESCRIPTION_TRIGGER_PATTERN, "get_prescription_attachment_url", "Prescription.pdf",
        "prescription_not_available", "prescription_delivered",
    ),
    (
        _VISIT_SUMMARY_TRIGGER_PATTERN, "get_visit_summary_url", "Prescription.pdf",
        "prescription_not_available", "prescription_delivered",
    ),
)


# ---------------------------------------------------------------------------------------
# Discharge-summary / prescription QR pull — resolves and replies in one shot, unlike
# check-in below. No current_step/context changes needed: existing conversation state
# (e.g. a booking in progress) is left completely untouched either way.
# ---------------------------------------------------------------------------------------


async def _handle_document_trigger(
    client,
    phone: str,
    code: str,
    context: dict,
    resolver_name: str,
    filename: str,
    not_available_key: str,
    delivered_key: str,
) -> None:
    from app import conversation

    # Re-resolves independently of the GET /d, /rx, /rxv routes' own validation
    # (app/webhook.py) — same "redirect is a UX nicety, never the authoritative check"
    # reasoning as _handle_checkin_trigger below, since the prefilled text is just a string
    # the patient's WhatsApp client could technically let them edit before send.
    lang = context.get("lang")
    resolver = getattr(hms_client, resolver_name)
    try:
        url = await resolver(code)
    except HmsApiError:
        await conversation.whatsapp_client.send_text(client, phone, t(not_available_key, lang))
        return

    sent = await conversation.whatsapp_client.send_document(client, phone, url, filename, caption=t(delivered_key, lang))
    if not sent:
        # The document exists but the WhatsApp send itself failed (network/API error) — a
        # generic retry message is more accurate here than "not available", which would
        # wrongly imply nothing was ever uploaded.
        await conversation.whatsapp_client.send_text(client, phone, t("error_hms", lang))


# ---------------------------------------------------------------------------------------
# Doctor Dekho per-doctor QR -- deterministically starts a NEW booking flow anchored to one
# already-known doctor (no specialty/name search at all), then hands off to the exact same
# machinery the free-text doctor-name-search path already uses (_advance_booking_flow's
# "doctor filled -> location auto-inferred -> ask date" cascade). Unlike the document-pull
# triggers above, this doesn't resolve-and-reply in one shot -- it kicks off a multi-step
# conversation, same shape as a patient typing a doctor's name from scratch.
# ---------------------------------------------------------------------------------------


async def _handle_doctor_booking_trigger(client, phone: str, doctor_id: str, context: dict) -> None:
    from app import conversation

    # Re-resolves independently of GET /doc/{doctorId}'s own validation (app/webhook.py) --
    # same "redirect is a UX nicety, never the authoritative check" reasoning as
    # _handle_checkin_trigger below.
    lang = context.get("lang")
    try:
        doctor = await hms_client.get_doctor_by_id(doctor_id)
    except HmsApiError:
        await conversation.whatsapp_client.send_text(client, phone, t("doctor_not_available", lang))
        return

    # Fresh clipboard, discarding whatever the patient was doing before -- scanning a
    # specific doctor's QR is unambiguously "start a new booking with THIS doctor", same
    # "start fresh" choice _handle_checkin_trigger below makes for check-in. Value shape
    # ({id, fullName}) matches exactly what the free-text doctor-name-search path fills this
    # slot with (see the two booking_slots.fill(booking, "doctor", ...) call sites above) --
    # everything downstream (location auto-infer, date/shift asks, final booking submission)
    # is shared code that doesn't know or care how the doctor slot got filled. Both existing
    # call sites ALSO set these three plain context keys alongside the slot fill (not derived
    # from booking["doctor"]["value"] anywhere downstream) -- _send_patient_details_flow reads
    # context["doctor_id"] directly to fetch offered slots, and the final booking-submission
    # step (line ~2086) reads it again to actually call hms_client.book_appointment. Missing
    # this was caught by test_doctor_booking_qr.py: without it, _send_patient_details_flow
    # sees no doctor_id, finds no slots, and silently falls back to "choosing_doctor" instead
    # of ever asking for a date.
    booking = booking_slots.empty()
    booking_slots.fill(
        booking, "doctor",
        {"id": doctor["doctorId"], "fullName": doctor["fullName"]},
        raw=doctor.get("fullName"), source="qr",
    )
    doctor_context_fields = {
        "doctor_id": doctor["doctorId"],
        "doctor_name": doctor.get("fullName") or "Doctor",
        "doctor_fee": _doctor_fee(doctor),
    }

    if lang:
        # Language already known from earlier this conversation -- skip straight to the
        # welcome + booking cascade, same branch _confirm_or_start_language takes on a
        # confident language detection.
        new_context = {"lang": lang, "booking": booking, **doctor_context_fields}
        booking_slots.fill(booking, "lang", lang, source="user")
        await conversation.whatsapp_client.send_text(client, phone, t("welcome_banner", lang))
        await conversation._advance_booking_flow(client, phone, new_context, booking)
    else:
        # First-ever contact (or language forgotten) -- ask, same as any other fresh booking
        # entry point. _start preserves whatever extra context keys are already set (see its
        # own `ctx = init_context or {}`), so the pre-filled doctor slot survives into
        # _handle_choosing_language's own _advance_booking_flow call once the patient picks one.
        await conversation._start(client, phone, {"booking": booking, **doctor_context_fields})


# ---------------------------------------------------------------------------------------
# OPD QR check-in — a separate, self-contained flow from booking above, entered only via the
# CHECKIN trigger in handle_message. Deliberately kept on its own current_step values rather
# than folded into booking_slots.py's clipboard model: the clipboard drives the *booking*
# flow's slot-filling, and check-in isn't a slot-filling problem (it's a fixed two-step
# location-share -> resolve sequence) -- reusing it here would mean teaching the clipboard
# about slots that only exist for a completely different flow.
# ---------------------------------------------------------------------------------------


async def _handle_checkin_trigger(
    client, phone: str, hospital_code: str, context: dict, current_step: str | None
) -> None:
    from app import conversation

    # Re-resolves independently of GET /c/{hospital_code}'s own validation (app/webhook.py) —
    # that redirect is a UX nicety, never the authoritative check, since the prefilled text is
    # just a string the patient's WhatsApp client could technically let them edit before send.
    try:
        hospital = await hms_client.get_hospital_by_code(hospital_code)
    except HmsApiError:
        lang = context.get("lang")
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_invalid_code", lang))
        return

    # Fresh context, not merged with whatever the patient was doing before (e.g. an
    # in-progress booking search) — check-in is an independent flow, and reusing stale keys
    # like doctor_options here would only risk cross-contamination.
    checkin_context = {
        "lang": context.get("lang"),
        "checkin_hospital_id": hospital["hospitalId"],
        "checkin_hospital_name": hospital.get("name") or "the hospital",
    }
    await conversation._transition_to(phone, "checkin_awaiting_location", checkin_context, current_step)
    lang = checkin_context.get("lang")
    await conversation.whatsapp_client.send_location_request(
        client, phone, t("checkin_location_prompt", lang, hospital_name=checkin_context["checkin_hospital_name"])
    )


async def _handle_checkin_awaiting_location(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type != "location":
        await conversation.whatsapp_client.send_text(
            client, phone, t("checkin_location_prompt", lang, hospital_name=context.get("checkin_hospital_name"))
        )
        return

    lat_str, lng_str = input_value.split(",")
    latitude, longitude = float(lat_str), float(lng_str)
    hospital_id = context["checkin_hospital_id"]

    result = await hms_client.resolve_checkin(hospital_id, mobile=phone, latitude=latitude, longitude=longitude)

    if result.get("success") and result.get("appointmentId"):
        await _finish_checkin(client, phone, lang, result["appointmentId"], result.get("tokenNo"))
        return

    candidates = result.get("candidates")
    if candidates:
        checkin_options = {c["appointmentId"]: c for c in candidates}
        new_context = {
            **context,
            "checkin_latitude": latitude,
            "checkin_longitude": longitude,
            "checkin_options": checkin_options,
        }
        new_context.pop("booking", None)  # _get_or_create_clipboard's side effect, not needed here
        await conversation._transition_to(phone, "checkin_choosing_appointment", new_context, "checkin_awaiting_location")
        rows = [
            (c["appointmentId"], c.get("doctorName") or "Doctor", c.get("startAt") or "")
            for c in candidates
        ]
        await conversation.whatsapp_client.send_list(
            client, phone, t("checkin_choose_appointment", lang), t("checkin_choose_button", lang), rows,
        )
        return

    # No candidates: either "too far" (geofence) or "nothing found today" — both are
    # success:false with no candidates, distinguished only by message text server-side.
    # There's no reliable machine-checkable signal to tell them apart here, so re-prompting
    # for location covers the geofence case (the more recoverable one) while the message
    # itself (not surfaced verbatim to avoid leaking backend wording) covers the other.
    message = result.get("message") or ""
    if "appointment" in message.lower():
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_no_appointment", lang))
        await conversation.db.clear_conversation_state(phone)
    else:
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_too_far", lang))


async def _handle_checkin_choosing_appointment(client, phone, input_type, input_value, context) -> None:
    from app import conversation

    lang = context.get("lang")
    if input_type != "list_reply":
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_choose_appointment", lang))
        return

    appointment_id = input_value
    if appointment_id not in context.get("checkin_options", {}):
        # Stale list (e.g. patient tapped an old message) — same guard as _handle_choosing_doctor.
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_choose_appointment", lang))
        return

    result = await hms_client.issue_queue_token(
        appointment_id, context["checkin_latitude"], context["checkin_longitude"]
    )

    if result.get("success"):
        await _finish_checkin(client, phone, lang, appointment_id, result.get("tokenNo"))
    else:
        await conversation.whatsapp_client.send_text(client, phone, t("checkin_failed", lang))
        await conversation.db.clear_conversation_state(phone)


async def _finish_checkin(client, phone, lang: str | None, appointment_id: str, token_no: int | None) -> None:
    from app import conversation

    # Registers this appointment into the same table POST /events/token-called reads from
    # (app/db.py:get_appointment_by_hms_id) — without this, a walk-in whose appointment wasn't
    # booked through this bot would check in successfully but never receive a queue-update
    # push, since that lookup only ever found bot-booked appointments before this call existed.
    await conversation.db.upsert_checkin_notification(appointment_id, phone, lang)
    await conversation.whatsapp_client.send_text(client, phone, t("checkin_success", lang, token_no=token_no))
    await conversation.db.clear_conversation_state(phone)
