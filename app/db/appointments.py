from datetime import date as date_type
from typing import Any
from uuid import UUID, uuid4

async def has_pending_appointment(phone: str, preferred_date: date_type) -> bool:
    from app.db import get_pool
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
    from app.db import get_pool
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
    from app.db import get_pool
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
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET hms_appointment_id = ?, status = 'booked' WHERE id = ?",
            (hms_appointment_id, str(row_id)),
        )


async def mark_appointment_failed(row_id: UUID) -> None:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET status = 'failed' WHERE id = ?",
            (str(row_id),),
        )


async def get_appointment_by_hms_id(hms_appointment_id: str) -> dict[str, Any] | None:
    """Used by POST /events/token-called (app/webhook.py) to find which WhatsApp
    conversation a queue update belongs to, and which language to send it in."""
    from app.db import get_pool
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
    from app.db import get_pool
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
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET status = 'cancelled' WHERE hms_appointment_id = ?",
            (hms_appointment_id,),
        )


async def mark_appointment_rescheduled_locally(hms_appointment_id: str, new_date: date_type) -> None:
    """Keeps this bot's own preferred_date in sync after a successful reschedule via hms_client —
    status stays 'booked' (it's still a live appointment, just moved)."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.pending_appointments SET preferred_date = ? WHERE hms_appointment_id = ?",
            (new_date, hms_appointment_id),
        )
