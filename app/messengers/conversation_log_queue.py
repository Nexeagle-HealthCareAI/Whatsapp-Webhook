"""
app/messengers/conversation_log_queue.py
------------------------------------------
Feeds dbo.conversation_sessions (see sql/schema.sql) without ever touching SQL Server on
the request path. app.conversation.__init__'s handle_message (every inbound message) and
_transition_to (every step change) push a small job here; conversation_logger.py (repo
root, its own process -- same convention as worker.py/sender.py) is the only thing that
actually writes to SQL Server, on its own time.

Why not log outbound messages one-for-one too: what the bot sends for a given step is
deterministic from (step, lang) via app/i18n.py's templates, so logging which step the
patient reached (via _transition_to, the one function every step change already goes
through) reconstructs the bot's side of the conversation just as well as recording each
send verbatim would -- without needing session_id/step threaded into every one of
whatsapp_client.py's eight send_* functions and every one of their call sites across
app/conversation/*.py. Inbound content IS logged verbatim (including patient-details form
submissions, per the product decision behind this feature), since that's the side that
isn't reconstructable from anything else.

Both log_event and log_conversion swallow their own errors -- a Redis hiccup here must
never be the reason a patient's message fails to process. Callers await these inline
(LPUSH is a sub-millisecond, in-memory op, not a network call to an external service), so
there's no task-lifecycle management to get wrong, and no meaningful latency added to what
the patient is waiting on.
"""

import json
import logging
import time

logger = logging.getLogger("conversation_log_queue")

LOG_KEY = "whatsapp:conversation_log"


async def log_event(
    redis,
    session_id: str | None,
    phone_number: str,
    direction: str,
    message_type: str,
    content: str | None,
    step: str | None,
) -> None:
    """direction: "in" (patient) or "out" (a step the bot moved the patient to).
    Silently does nothing pre-language-select, before a session_id exists to attribute to."""
    if not session_id:
        return
    job = {
        "kind": "event",
        "session_id": session_id,
        "phone_number": phone_number,
        "direction": direction,
        "message_type": message_type,
        "content": content,
        "step": step,
        "at": time.time(),
    }
    try:
        await redis.lpush(LOG_KEY, json.dumps(job))
    except Exception:
        logger.warning("Failed to enqueue conversation log event for session %s", session_id, exc_info=True)


async def log_conversion(redis, session_id: str | None, appointment_id: str) -> None:
    """Marks a session as having produced a real booking -- see sql/schema.sql's
    appointment_id column for how this drives converted-vs-abandoned analysis."""
    if not session_id:
        return
    job = {"kind": "conversion", "session_id": session_id, "appointment_id": appointment_id, "at": time.time()}
    try:
        await redis.lpush(LOG_KEY, json.dumps(job))
    except Exception:
        logger.warning("Failed to enqueue conversion marker for session %s", session_id, exc_info=True)
