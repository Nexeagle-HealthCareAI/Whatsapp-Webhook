import asyncio
import json
import logging

import httpx

from app.config import settings
from app.redis_client import get_redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{settings.whatsapp_phone_number_id}/messages"

# Step 1 prototype only — replaced by real conversation logic in Phase 2.
STATIC_REPLY = "Thanks for your message! Booking system is under construction."


async def send_whatsapp_text(client: httpx.AsyncClient, to: str, body: str) -> None:
    response = await client.post(
        GRAPH_API_URL,
        headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "text": {"body": body},
        },
    )
    logger.info("WhatsApp send to %s -> %s", to, response.status_code)
    if response.status_code >= 400:
        logger.error("WhatsApp send failed: %s", response.text)


async def handle_job(client: httpx.AsyncClient, job: dict) -> None:
    sender = job["sender"]
    logger.info("Processing message %s from %s", job.get("message_id"), sender)
    await send_whatsapp_text(client, sender, STATIC_REPLY)


async def main() -> None:
    redis = get_redis()
    async with httpx.AsyncClient(timeout=10) as client:
        logger.info("Worker started, waiting on %s", settings.booking_jobs_key)
        while True:
            item = await redis.brpop(settings.booking_jobs_key, timeout=5)
            if item is None:
                continue
            _, raw_job = item
            try:
                job = json.loads(raw_job)
                await handle_job(client, job)
            except Exception:
                logger.exception("Failed to process job: %s", raw_job)


if __name__ == "__main__":
    asyncio.run(main())
