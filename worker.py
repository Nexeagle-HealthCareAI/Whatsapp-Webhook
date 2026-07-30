import asyncio
import json
import logging

import httpx

from app import conversation, db
from app.config import settings
from app.redis_client import get_redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")


async def handle_job(client: httpx.AsyncClient, job: dict) -> None:
    message_id = job.get("message_id")
    sender = job["sender"]

    # Durable backstop beyond Redis's TTL-based dedupe (app/webhook.py) — belt and
    # suspenders against a duplicate booking if a job is ever replayed after that
    # Redis key has expired.
    if message_id and await db.is_message_processed(message_id):
        logger.info("Message %s already processed, skipping", message_id)
        return

    logger.info("Processing message %s from %s", message_id, sender)
    await conversation.handle_message(
        client,
        sender,
        job.get("sender_name"),
        job.get("input_type") or "text",
        job.get("input_value") or "",
    )

    if message_id:
        await db.mark_message_processed(message_id)


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
