import hashlib
import hmac
import json
import logging
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Header, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app import db, hms_client, i18n
from app.config import settings
from app.hms_client import HmsApiError
from app.redis_client import get_redis
from app.whatsapp_client import send_text

logger = logging.getLogger("webhook")

router = APIRouter()


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    if hub_mode != "subscribe" or hub_verify_token != settings.whatsapp_verify_token:
        raise HTTPException(status_code=403, detail="Verification token mismatch")
    return Response(content=hub_challenge, media_type="text/plain")


@router.get("/c/{hospital_code}")
async def checkin_qr_redirect(hospital_code: str):
    """Physical OPD QR target. Validates the code server-side before ever opening WhatsApp —
    conversation.py re-validates it independently when the CHECKIN message actually arrives
    (this is a UX nicety, catching a dead/mistyped code before the redirect, not the
    authoritative check). Not wired to worker.py/Redis at all: nothing async needed here."""
    if not settings.whatsapp_display_number:
        logger.warning("GET /c/%s hit but WHATSAPP_DISPLAY_NUMBER isn't configured yet", hospital_code)
        return Response(
            content="Check-in isn't set up yet at this hospital. Please check in at reception.",
            media_type="text/plain",
            status_code=503,
        )

    try:
        hospital = await hms_client.get_hospital_by_code(hospital_code)
    except HmsApiError:
        return Response(
            content="This QR code isn't valid. Please ask reception for help.",
            media_type="text/plain",
            status_code=404,
        )

    logger.info("QR scan for hospital %s (%s)", hospital.get("hospitalId"), hospital.get("name"))
    wa_text = quote(f"CHECKIN {hospital_code}")
    return RedirectResponse(f"https://wa.me/{settings.whatsapp_display_number}?text={wa_text}")


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def _extract_messages_and_contacts(payload: dict) -> tuple[list[dict], dict[str, str]]:
    messages = []
    names_by_wa_id: dict[str, str] = {}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages.extend(value.get("messages", []))
            for contact in value.get("contacts", []):
                wa_id = contact.get("wa_id")
                name = contact.get("profile", {}).get("name")
                if wa_id and name:
                    names_by_wa_id[wa_id] = name
    return messages, names_by_wa_id


