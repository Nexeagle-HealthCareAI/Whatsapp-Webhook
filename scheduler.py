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
from app.whatsapp_client import send_text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scheduler")

_POLL_INTERVAL_SECONDS = 6 * 60 * 60  # four checks a day is plenty for a once-daily job


async def send_followups_for(client: httpx.AsyncClient, visit_date: date) -> None:
    due = await db.list_due_followups(visit_date)
    logger.info("Found %d follow-up(s) due for visit date %s", len(due), visit_date)
    for row in due:
        text = i18n.t(
            "followup_reminder",
            row["preferred_language"],
            patient_name=row["patient_display_name"] or "there",
            doctor_name="your doctor",  # doctor name isn't retained past booking today —
            # see note in app/conversation.py about context being dropped after booking;
            # if this should say the actual doctor's name, store it on pending_appointments
            # at booking time the same way patient_display_name already is.
        )
        try:
            await send_text(client, row["phone_number"], text)
        except httpx.HTTPError:
            logger.exception("Failed to send follow-up for appointment %s", row["hms_appointment_id"])
            continue  # leave followup_sent_at unset so the next run retries this one
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
