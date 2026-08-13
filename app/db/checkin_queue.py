from datetime import date as date_type
from typing import Any

async def upsert_checkin_notification(
    hms_appointment_id: str,
    phone_number: str,
    preferred_language: str | None,
    patient_display_name: str | None = None,
) -> None:
    """Registers an appointment that wasn't booked through this bot (a walk-in who scanned the
    OPD QR and checked in) into the same table get_appointment_by_hms_id (above) reads from --
    without this, a walk-in would successfully check in but never receive a queue-update push,
    since that lookup only ever found appointments this bot itself booked. Same MERGE-upsert
    idiom as save_conversation_state/save_queue_status."""
    from app.db import get_pool
    pool = await get_pool()
    today = date_type.today()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.pending_appointments AS target
            USING (SELECT ? AS hms_appointment_id) AS src
            ON target.hms_appointment_id = src.hms_appointment_id
            WHEN MATCHED THEN
                UPDATE SET phone_number = ?, preferred_language = ?,
                           patient_display_name = COALESCE(target.patient_display_name, ?)
            WHEN NOT MATCHED THEN
                INSERT (phone_number, preferred_date, hms_appointment_id, status, preferred_language, patient_display_name)
                VALUES (?, ?, ?, 'checked_in', ?, ?);
            """,
            (
                hms_appointment_id,
                phone_number, preferred_language, patient_display_name,
                phone_number, today, hms_appointment_id, preferred_language, patient_display_name,
            ),
        )


async def save_queue_status(
    hms_appointment_id: str, current_token: int, estimated_wait_minutes: int | None
) -> None:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.appointment_queue_status AS target
            USING (SELECT ? AS hms_appointment_id) AS src
            ON target.hms_appointment_id = src.hms_appointment_id
            WHEN MATCHED THEN
                UPDATE SET current_token = ?, estimated_wait_minutes = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (hms_appointment_id, current_token, estimated_wait_minutes)
                VALUES (?, ?, ?);
            """,
            (
                hms_appointment_id,
                current_token,
                estimated_wait_minutes,
                hms_appointment_id,
                current_token,
                estimated_wait_minutes,
            ),
        )


async def list_due_followups(visit_date: date_type) -> list[dict[str, Any]]:
    """Booked appointments whose visit date has passed and haven't had a follow-up sent yet
    — read by scheduler.py. Deliberately scoped to a single exact date rather than "<=
    yesterday" so a late-starting scheduler run doesn't blast out a backlog of old
    follow-ups all at once; see scheduler.py for how this is called."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, phone_number, hms_appointment_id, preferred_language, patient_display_name "
            "FROM dbo.pending_appointments "
            "WHERE preferred_date = ? AND status = 'booked' AND followup_sent_at IS NULL",
            (visit_date,),
        )
        columns = ["id", "phone_number", "hms_appointment_id", "preferred_language", "patient_display_name"]
        rows = await cur.fetchall()
        return [dict(zip(columns, row)) for row in rows]


async def mark_followup_sent(row_id) -> None:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET followup_sent_at = SYSUTCDATETIME() WHERE id = ?",
            (str(row_id),),
        )
