import asyncio
import json
import logging

import httpx

from app import conversation, db
from app.config import settings
from app.i18n import t
from app.messengers import city_index, whatsapp_client
from app.messengers.redis_client import get_redis
from app.pii import mask_phone

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("worker")


async def _notify_patient_of_failure(client: httpx.AsyncClient, phone: str) -> None:
    """Best-effort -- a failure notifying the patient about a failure must never itself
    raise and mask the original error. Looks up their saved language so the notice isn't
    always in English, but that lookup is itself best-effort (a crash this deep shouldn't
    also block on a second DB call)."""
    lang = None
    try:
        state = await db.get_conversation_state(phone)
        lang = state["context"].get("lang") if state else None
    except Exception:
        logger.warning("Could not look up language for failure notice to %s", mask_phone(phone))
    try:
        await whatsapp_client.send_text(client, phone, t("error_hms", lang))
    except Exception:
        logger.exception("Also failed to send the failure notice to %s", mask_phone(phone))


async def handle_job(client: httpx.AsyncClient, job: dict) -> None:
    message_id = job.get("message_id")
    sender = job["sender"]

    # Durable backstop beyond Redis's TTL-based dedupe (app/webhook.py) — belt and
    # suspenders against a duplicate booking if a job is ever replayed after that
    # Redis key has expired.
    if message_id and await db.is_message_processed(message_id):
        logger.info("Message %s already processed, skipping", message_id)
        return

    logger.info("Processing message %s from %s", message_id, mask_phone(sender))
    args = (
        client, sender, job.get("sender_name"),
        job.get("input_type") or "text", job.get("input_value") or "", message_id,
    )
    # Previously fire-and-forget with no exception handling at all here -- any error other
    # than the two handle_message already catches internally (HmsApiError/httpx.HTTPError)
    # left the patient with total silence: no reply, no fallback message, nothing in the
    # logs pointing at who or what. One immediate retry covers transient blips (a DB
    # reconnect, a momentary Redis hiccup); if it fails twice in a row it's treated as a
    # real failure -- the patient is told, and the message is marked processed so a later
    # webhook replay of the same message_id doesn't retry it forever.
    try:
        await conversation.handle_message(*args)
    except Exception:
        logger.exception(
            "handle_message failed (attempt 1), retrying once: message_id=%s phone=%s",
            message_id, mask_phone(sender),
        )
        try:
            await conversation.handle_message(*args)
        except Exception:
            logger.exception(
                "handle_message failed on retry, giving up: message_id=%s phone=%s",
                message_id, mask_phone(sender),
            )
            await _notify_patient_of_failure(client, sender)
            if message_id:
                await db.mark_message_processed(message_id)
            return

    if message_id:
        await db.mark_message_processed(message_id)


async def warm_city_index() -> None:
    """Build the city index up front so no patient waits on it mid-conversation.

    Only the first run after a deploy (or after the cache expires) does real work — it pages
    the whole public doctor directory, which takes seconds, not milliseconds. Non-fatal: if
    1HMS is unreachable at boot the worker still starts, and the index is rebuilt lazily on
    first use. See app/city_index.py."""
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
            job = None
            try:
                job = json.loads(raw_job)
                # Create a concurrent task to handle the job without blocking the loop
                task = asyncio.create_task(handle_job(client, job))
                background_tasks.add(task)
                task.add_done_callback(background_tasks.discard)
            except Exception:
                # message_id only -- never the raw payload, which carries the patient's
                # verbatim text (incl. patient-details form submissions) and full phone.
                logger.exception(
                    "Failed to process job: message_id=%s",
                    job.get("message_id") if job else None,
                )


if __name__ == "__main__":
    asyncio.run(main())
