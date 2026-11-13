"""
app/conversation/vita_chat.py
-----------------------------
Vita (the chat agent in the NLP Router) as the front door of the doctor search: symptoms, a doctor's name, a
specialty or an area, said in the patient's own words. Vita searches and suggests; the moment the patient picks a
doctor, this bot takes over again with its own slot, patient-details, confirm and booking steps, unchanged.

Switched on per number (VITA_ALLOWED_PHONES). Anything that goes wrong with Vita -- not reachable, an error, a
reply this bot cannot use -- drops that conversation back to the fixed buttons it always had.

Cross-references back into app/conversation/__init__.py go through a function-body-local
`from app import conversation`, same as the other domain modules there.
"""
import logging
import re

import httpx

from app.config import settings
from app.i18n import t
from app.messengers import hms_client, vita_client
from app.messengers.hms_client import HmsApiError
from app.messengers.vita_client import VitaRejected, VitaUnavailable
from app.pii import mask_phone
from app.types import ConversationContext

logger = logging.getLogger("conversation")

STEP = "vita_chat"

# Things Vita cannot do (it only searches), said in words rather than a tap: leave Vita and let the bot's own
# intent handling take it. English, Hinglish, Hindi and Bengali.
_APPOINTMENT_MANAGEMENT = re.compile(
    r"cancel|resched|postpone|appointment\s+(status|dekh|badal)|my\s+appointment|meri\s+appointment"
    r"|कैंसिल|रद्द|रीशेड्यूल|বাতিল|রিশিডিউল",
    re.IGNORECASE,
)
_EXACT_COMMANDS = {"cancel", "quit", "back"}


def _tail(number: str) -> str:
    """The last 10 digits, so +91 98765 43210, 919876543210 and 9876543210 are the same number."""
    return re.sub(r"\D", "", number or "")[-10:]


def enabled_for(phone: str, context: ConversationContext | None = None) -> bool:
    if not vita_client.is_configured():
        return False
    if context and context.get("vita_off"):
        return False
    allowed = [a.strip() for a in (settings.vita_allowed_phones or "").split(",") if a.strip()]
    if "*" in allowed:
        return True
    tail = _tail(phone)
    return bool(tail) and any(_tail(a) == tail for a in allowed if a != "*")


async def enter(client: httpx.AsyncClient, phone: str, context: ConversationContext) -> None:
    from app import conversation

    context.pop("vita_session_id", None)
    await conversation._transition_to(phone, STEP, context, "choosing_location")
    await conversation.whatsapp_client.send_text(client, phone, t("vita_intro", context.get("lang")))


async def prompt(client: httpx.AsyncClient, phone: str, context: ConversationContext) -> None:
    """Reached by "back" navigation: start a fresh conversation with Vita."""
    from app import conversation

    context.pop("vita_session_id", None)
    await conversation.whatsapp_client.send_text(client, phone, t("vita_intro", context.get("lang")))


async def maybe_handle(
    client: httpx.AsyncClient, phone: str, context: ConversationContext, current_step: str | None,
    input_type: str, input_value: str,
) -> bool:
    """True when this turn is over. Only acts while the conversation is in the Vita step."""
    from app import conversation

    if current_step != STEP:
        return False
    if not enabled_for(phone, context):
        await _fall_back(client, phone, context, notify=False)
        return True
    if input_type == "text":
        text = input_value.strip()
        if text.lower() in _EXACT_COMMANDS:
            lang = context.get("lang")
            return await conversation._maybe_handle_manual_command(client, phone, context, current_step, lang, input_type, input_value)
        if _APPOINTMENT_MANAGEMENT.search(text):
            context["vita_off"] = True
            context.pop("vita_session_id", None)
            await conversation.db.save_conversation_state(phone, "choosing_search_mode", context)
            return False
    await _turn(client, phone, context, input_type, input_value)
    return True


