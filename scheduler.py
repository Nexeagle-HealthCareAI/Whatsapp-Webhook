"""
scheduler.py
Requirement 9 — follow-up notifications. Runs as its own container (see docker-compose.yml,
"scheduler" service), separate from worker.py because this is time-driven (checks once a
day) rather than queue-driven (worker.py drains Redis as fast as messages arrive) — mixing
the two loops in one process would make the polling interval and the message-handling
latency fight each other for no reason.

*** Known hard blocker, flagged rather than hidden: ***
Follow-ups fire the day after a visit, which means the patient's 24-hour free-form messaging
window (last_inbound_at) has almost always closed by then. WhatsApp will reject a free-form
send outside that window — a real send requires an *approved Utility template*
(followup_reminder is one of the six template names already identified in this project's
earlier docs). As of the last status snapshot in this project's history, none of the six
Utility templates had been submitted yet. This script is correct and ready to run, but until
those templates exist and are approved, send_text() below will fail for any patient outside
the 24h window (which, for a next-day follow-up, is effectively everyone) — check server
logs for delivery failures, that's expected until template approval lands, not a bug here.
Swap send_text() for a template-send call (a new function in app/whatsapp_client.py, not
yet written since there's no approved template name/params to send) once that's unblocked.
"""

import asyncio
import logging
from datetime import date, timedelta

import httpx

from app import db, i18n
from app.messengers.whatsapp_client import send_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

_POLL_INTERVAL_SECONDS = 6 * 60 * 60  # four checks a day is plenty for a once-daily job


# Localized fallback for the small number of rows booked before pending_appointments.doctor_name
# existed (see sql/schema.sql) -- those have NULL here and fall back to this generic phrase,
# same as _do_book_appointment already does for a display name via "there"/patient fallbacks.
_GENERIC_DOCTOR_FALLBACK = {
    "en": "your doctor", "hi": "आपके डॉक्टर", "hg": "aapke doctor", "bn": "আপনার ডাক্তার",
}


async def send_followups_for(client: httpx.AsyncClient, visit_date: date) -> None:
    due = await db.list_due_followups(visit_date)
    logger.info("Found %d follow-up(s) due for visit date %s", len(due), visit_date)
    for row in due:
        lang = row["preferred_language"]
        doctor_name = row.get("doctor_name") or _GENERIC_DOCTOR_FALLBACK.get(lang, _GENERIC_DOCTOR_FALLBACK["en"])
        text = i18n.t(
            "followup_reminder",
            lang,
            patient_name=row["patient_display_name"] or "there",
            doctor_name=doctor_name,
        )
        try:
            await send_text(client, row["phone_number"], text)
        except httpx.HTTPError:
            logger.exception("Failed to send follow-up for appointment %s", row["hms_appointment_id"])
            continue  # leave followup_sent_at unset so the next run retries this one

        # Peer-review live-reported bug: without this, a reply to the message above landed
        # on a completely blank conversation state (a successful booking DELETEs its own
        # state row) and got NLU-classified/routed as if from a brand-new patient -- a casual
        # "I'm feeling fine" was misread as a symptom description and answered with a
        # nonsensical "couldn't confidently match that to a specialty" list. See
        # app/conversation/followup.py, intercepted early in handle_message (NOT via
        # STEP_REGISTRY, which would be too late) specifically for this step. Best-effort:
        # if this write fails, the follow-up text itself already sent successfully above, so
        # still mark it sent rather than resending the same message on the next run -- the
        # patient's very next reply would just fall back to the pre-fix (blank-state) path.
        try:
            await db.save_conversation_state(
                row["phone_number"], "awaiting_followup_reply",
                {"lang": row["preferred_language"], "hms_appointment_id": row["hms_appointment_id"]},
            )
        except Exception:
            logger.exception(
                "Failed to save awaiting_followup_reply state for appointment %s", row["hms_appointment_id"]
            )

        await db.mark_followup_sent(row["id"])


async def run_once() -> None:
    # Follow-ups are for visits that already happened — yesterday, from "today"'s vantage
    # point of the scheduler run.
    visit_date = date.today() - timedelta(days=1)
    async with httpx.AsyncClient(timeout=10) as client:
        await send_followups_for(client, visit_date)


async def main() -> None:
    logger.info("Scheduler started, checking every %d seconds", _POLL_INTERVAL_SECONDS)
    while True:
        try:
            await run_once()
        except Exception:
            logger.exception("Scheduler run failed")
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
