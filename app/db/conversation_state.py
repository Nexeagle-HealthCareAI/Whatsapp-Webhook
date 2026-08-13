import json
from typing import Any

async def get_conversation_state(phone: str) -> dict[str, Any] | None:
    from app.db import get_pool
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
    from app.db import get_pool
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
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM dbo.conversation_state WHERE phone_number = ?", (phone,))
