import logging

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import db, i18n
from app.config import settings
from app.messengers.redis_client import get_redis
from app.messengers.whatsapp_client import send_text

logger = logging.getLogger("webhook.hms_events")
router = APIRouter()


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

    is_new = await redis.set(
        f"booking:dedupe:event:{event.eventId}",
        "1",
        nx=True,
        ex=settings.message_dedupe_ttl_seconds,
    )
    if not is_new:
        logger.info("Duplicate token-called event %s, skipping", event.eventId)
        return {"status": "ok"}

    row = await db.get_appointment_by_hms_id(event.appointmentId)
    if row is None:
        logger.info("token-called for unknown appointment %s, ignoring", event.appointmentId)
        return {"status": "ok"}

    await db.save_queue_status(
        event.appointmentId, event.currentToken, event.estimatedWaitMinutes
    )

    wait_note = (
        f", ~{event.estimatedWaitMinutes} min wait"
        if event.estimatedWaitMinutes is not None
        else ""
    )
    text = {
        "en": f"Queue update: currently serving token #{event.currentToken}{wait_note}.",
        "hi": f"क्यू अपडेट: अभी टोकन #{event.currentToken} चल रहा है{wait_note}।",
        "hg": f"Queue update: abhi token #{event.currentToken} chal raha hai{wait_note}.",
    }.get(row["preferred_language"] or i18n.DEFAULT_LANG)

    async with httpx.AsyncClient(timeout=10) as client:
        await send_text(client, row["phone_number"], text)

    return {"status": "ok"}
