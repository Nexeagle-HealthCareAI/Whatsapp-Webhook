"""
conversation_logger.py
-----------------------
Drains app/messengers/conversation_log_queue.py's whatsapp:conversation_log into
dbo.conversation_sessions -- its own process, same convention as worker.py (inbound) and
sender.py (outbound), but with one deliberate difference: jobs are handled strictly one at
a time, NOT fanned out concurrently via asyncio.create_task like those two. Every event for
a given session_id appends into that same session's transcript_json array (see
app/db/conversation_log.py) -- processing sequentially is what guarantees those appends
land in the order they actually happened, without needing to reason about concurrent writes
racing on the same row. Volume here is one small write per conversation event, not one HTTP
call per outbound message, so there's no throughput reason to prefer concurrency anyway.

Kept entirely off app/conversation/__init__.py's request path on purpose: a patient's reply
is never slowed down by, or made to fail because of, a SQL Server write meant purely for
later analysis. See app/messengers/conversation_log_queue.py's module docstring for the
full reasoning.
"""

import json
import logging

from app import db
from app.messengers.conversation_log_queue import LOG_KEY
from app.messengers.redis_client import get_redis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("conversation_logger")

_PROCESSING_KEY = "whatsapp:conversation_log:processing"


async def _handle_job(redis, raw_job: str) -> None:
    try:
        job = json.loads(raw_job)
        if job.get("kind") == "conversion":
            await db.mark_session_converted(job["session_id"], job["appointment_id"])
        else:
            await db.append_conversation_event(
                job["session_id"], job["phone_number"], job["direction"],
                job["message_type"], job.get("content"), job.get("step"), job.get("at"),
            )
    except Exception:
        # Best-effort by design (see module docstring) -- log and move on rather than
        # blocking the queue or crashing the process over one malformed/failed job.
        logger.exception("Failed to persist conversation log job: %s", raw_job)
    finally:
        await redis.lrem(_PROCESSING_KEY, 1, raw_job)


async def main() -> None:
    redis = get_redis()
    logger.info("Conversation logger started, draining %s", LOG_KEY)
    while True:
        # BLMOVE, not BRPOP: a crash mid-write leaves the job sitting in _PROCESSING_KEY
        # instead of losing it -- same durability pattern as sender.py's outbound queue.
        raw_job = await redis.blmove(LOG_KEY, _PROCESSING_KEY, timeout=5, src="RIGHT", dest="LEFT")
        if raw_job is None:
            continue
        await _handle_job(redis, raw_job)


if __name__ == "__main__":
    asyncio.run(main())
