import json
from datetime import date as date_type
from typing import Any
from uuid import UUID, uuid4

import aioodbc

from app.config import settings
from app.db.dedup import is_message_processed, mark_message_processed
from app.db.conversation_state import get_conversation_state, save_conversation_state, clear_conversation_state

_pool: aioodbc.Pool | None = None


async def get_pool() -> aioodbc.Pool:
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(dsn=settings.sqlserver_conn_string, autocommit=True)
    return _pool








async def has_pending_appointment(phone: str, preferred_date: date_type) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP 1 1 FROM dbo.pending_appointments "
            "WHERE phone_number = ? AND preferred_date = ? AND status = 'pending'",
            (phone, preferred_date),
        )
        return await cur.fetchone() is not None


async def get_upcoming_active_appointment(phone: str) -> tuple[bool, str | None]:
    """Check if the user has any active booked/pending appointments on or after today,
    returning a tuple: (has_active, active_date_str)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP 1 preferred_date FROM dbo.pending_appointments "
            "WHERE phone_number = ? AND status IN ('pending', 'booked') AND preferred_date >= CAST(GETDATE() AS DATE) "
            "ORDER BY preferred_date ASC",
            (phone,),
        )
        row = await cur.fetchone()
        if row:
            date_val = row[0]
            date_str = date_val.strftime("%Y-%m-%d") if hasattr(date_val, "strftime") else str(date_val)
            return True, date_str
        return False, None


async def create_pending_appointment(
    phone: str,
    preferred_date: date_type,
    preferred_language: str | None = None,
    booking_for: str = "self",
    patient_display_name: str | None = None,
    patient_age: int | None = None,
    patient_gender: str | None = None,
    patient_guardian: str | None = None,
) -> UUID:
    pool = await get_pool()
    row_id = uuid4()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO dbo.pending_appointments "
            "(id, phone_number, preferred_date, status, preferred_language, booking_for, patient_display_name, patient_age, patient_gender, patient_guardian) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)",
            (
                str(row_id),
                phone,
                preferred_date,
                preferred_language,
                booking_for,
                patient_display_name,
                patient_age,
                patient_gender,
                patient_guardian,
            ),
        )
    return row_id


async def mark_appointment_booked(row_id: UUID, hms_appointment_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET hms_appointment_id = ?, status = 'booked' WHERE id = ?",
            (hms_appointment_id, str(row_id)),
        )


async def mark_appointment_failed(row_id: UUID) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET status = 'failed' WHERE id = ?",
            (str(row_id),),
        )


async def get_appointment_by_hms_id(hms_appointment_id: str) -> dict[str, Any] | None:
    """Used by POST /events/token-called (app/webhook.py) to find which WhatsApp
    conversation a queue update belongs to, and which language to send it in."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT phone_number, preferred_language, patient_display_name "
            "FROM dbo.pending_appointments WHERE hms_appointment_id = ?",
            (hms_appointment_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        phone_number, preferred_language, patient_display_name = row
        return {
            "phone_number": phone_number,
            "preferred_language": preferred_language,
            "patient_display_name": patient_display_name,
        }


async def get_booked_appointments_for_phone(phone: str) -> list[dict[str, Any]]:
    """Candidates for "cancel/reschedule my appointment" — every appointment this bot itself
    booked for this phone number that it still believes is live. Local status alone isn't proof
    of that (staff could have cancelled/completed it from the hospital side since), so
    conversation.py re-verifies each candidate against GET /public/appointments/{id} before
    presenting or acting on it — this is just the local shortlist."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT id, hms_appointment_id, preferred_date "
            "FROM dbo.pending_appointments "
            "WHERE phone_number = ? AND status = 'booked' AND hms_appointment_id IS NOT NULL "
            "ORDER BY preferred_date ASC",
            (phone,),
        )
        rows = await cur.fetchall()
        return [
            {"id": row[0], "hms_appointment_id": row[1], "preferred_date": row[2]}
            for row in rows
        ]


async def mark_appointment_cancelled_locally(hms_appointment_id: str) -> None:
    """Keeps this bot's own record in sync after a successful cancel via hms_client — without
    this, get_booked_appointments_for_phone would keep offering an appointment the patient
    already cancelled through the bot as a cancel/reschedule candidate."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET status = 'cancelled' WHERE hms_appointment_id = ?",
            (hms_appointment_id,),
        )


async def mark_appointment_rescheduled_locally(hms_appointment_id: str, new_date: date_type) -> None:
    """Keeps this bot's own preferred_date in sync after a successful reschedule via hms_client —
    status stays 'booked' (it's still a live appointment, just moved)."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET preferred_date = ? WHERE hms_appointment_id = ?",
            (new_date, hms_appointment_id),
        )


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
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET followup_sent_at = SYSUTCDATETIME() WHERE id = ?",
            (str(row_id),),
        )


async def log_nlu_interaction(
    phone: str,
    session_id: str | None,
    utterance: str,
    nlu_brain: str,
    intent: str | None,
    confidence: float | None,
    doctor_name: str | None,
    specialty: str | None,
    symptom: str | None,
    formatted_date: str | None,
    routed_step: str | None = None,
    is_correct: int | None = None,
    user_feedback: str | None = None
) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dbo.nlu_logs (
                phone_number, session_id, utterance, nlu_brain,
                detected_intent, confidence, doctor_name, specialty,
                symptom, formatted_date, routed_step, is_correct, user_feedback
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                phone, session_id, utterance, nlu_brain,
                intent, confidence, doctor_name, specialty,
                symptom, formatted_date, routed_step, is_correct, user_feedback
            )
        )


async def update_last_nlu_log_correctness(phone: str, is_correct: int, feedback: str) -> None:
    """Updates the is_correct flag and user_feedback string on the most recent NLU log for a phone number."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP 1 id FROM dbo.nlu_logs WHERE phone_number = ? ORDER BY created_at DESC",
            (phone,)
        )
        row = await cur.fetchone()
        if row:
            log_id = row[0]
            await cur.execute(
                "UPDATE dbo.nlu_logs SET is_correct = ?, user_feedback = ? WHERE id = ?",
                (is_correct, feedback, log_id)
            )


async def mark_session_nlu_correctness_on_booking(phone: str, booked_doctor_name: str) -> None:
    """Checks previous NLU logs in the active conversation and marks them as correct/incorrect
    by comparing extracted doctor_name with the booked doctor name."""
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT context_json FROM dbo.conversation_state WHERE phone_number = ?",
            (phone,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return
        context_json = row[0]
        try:
            context = json.loads(context_json)
        except Exception:
            return
        
        session_id = context.get("session_id")
        if not session_id:
            return
            
        await cur.execute(
            "SELECT id, doctor_name FROM dbo.nlu_logs WHERE session_id = ?",
            (session_id,)
        )
        logs = await cur.fetchall()
        for log_id, extracted_doc in logs:
            if extracted_doc:
                ext_clean = extracted_doc.lower().replace("dr", "").replace(".", "").strip()
                booked_clean = booked_doctor_name.lower().replace("dr", "").replace(".", "").strip()
                if ext_clean in booked_clean or booked_clean in ext_clean:
                    await cur.execute(
                        "UPDATE dbo.nlu_logs SET is_correct = 1, user_feedback = 'booked_match' WHERE id = ?",
                        (log_id,)
                    )
                else:
                    await cur.execute(
                        "UPDATE dbo.nlu_logs SET is_correct = 0, user_feedback = 'booked_mismatch' WHERE id = ?",
                        (log_id,)
                    )
