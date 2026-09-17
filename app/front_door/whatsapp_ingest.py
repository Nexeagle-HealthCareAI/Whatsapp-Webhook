import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.config import settings
from app.messengers.redis_client import get_redis

logger = logging.getLogger("webhook.whatsapp_ingest")
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
            # Dev and Prod are separate Meta Apps/WABAs, each meant to only ever receive
            # webhooks for its OWN phone_number_id -- but that routing is a manual setting in
            # each Meta App's dashboard (see the Caddyfile's own comment), nothing on Meta's
            # side stops one App's dashboard from being pointed at the other environment's
            # callback URL by mistake. Live-reported: exactly that happened once, and this
            # server silently processed and replied to a message meant for the other
            # environment's number, since signature verification alone doesn't distinguish
            # "signed by a DIFFERENT correctly-configured Meta App" from "misrouted to us" --
            # only phone_number_id in the payload itself says which number a message was
            # actually sent to. This is the one place that check belongs: every downstream
            # caller (worker.py, conversation.py) already assumes the phone_number_id it holds
            # in settings IS the one it should be replying from, and has no way to second-guess
            # that per-message.
            incoming_phone_number_id = value.get("metadata", {}).get("phone_number_id")
            if incoming_phone_number_id and incoming_phone_number_id != settings.whatsapp_phone_number_id:
                logger.warning(
                    "Ignoring webhook for phone_number_id %s -- this server is configured for %s "
                    "(Meta App dashboard's callback URL may be pointed at the wrong environment)",
                    incoming_phone_number_id, settings.whatsapp_phone_number_id,
                )
                continue
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

    return {"status": "ok"}
