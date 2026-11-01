"""
app/vita_bridge.py
------------------
Vita mode: the patient conversation is handled by Vita, the patient agent in the 1HMS NLP
Router (POST /v2/agent/chat with channel "whatsapp"), not by this bot's own NLU / safety
triage / step machine. The contract Vita implements is 1HMS-NLP-Router/docs/vita_whatsapp.md:

  patient text        -> {channel, session_id, text}
  tapped row / button -> {channel, session_id, reply_id}   (the id exactly as Vita wrote it)
  shared location     -> {channel, session_id, lat, lng}
  reply["whatsapp"]   -> ready-to-send Cloud API messages (minus "to"), sent here in order

The bot never builds Vita's messages and never parses Vita's ids. Switched on only when
settings.vita_chat_url is set; see conversation.handle_message (_maybe_hand_to_vita) for the
seam, and stays_with_bot() below for which messages remain with the bot's own flows.

One brain, one safety layer: when Vita answers a message, the bot's own safety triage and
NLU do not run for it -- Vita owns emergencies (108 / 112 first) for its conversations.
"""

import json
import logging
import re

import httpx

from app.config import settings
from app.i18n import t
from app.messengers import conversation_log_queue, outbound_queue
from app.messengers.redis_client import get_redis
from app.pii import mask_phone

logger = logging.getLogger("vita_bridge")

# Vita's session lasts 30 minutes after the last message (router side); ours matches it and is
# refreshed on every message, so a patient picking up within 30 minutes stays in the same chat.
SESSION_TTL_SECONDS = 30 * 60
_SESSION_KEY = "vita:wa:session:{phone}"

# Set while the patient is inside one of the bot's OWN flows (QR check-in, QR booking, hospital
# QR menu, follow-up reply, appointment actions). Those flows continue through the bot's generic
# steps (language, slot, patient details, confirm...), so while this marker is alive AND the bot
# still has a conversation step saved for the patient, the next message stays with the bot too.
BOT_FLOW_TTL_SECONDS = 30 * 60
_BOT_FLOW_KEY = "vita:wa:botflow:{phone}"

# Button ids only the bot's own messages carry (conversation._maybe_handle_hospbook_button and
# _maybe_handle_start_booking_button). WhatsApp keeps old buttons tappable forever.
_BOT_BUTTON_IDS = frozenset({"hospbook_book", "hospbook_status", "start_booking"})

# Conversation steps that belong to the bot's own non-conversation flows (see STEP_REGISTRY in
# app/conversation/__init__.py): the day-after follow-up (scheduler.py ->
# _maybe_handle_followup_reply_step), appointment actions (appointment_actions.py) and the
# hospital-QR action menu / OPD check-in (checkin.py).
BOT_OWNED_STEPS = frozenset({
    "awaiting_followup_reply", "confirming_followup_booking",
    "choosing_appointment_to_cancel", "choosing_appointment_to_reschedule",
    "choosing_appointment_to_view", "confirming_appointment_cancel",
    "confirming_appointment_reschedule", "viewing_appointment_status",
    "awaiting_reschedule_date",
    "choosing_hospital_action", "checkin_awaiting_location", "checkin_choosing_appointment",
})

# Bot language code (app/i18n.py) <-> Vita's ui_language / reply "language".
_BOT_TO_VITA_LANG = {"en": "en", "hi": "hi", "hg": "hinglish", "bn": "bn"}
_VITA_TO_BOT_LANG = {"english": "en", "en": "en", "hindi": "hi", "hi": "hi", "hinglish": "hg",
                     "bangla": "bn", "bengali": "bn", "bn": "bn"}

_VERIFIED_MOBILE = re.compile(r"^\+?\d{10,13}$")  # the router's own pattern for verified_mobile


def _is_qr_trigger(text: str) -> bool:
    # Imported here, not at module level: app.conversation imports this module.
    from app.conversation.checkin import (
        _CHECKIN_TRIGGER_PATTERN, _DOCTOR_BOOKING_TRIGGER_PATTERN, _DOCUMENT_TRIGGERS,
        _HOSPITAL_BOOKING_TRIGGER_PATTERN,
    )
    s = text.strip()
    if _CHECKIN_TRIGGER_PATTERN.match(s) or _DOCTOR_BOOKING_TRIGGER_PATTERN.match(s):
        return True
    if _HOSPITAL_BOOKING_TRIGGER_PATTERN.search(s):   # .search: see that pattern's own comment
        return True
    return any(entry[0].match(s) for entry in _DOCUMENT_TRIGGERS)


def is_bot_owned(input_type: str, input_value: str, current_step: str | None) -> bool:
    """True for a message that STARTS or CONTINUES one of the bot's own flows on its own
    evidence (no memory needed):
      - nfm_reply: a WhatsApp Flow submission (the bot's patient-details Flow)
      - button_reply hospbook_book / hospbook_status / start_booking
      - text that is a deterministic QR trigger: CHECKIN <code>, DRBOOK <id>, the hospital-QR
        text ("... QR ID of this hospital is <code>"), DISCHARGE / RX / RXV <id>
      - any message while the bot's saved step is one of BOT_OWNED_STEPS"""
    if input_type == "nfm_reply":
        return True
    if input_type == "button_reply" and input_value in _BOT_BUTTON_IDS:
        return True
    if input_type == "text" and input_value and _is_qr_trigger(input_value):
        return True
    return current_step in BOT_OWNED_STEPS


