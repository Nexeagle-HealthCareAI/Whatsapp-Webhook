"""
sender.py
---------
Drains app/messengers/outbound_queue.py's whatsapp:outbox at a steady, Meta-safe pace --
its own process, same convention as worker.py (inbound) and scheduler.py (follow-ups).
See app/messengers/outbound_queue.py for the durability/retry/backoff design this relies
on; this file is just the loop that uses it.

Jobs are dequeued in order but fired as independent concurrent tasks for throughput -- one
slow/rate-limited HTTP round-trip to Meta must not stall every other message behind it. That
concurrency has a real cost, though: nothing then guarantees the FIRST job's HTTP call
actually *completes* before the second one's does, so two messages queued back-to-back for
the same patient (e.g. a language-confirmation text immediately followed by the
location-request prompt) could arrive on their phone out of order -- live-reported. main()'s
per-recipient task chaining below fixes that: a job for a phone number that already has one
in flight waits for it to finish first, so messages to the SAME recipient are strictly
ordered, while different recipients' sends stay fully concurrent -- no throughput lost across
the conversations that matter for the 70/sec rate limit, only serialized where it's actually
needed.
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


def dispatch_job(
    client: httpx.AsyncClient,
    redis,
    raw_job: str,
    last_task_per_phone: dict[str, asyncio.Task],
    background_tasks: set[asyncio.Task],
) -> asyncio.Task:
    """Schedules one dequeued job, chaining it behind any still-in-flight send to the SAME
    recipient (see module docstring) while leaving different recipients fully concurrent.
    Mutates last_task_per_phone/background_tasks in place; returns the scheduled task."""
    to = json.loads(raw_job).get("payload", {}).get("to")
    prev_task = last_task_per_phone.get(to) if to else None

    async def _run_in_order(prev: asyncio.Task | None = prev_task, job: str = raw_job) -> None:
        if prev is not None:
            try:
                await prev
            except Exception:
                pass  # only waiting for it to finish, not chaining its failure
        await _attempt_send(client, redis, job)

    task = asyncio.create_task(_run_in_order())
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    if to:
        last_task_per_phone[to] = task
        task.add_done_callback(
            lambda t, phone=to: last_task_per_phone.pop(phone, None) if last_task_per_phone.get(phone) is t else None
        )
    return task


async def main() -> None:
    redis = get_redis()
    async with httpx.AsyncClient(timeout=10) as client:
        logger.info(
            "Sender started, draining %s at up to %s/sec",
            OUTBOX_KEY,
            settings.whatsapp_send_rate_limit,
        )
        background_tasks: set[asyncio.Task] = set()
        # Latest in-flight task per recipient -- see module docstring for why this exists.
        # Pruned via each task's own done-callback, so this only ever holds entries for
        # phones with a send genuinely still in flight, not every phone ever seen.
        last_task_per_phone: dict[str, asyncio.Task] = {}
        while True:
            await promote_ready_delayed_jobs(redis)

            # BLMOVE, not BRPOP: atomically hands the job to PROCESSING_KEY instead of just
            # deleting it from OUTBOX_KEY. If this process crashes before _attempt_send
            # finishes, the job is still sitting in PROCESSING_KEY, not gone -- see
            # app/messengers/outbound_queue.py's module docstring for the full reasoning.
            raw_job = await redis.blmove(OUTBOX_KEY, PROCESSING_KEY, timeout=5, src="RIGHT", dest="LEFT")
            if raw_job is None:
                continue

            dispatch_job(client, redis, raw_job, last_task_per_phone, background_tasks)


if __name__ == "__main__":
    asyncio.run(main())
