import logging
from functools import lru_cache

from redis.asyncio import Redis

from app.config import settings

logger = logging.getLogger("redis_client")


@lru_cache
def get_redis() -> Redis:
    return Redis.from_url(settings.redis_url, decode_responses=True)


async def sweep_stuck_jobs(redis: Redis, processing_key: str, main_key: str) -> int:
    """Peer-review P0: worker.py/sender.py/conversation_logger.py all hand a job from their
    main queue to a `processing_key` list via BLMOVE before acting on it, specifically so a
    hard crash (OOM, a deploy restarting the container mid-job) leaves the job sitting there
    instead of losing it -- but nothing ever read `processing_key` back out again, so a job
    stranded there by a crash just sat forever, never retried, invisible unless someone
    manually inspected Redis. This is the missing other half: call once at process startup,
    before the main consume loop begins, to requeue anything a previous crash left behind.

    Uses RPOPLPUSH (oldest stuck job first) rather than a wholesale rename/move, so jobs land
    on `main_key` the same way a real producer's LPUSH would, and the relative order among
    the recovered jobs themselves is preserved (oldest recovered job is still the next one a
    consumer's BLMOVE picks up). Recovered jobs land behind whatever is already queued from
    during the outage, not ahead of it -- a deliberate, simple default, not a priority queue.
    """
    count = 0
    while True:
        moved = await redis.rpoplpush(processing_key, main_key)
        if moved is None:
            break
        count += 1
    if count:
        logger.warning(
            "Recovered %d job(s) a previous crash left stranded in %s", count, processing_key
        )
    return count