async def stays_with_bot(redis, phone: str, input_type: str, input_value: str, current_step: str | None) -> bool:
    """The routing rule for Vita mode. A message stays with the bot's old handler when:
      1. is_bot_owned() says so (and that marks the patient as inside a bot flow for 30 min), or
      2. the patient is inside a bot flow started within the last 30 minutes (marker set by 1,
         refreshed by every message it keeps) AND the bot still has a conversation step saved
         -- e.g. DRBOOK -> language -> slot -> details -> confirm all stay with the bot.
    Everything else goes to Vita. When the bot's flow ends it clears its own conversation
    state, so the next message goes to Vita even if the marker has not expired yet."""
    key = _BOT_FLOW_KEY.format(phone=phone)
    try:
        if is_bot_owned(input_type, input_value, current_step):
            await redis.set(key, "1", ex=BOT_FLOW_TTL_SECONDS)
            return True
        in_bot_flow = await redis.get(key)
        if in_bot_flow and current_step:
            await redis.set(key, "1", ex=BOT_FLOW_TTL_SECONDS)
            return True
        if in_bot_flow:
            await redis.delete(key)
    except Exception:
        # Redis trouble: decide on the message's own evidence alone, never crash the turn.
        logger.warning("Bot-flow marker unavailable for %s", mask_phone(phone))
        return is_bot_owned(input_type, input_value, current_step)
    return False


def _load_session(raw) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def _vita_lang(session: dict, lang: str | None) -> str | None:
    """Bot language code for our own short texts (expired list / unavailable): the language Vita
    last spoke in this session, else the bot's own saved language."""
    return _VITA_TO_BOT_LANG.get(str(session.get("language") or "").lower()) or lang


def _request_body(phone: str, input_type: str, input_value: str, session_id: str | None,
                  lang: str | None) -> dict | None:
    body: dict = {"channel": "whatsapp"}
    if session_id:
        body["session_id"] = session_id
    if input_type == "text":
        if not (input_value or "").strip():
            return None
        body["text"] = input_value[:1000]          # the router's max_length for text
    elif input_type in ("list_reply", "button_reply"):
        if not input_value:
            return None
        body["reply_id"] = input_value             # exactly as received -- never parsed here
    elif input_type == "location":
        try:
            lat_s, lng_s = (input_value or "").split(",", 1)
            body["lat"], body["lng"] = float(lat_s), float(lng_s)
        except ValueError:
            return None
    else:
        return None
    if phone and _VERIFIED_MOBILE.match(phone):
        body["verified_mobile"] = phone            # WhatsApp already verified the sender
    if lang and lang in _BOT_TO_VITA_LANG:
        body["ui_language"] = _BOT_TO_VITA_LANG[lang]
    return body


def _messages_to_send(reply) -> list[dict] | None:
    """The Cloud API messages to send, or None when the reply isn't a shape we understand."""
    if not isinstance(reply, dict):
        return None
    wa = reply.get("whatsapp")
    if isinstance(wa, list) and wa:
        if not all(isinstance(m, dict) and m.get("type") for m in wa):
            return None
        return wa
    message = reply.get("message")
    if isinstance(message, str) and message.strip():
        return [{"type": "text", "text": {"body": message[:4096]}}]
    return None


async def send_text(phone: str, body: str) -> None:
    await outbound_queue.enqueue(get_redis(), {"messaging_product": "whatsapp", "to": phone, "text": {"body": body}})


async def send_unavailable(phone: str, lang: str | None) -> None:
    """VITA_FALLBACK=message: one short apology, always with the 108 line."""
    raw = None
    try:
        raw = await get_redis().get(_SESSION_KEY.format(phone=phone))
    except Exception:
        pass
    await send_text(phone, t("vita_unavailable", _vita_lang(_load_session(raw), lang)))


