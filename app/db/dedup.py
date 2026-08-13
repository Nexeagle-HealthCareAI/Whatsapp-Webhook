async def is_message_processed(message_id: str) -> bool:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT 1 FROM dbo.processed_messages WHERE message_id = ?", (message_id,)
        )
        return await cur.fetchone() is not None


async def mark_message_processed(message_id: str) -> None:
    from app.db import get_pool
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