def _input_type_and_value(message: dict) -> tuple[str, str] | tuple[None, None]:
    msg_type = message.get("type")
    if msg_type == "text":
        body = message.get("text", {}).get("body")
        return ("text", body) if body else (None, None)
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "list_reply":
            row_id = interactive.get("list_reply", {}).get("id")
            return ("list_reply", row_id) if row_id else (None, None)
        if interactive.get("type") == "button_reply":
            btn_id = interactive.get("button_reply", {}).get("id")
            return ("button_reply", btn_id) if btn_id else (None, None)
        if interactive.get("type") == "nfm_reply":
            response_json = interactive.get("nfm_reply", {}).get("response_json")
            return ("nfm_reply", response_json) if response_json else (None, None)
    if msg_type == "location":
        # Patient tapped "Send your current location" in response to send_location_request()
        # (app/whatsapp_client.py). Encoded as "lat,lng" — conversation.py parses this back
        # into floats; kept as a plain string since every job on the Redis queue is
        # string-valued (see the job dict below), same convention as every other input type.
        location = message.get("location", {})
        lat, lng = location.get("latitude"), location.get("longitude")
        if lat is None or lng is None:
            return None, None
        return "location", f"{lat},{lng}"
    return None, None


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
):
    raw_body = await request.body()
    if not _verify_signature(raw_body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(raw_body)
    redis = get_redis()

    messages, names_by_wa_id = _extract_messages_and_contacts(payload)

    for message in messages:
        message_id = message.get("id")
        sender = message.get("from")
        if not message_id or not sender:
            continue

        # SET NX with a TTL: only the first webhook delivery for a given message id wins the
        # key, so a Meta retry of the same message is a no-op past this point.
        is_new = await redis.set(
            f"booking:dedupe:{message_id}",
            "1",
            nx=True,
            ex=settings.message_dedupe_ttl_seconds,
        )
        if not is_new:
            logger.info("Duplicate delivery of message %s, skipping enqueue", message_id)
            continue

        input_type, input_value = _input_type_and_value(message)
        if input_type is None:
            logger.info("Ignoring unsupported message type from %s: %s", sender, message.get("type"))
            continue

        job = {
            "message_id": message_id,
            "sender": sender,
            "sender_name": names_by_wa_id.get(sender),
            "input_type": input_type,
            "input_value": input_value,
            "received_at": time.time(),
        }
        await redis.lpush(settings.booking_jobs_key, json.dumps(job))
        logger.info("Enqueued message %s from %s", message_id, sender)

    # Always 200 — Meta retries on anything else, and retries are exactly what dedupe exists
    # to absorb, not what should trigger more of them.
    return {"status": "ok"}


# ---------------------------------------------------------------------------------------
# 1HMS -> Gateway event push: "live queue" (requirement 8). This is the piece that couldn't
# exist before this change — there was no route for 1HMS to call at all. What this does NOT
# solve yet: 1HMS actually calling it. Per the existing project docs this event was listed
# as "not yet built" on the 1HMS side as of the last status snapshot; this endpoint is the
# gateway half of that contract, ready the moment 1HMS starts sending. Until then this is
# simply never invoked, which is safe (no behaviour change) rather than broken.
# ---------------------------------------------------------------------------------------


class TokenCalledEvent(BaseModel):
    eventId: str
    appointmentId: str
    currentToken: int
    estimatedWaitMinutes: int | None = None


def _check_internal_token(x_internal_token: str | None) -> None:
    if not x_internal_token or x_internal_token != settings.internal_events_token:
        raise HTTPException(status_code=401, detail="Invalid internal events token")


@router.post("/events/token-called")
async def token_called(
    event: TokenCalledEvent,
    x_internal_token: str | None = Header(default=None),
):
    _check_internal_token(x_internal_token)
    redis = get_redis()

    # Same dedupe pattern as the WhatsApp webhook (Section 2.4 of the build spec) — 1HMS is
    # asked to retry on a failed ack (Section 3.4, non-functional reqs), so this side needs
    # the same "second delivery of the same eventId is a no-op" guarantee.
    is_new = await redis.set(
        f"booking:dedupe:event:{event.eventId}", "1", nx=True, ex=settings.message_dedupe_ttl_seconds
    )
    if not is_new:
        logger.info("Duplicate token-called event %s, skipping", event.eventId)
        return {"status": "ok"}

    row = await db.get_appointment_by_hms_id(event.appointmentId)
    if row is None:
        # Appointment isn't one this bot booked (e.g. booked at the front desk, or a stale
        # ID) — nothing to notify on WhatsApp for. Not an error; ack normally so 1HMS
        # doesn't retry forever.
        logger.info("token-called for unknown appointment %s, ignoring", event.appointmentId)
        return {"status": "ok"}

    await db.save_queue_status(event.appointmentId, event.currentToken, event.estimatedWaitMinutes)

    wait_note = (
        f", ~{event.estimatedWaitMinutes} min wait" if event.estimatedWaitMinutes is not None else ""
    )
    text = {
        "en": f"Queue update: currently serving token #{event.currentToken}{wait_note}.",
        "hi": f"क्यू अपडेट: अभी टोकन #{event.currentToken} चल रहा है{wait_note}।",
        "hg": f"Queue update: abhi token #{event.currentToken} chal raha hai{wait_note}.",
    }.get(row["preferred_language"] or i18n.DEFAULT_LANG)

    async with httpx.AsyncClient(timeout=10) as client:
        await send_text(client, row["phone_number"], text)

    return {"status": "ok"}