async def handle_with_vita(
    client: httpx.AsyncClient, phone: str, sender_name: str | None, input_type: str,
    input_value: str, message_id: str | None = None, lang: str | None = None,
) -> bool:
    """One patient message -> Vita -> Vita's messages to the patient, in order.

    Returns True when the patient got an answer (Vita's messages, or the "list has expired"
    text for a stale doctor row), False on 5xx / timeout / network error / a reply shape we
    don't understand -- the caller then applies settings.vita_fallback. Never raises.
    `lang` is the bot's own saved language for this patient (en/hi/hg/bn), if any, sent as
    ui_language (Vita uses it only until the patient's own words show a language)."""
    if not settings.vita_chat_url:
        return False
    redis = get_redis()
    key = _SESSION_KEY.format(phone=phone)
    try:
        session = _load_session(await redis.get(key))
    except Exception:
        logger.warning("Could not read Vita session for %s", mask_phone(phone))
        session = {}
    body = _request_body(phone, input_type, input_value, session.get("session_id"), lang)
    if body is None:
        logger.info("Vita: nothing to send for input_type=%s from %s", input_type, mask_phone(phone))
        return False

    headers = {"X-Api-Key": settings.vita_api_key} if settings.vita_api_key else {}
    try:
        response = await client.post(settings.vita_chat_url, json=body, headers=headers,
                                     timeout=settings.vita_timeout_seconds)
    except httpx.HTTPError as exc:
        logger.warning("Vita unreachable for %s: %s", mask_phone(phone), exc.__class__.__name__)
        return False
    except Exception:
        logger.exception("Vita call failed for %s", mask_phone(phone))
        return False

    if response.status_code == 422 and str(body.get("reply_id", "")).startswith("doc:"):
        # A doctor row from an old list Vita no longer has in this session.
        await _refresh_session(redis, key, session)
        await send_text(phone, t("vita_list_expired", _vita_lang(session, lang)))
        return True
    if response.status_code != 200:
        logger.warning("Vita returned HTTP %s for %s", response.status_code, mask_phone(phone))
        return False
    try:
        reply = response.json()
    except ValueError:
        logger.warning("Vita returned a non-JSON body for %s", mask_phone(phone))
        return False
    messages = _messages_to_send(reply)
    if messages is None:
        logger.warning("Vita reply had no sendable messages for %s (kind=%s)", mask_phone(phone),
                       reply.get("kind") if isinstance(reply, dict) else None)
        return False

    if reply.get("session_id"):
        session["session_id"] = reply["session_id"]
    if reply.get("language"):
        session["language"] = reply["language"]
    await _refresh_session(redis, key, session)
    await _log_turn(redis, phone, session, input_type, input_value, reply)

    sent = 0
    try:
        for message in messages:
            await outbound_queue.enqueue(redis, {**message, "messaging_product": "whatsapp", "to": phone})
            sent += 1
    except Exception:
        logger.exception("Could not queue Vita's messages for %s (%d of %d queued)",
                         mask_phone(phone), sent, len(messages))
        return sent > 0     # part of the answer is already on its way: don't add a fallback to it
    logger.info("Vita answered %s: kind=%s, %d message(s)", mask_phone(phone), reply.get("kind"), len(messages))
    handover = reply.get("handover") if isinstance(reply.get("handover"), dict) else {}
    if handover.get("to") == "booking" and handover.get("doctorId"):
        await _hand_over_booking(client, redis, phone, str(handover["doctorId"]), _vita_lang(session, lang))
    return True


async def _hand_over_booking(client, redis, phone: str, doctor_id: str, lang: str | None) -> None:
    """Vita found the doctor and isn't booking it (router booking off): the bot's own booking flow
    takes over for that doctor (the DRBOOK path, without its welcome banner), in the language Vita
    was speaking, and the next messages stay with the bot until that flow ends (stays_with_bot)."""
    from app.conversation.checkin import start_doctor_booking   # app.conversation imports this module
    try:
        await redis.set(_BOT_FLOW_KEY.format(phone=phone), "1", ex=BOT_FLOW_TTL_SECONDS)
    except Exception:
        logger.warning("Bot-flow marker not set for %s", mask_phone(phone))
    try:
        await start_doctor_booking(client, phone, doctor_id, lang, greet=False)
        logger.info("Vita handed %s over to the bot's booking flow", mask_phone(phone))
    except Exception:
        logger.exception("Booking hand-over failed for %s", mask_phone(phone))


async def _log_turn(redis, phone: str, session: dict, input_type: str, input_value: str, reply: dict) -> None:
    """Vita's turns in the bot's conversation log (dbo.conversation_sessions), under the session id
    "vita:<id>": the patient's message, what Vita did (step "vita:<kind>") with its words, and a
    conversion when Vita booked. Never fails the turn (log_event swallows its own errors)."""
    sid = session.get("session_id")
    if not sid:
        return
    log_sid, kind = f"vita:{sid}", str(reply.get("kind") or "reply")
    step = f"vita:{kind}"[:50]
    content = input_value if input_type != "location" else "[location]"
    await conversation_log_queue.log_event(redis, log_sid, phone, "in", input_type, content, step)
    await conversation_log_queue.log_event(redis, log_sid, phone, "out", "vita", (reply.get("message") or "")[:1000], step)
    appointment = (reply.get("booking") or {}).get("appointmentId") if isinstance(reply.get("booking"), dict) else None
    if kind == "booked" and appointment:
        await conversation_log_queue.log_conversion(redis, log_sid, str(appointment))


async def _refresh_session(redis, key: str, session: dict) -> None:
    if not session.get("session_id"):
        return
    try:
        await redis.set(key, json.dumps(session), ex=SESSION_TTL_SECONDS)
    except Exception:
        logger.warning("Could not save the Vita session id")
