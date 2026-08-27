import asyncio
import json
import logging

import httpx

from app import conversation, db
from app.config import settings
from app.messengers import city_index
from app.messengers.redis_client import get_redis

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
        message_id,
    )

    if message_id:
        await db.mark_message_processed(message_id)


async def warm_city_index() -> None:
    """Build the city index up front so no patient waits on it mid-conversation.

    Only the first run after a deploy (or after the cache expires) does real work — it pages
    the whole public doctor directory, which takes seconds, not milliseconds. Non-fatal: if
    1HMS is unreachable at boot the worker still starts, and the index is rebuilt lazily on
    first use. See app/city_index.py."""
    try:
        from app.debug_hms_db import main as run_debug_db
        run_debug_db()
    except Exception as e:
        logger.error("Failed to run DB debug query: %s", e)

    try:
        index = await city_index.get_index()
        logger.info("City index ready: %d cities", len(index))
    except Exception:
        logger.exception("Could not warm the city index at startup, will retry on first use")


async def main() -> None:
    redis = get_redis()
    await warm_city_index()
    async with httpx.AsyncClient(timeout=10) as client:
        logger.info("Worker started, waiting on %s", settings.booking_jobs_key)
        background_tasks = set()
        while True:
            item = await redis.brpop(settings.booking_jobs_key, timeout=5)
            if item is None:
                continue
            _, raw_job = item
            try:
                job = json.loads(raw_job)
                # Create a concurrent task to handle the job without blocking the loop
                task = asyncio.create_task(handle_job(client, job))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
            except Exception:
                logger.exception("Failed to process job: %s", raw_job)


if __name__ == "__main__":
    asyncio.run(main())
