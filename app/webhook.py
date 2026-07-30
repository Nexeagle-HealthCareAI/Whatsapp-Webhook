import hashlib
import hmac
import json
import logging
import time

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.config import settings
from app.redis_client import get_redis

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


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def _extract_messages(payload: dict) -> list[dict]:
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            messages.extend(change.get("value", {}).get("messages", []))
    return messages


def _message_text(message: dict) -> str | None:
    msg_type = message.get("type")
    if msg_type == "text":
        return message.get("text", {}).get("body")
    if msg_type == "interactive":
        interactive = message.get("interactive", {})
        if interactive.get("type") == "list_reply":
            return interactive.get("list_reply", {}).get("id")
        if interactive.get("type") == "button_reply":
            return interactive.get("button_reply", {}).get("id")
    return None


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

    for message in _extract_messages(payload):
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

        job = {
            "message_id": message_id,
            "sender": sender,
            "text": _message_text(message),
            "received_at": time.time(),
        }
        await redis.lpush(settings.booking_jobs_key, json.dumps(job))
        logger.info("Enqueued message %s from %s", message_id, sender)

    # Always 200 — Meta retries on anything else, and retries are exactly what dedupe exists
    # to absorb, not what should trigger more of them.
    return {"status": "ok"}
