"""
app/messengers/outbound_queue.py
---------------------------------
The durable, rate-limited path every outbound WhatsApp send goes through.
app.messengers.whatsapp_client's send_text/send_list/send_buttons/send_location_request/
send_location push a job here instead of calling Meta directly; sender.py (repo root,
its own process -- same convention as worker.py/scheduler.py) drains this queue at a
steady, Meta-safe pace.

send_typing_indicator, send_document, and send_flow are deliberately NOT routed through
here -- send_typing_indicator needs to fire immediately to mean anything (a "typing..."
indicator that shows up after the reply already arrived is pointless), and
send_document/send_flow both have callers that branch on an immediate success/failure
result (see app/conversation/checkin.py's document-QR handler and
app/conversation/__init__.py's patient-details-flow offer) -- a queued send can't answer
"did it work" synchronously. Those three keep calling Meta directly, same as before.

What this buys you, and why each piece exists:
- A burst of replies (e.g. right after a marketing push) is absorbed here instead of
  exceeding Meta's per-second sending limit and having some replies silently dropped.
- A crash mid-send can't lose a job: sender.py uses BLMOVE, not BRPOP, to atomically hand
  a job from the main queue to a "processing" list -- it's only removed from there once
  the send actually finishes. A crash leaves it safely sitting in `processing`, not gone.
- A transient failure (429 rate-limited, or a 5xx from Meta's side) is retried with
  exponential backoff via a delayed queue, not given up on immediately.
- A permanent failure (bad payload, etc. -- retrying won't help) goes to a visible
  dead-letter list after whatsapp_send_max_attempts, instead of vanishing into a log line.
"""

import asyncio
import json
import time

OUTBOX_KEY = "whatsapp:outbox"
PROCESSING_KEY = "whatsapp:outbox:processing"
DELAYED_KEY = "whatsapp:outbox:delayed"
DEAD_KEY = "whatsapp:outbox:dead"
_RATE_KEY_PREFIX = "whatsapp:rate"


async def enqueue(redis, payload: dict) -> None:
    """Called by whatsapp_client.py's send_* functions. Returns as soon as the job is
    safely in Redis -- the actual HTTP call to Meta happens later, in sender.py."""
    job = {"payload": payload, "attempt": 0, "enqueued_at": time.time()}
    await redis.lpush(OUTBOX_KEY, json.dumps(job))


async def acquire_send_slot(redis, limit_per_sec: int) -> None:
    """Blocks (briefly, in small steps) until it's actually safe to send one more message
    this second. A plain per-second counter in Redis -- correct even if sender.py is ever
    scaled to more than one process, since the counter is shared state, not in-memory."""
    while True:
        now_bucket = int(time.time())
        key = f"{_RATE_KEY_PREFIX}:{now_bucket}"
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, 2)  # self-cleans; only ever need the current+prior second
        if count <= limit_per_sec:
            return
        await asyncio.sleep(0.05)


async def requeue_with_backoff(redis, job: dict, max_attempts: int) -> None:
    """A transient failure -- try again shortly, waiting longer each time. After
    max_attempts, give up and move it somewhere a human can see it instead of retrying
    forever."""
    job["attempt"] = job.get("attempt", 0) + 1
    if job["attempt"] >= max_attempts:
        await redis.lpush(DEAD_KEY, json.dumps(job))
        return
    delay = min(2 ** job["attempt"], 60)  # 2s, 4s, 8s, 16s, ... capped at 60s
    ready_at = time.time() + delay
    await redis.zadd(DELAYED_KEY, {json.dumps(job): ready_at})


async def promote_ready_delayed_jobs(redis) -> None:
    """Moves any delayed job whose backoff has elapsed back onto the main outbound queue.
    Called once per sender.py loop iteration -- cheap when nothing's due (empty range)."""
    now = time.time()
    ready = await redis.zrangebyscore(DELAYED_KEY, min=0, max=now)
    for raw in ready:
        removed = await redis.zrem(DELAYED_KEY, raw)
        if removed:  # guards against two sender.py instances racing on the same job
            await redis.lpush(OUTBOX_KEY, raw)
