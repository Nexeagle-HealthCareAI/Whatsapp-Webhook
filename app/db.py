import json
from datetime import date as date_type
from typing import Any
from uuid import UUID, uuid4

import aioodbc

from app.config import settings

_pool: aioodbc.Pool | None = None


async def get_pool() -> aioodbc.Pool:
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(dsn=settings.sqlserver_conn_string, autocommit=True)
    return _pool


async def get_conversation_state(phone: str) -> dict[str, Any] | None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT current_step, context_json FROM dbo.conversation_state WHERE phone_number = ?",
            (phone,),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        current_step, context_json = row
        return {
            "current_step": current_step,
            "context": json.loads(context_json) if context_json else {},
        }


async def save_conversation_state(phone: str, step: str, context: dict[str, Any]) -> None:
    pool = await get_pool()
    context_json = json.dumps(context)
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.conversation_state AS target
            USING (SELECT ? AS phone_number) AS src
            ON target.phone_number = src.phone_number
            WHEN MATCHED THEN
                UPDATE SET current_step = ?, context_json = ?, updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (phone_number, current_step, context_json)
                VALUES (?, ?, ?);
            """,
            (phone, step, context_json, phone, step, context_json),
        )


async def clear_conversation_state(phone: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.conversation_state WHERE phone_number = ?", (phone,))


async def is_message_processed(message_id: str) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM dbo.processed_messages WHERE message_id = ?", (message_id,)
        )
        return await cur.fetchone() is not None


async def mark_message_processed(message_id: str) -> None:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        try:
            await cur.execute(
                "INSERT INTO dbo.processed_messages (message_id) VALUES (?)", (message_id,)
            )
        except Exception:
            # Already present (e.g. a retried job) — this table only needs to be a no-op
            # the second time, not raise.
            pass


async def has_pending_appointment(phone: str, preferred_date: date_type) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP 1 1 FROM dbo.pending_appointments "
            "WHERE phone_number = ? AND preferred_date = ? AND status = 'pending'",
            (phone, preferred_date),
        )
        return await cur.fetchone() is not None


async def create_pending_appointment(phone: str, preferred_date: date_type) -> UUID:
    pool = await get_pool()
    row_id = uuid4()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO dbo.pending_appointments (id, phone_number, preferred_date, status) "
            "VALUES (?, ?, ?, 'pending')",
            (str(row_id), phone, preferred_date),
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
