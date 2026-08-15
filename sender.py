"""
sender.py
---------
Drains app/messengers/outbound_queue.py's whatsapp:outbox at a steady, Meta-safe pace --
its own process, same convention as worker.py (inbound) and scheduler.py (follow-ups).
See app/messengers/outbound_queue.py for the durability/retry/backoff design this relies
on; this file is just the loop that uses it.
"""

import asyncio
import json
import logging

import httpx

from app.config import settings
from app.messengers.outbound_queue import (
    DEAD_KEY,
    OUTBOX_KEY,
    PROCESSING_KEY,
    acquire_send_slot,
    promote_ready_delayed_jobs,
    requeue_with_backoff,
)
from app.messengers.redis_client import get_redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sender")

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{settings.whatsapp_phone_number_id}/messages"


async def _attempt_send(client: httpx.AsyncClient, redis, raw_job: str) -> None:
    job = json.loads(raw_job)
    await acquire_send_slot(redis, settings.whatsapp_send_rate_limit)

    try:
        response = await client.post(
            GRAPH_API_URL,
            headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
            json=job["payload"],
        )
    except httpx.TransportError as exc:
        logger.warning("Transport error sending to %s, will retry: %s", job["payload"].get("to"), exc)
        await requeue_with_backoff(redis, job, settings.whatsapp_send_max_attempts)
        await redis.lrem(PROCESSING_KEY, 1, raw_job)
        return

    to = job["payload"].get("to") or f"msg:{job['payload'].get('message_id')}"

    if response.status_code == 429 or response.status_code >= 500:
        # Meta said slow down, or had its own hiccup -- not this message's fault.
        logger.warning("WhatsApp send to %s got %s, requeuing with backoff", to, response.status_code)
        await requeue_with_backoff(redis, job, settings.whatsapp_send_max_attempts)
    elif response.status_code >= 400:
        # Genuinely bad request (bad number, malformed payload) -- retrying won't help.
        logger.error("Permanently failed WhatsApp send to %s: %s", to, response.text)
        await redis.lpush(DEAD_KEY, raw_job)
    else:
        logger.info("Sent to %s -> %s", to, response.status_code)

    await redis.lrem(PROCESSING_KEY, 1, raw_job)


async def main() -> None:
    redis = get_redis()
    async with httpx.AsyncClient(timeout=10) as client:
        logger.info(
            "Sender started, draining %s at up to %s/sec",
            OUTBOX_KEY,
            settings.whatsapp_send_rate_limit,
        )
        background_tasks = set()
        while True:
            await promote_ready_delayed_jobs(redis)

            # BLMOVE, not BRPOP: atomically hands the job to PROCESSING_KEY instead of just
            # deleting it from OUTBOX_KEY. If this process crashes before _attempt_send
            # finishes, the job is still sitting in PROCESSING_KEY, not gone -- see
            # app/messengers/outbound_queue.py's module docstring for the full reasoning.
            raw_job = await redis.blmove(OUTBOX_KEY, PROCESSING_KEY, timeout=5, src="RIGHT", dest="LEFT")
            if raw_job is None:
                continue

            task = asyncio.create_task(_attempt_send(client, redis, raw_job))
            background_tasks.add(task)
            task.add_done_callback(background_tasks.discard)


if __name__ == "__main__":
    asyncio.run(main())