async def handle_step(client, phone, input_type, input_value, context: ConversationContext) -> None:
    """STEP_REGISTRY handler. maybe_handle answers every Vita turn before the registry is reached, so this only
    runs after it chose to hand a message back to the bot (Vita switched off for this conversation)."""
    from app.conversation import specialty_browsing

    if context.get("vita_off"):
        await specialty_browsing._handle_choosing_search_mode(client, phone, input_type, input_value, context)
        return
    await _turn(client, phone, context, input_type, input_value)


def _vita_input(input_type: str, input_value: str) -> dict | None:
    if input_type == "text":
        return {"text": input_value} if input_value.strip() else None
    if input_type in ("list_reply", "button_reply"):
        return {"reply_id": input_value} if input_value else None
    if input_type == "location":
        try:
            lat, lng = (float(x) for x in input_value.split(","))
        except ValueError:
            return None
        return {"lat": lat, "lng": lng}
    return None


async def _turn(client: httpx.AsyncClient, phone: str, context: ConversationContext, input_type: str, input_value: str) -> None:
    from app import conversation

    args = _vita_input(input_type, input_value)
    if args is None:
        await conversation.whatsapp_client.send_text(client, phone, t("vita_intro", context.get("lang")))
        return

    city = context.get("city")
    try:
        try:
            reply = await vita_client.chat(**args, session_id=context.get("vita_session_id"), city=city)
        except VitaRejected as exc:
            if exc.status_code != 422:
                raise
            # An expired session, or a row from an old message. A typed message can simply start over; a tap on
            # an old row cannot be answered, so the patient is asked again.
            context.pop("vita_session_id", None)
            if "text" not in args:
                await conversation.db.save_conversation_state(phone, STEP, context)
                await conversation.whatsapp_client.send_text(client, phone, t("vita_intro", context.get("lang")))
                return
            reply = await vita_client.chat(**args, session_id=None, city=city)
    except VitaUnavailable:
        await _fall_back(client, phone, context, notify=True)
        return

    context["vita_session_id"] = reply.get("session_id") or context.get("vita_session_id")
    if reply["kind"] == "doctor_selected":
        await _hand_over(client, phone, context, reply)
        return

    messages = reply.get("whatsapp") or []
    for message in messages:
        await conversation.whatsapp_client.send_prepared(client, phone, message)
    if not messages and reply.get("message"):
        await conversation.whatsapp_client.send_text(client, phone, reply["message"])
    await conversation.db.save_conversation_state(phone, STEP, context)


async def _hand_over(client: httpx.AsyncClient, phone: str, context: ConversationContext, reply: dict) -> None:
    """The patient picked a doctor in Vita. Continue exactly as if they had tapped that doctor in the bot's own list:
    the doctor is read fresh from 1HMS (same shape the list has), then the bot's day, shift and details steps run."""
    from app import conversation

    doctor_id = (reply.get("doctor") or {}).get("doctorId") or reply.get("selectedDoctorId")
    doctor = None
    if doctor_id:
        try:
            doctor = await hms_client.get_doctor_by_id(doctor_id)
        except (HmsApiError, httpx.HTTPError) as exc:
            logger.warning("Could not load the doctor Vita selected for %s: %s", mask_phone(phone), exc)
    if not doctor:
        await conversation.whatsapp_client.send_text(client, phone, t("error_hms", context.get("lang")))
        await conversation.db.save_conversation_state(phone, STEP, context)
        return

    context.pop("vita_session_id", None)
    context["doctor_options"] = {doctor_id: doctor}
    await conversation._handle_choosing_doctor(client, phone, "list_reply", doctor_id, context)


async def _fall_back(client: httpx.AsyncClient, phone: str, context: ConversationContext, *, notify: bool) -> None:
    """Vita cannot serve this conversation: the fixed buttons, for the rest of it."""
    from app import conversation
    from app.conversation import specialty_browsing

    context["vita_off"] = True
    context.pop("vita_session_id", None)
    if notify:
        await conversation.whatsapp_client.send_text(client, phone, t("vita_unavailable", context.get("lang")))
    await specialty_browsing._send_search_mode_prompt(client, phone, context)
