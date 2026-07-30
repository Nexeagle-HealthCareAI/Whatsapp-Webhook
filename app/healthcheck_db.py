import asyncio

from app import db


async def check() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1")
        await cur.fetchone()
    # This is a short-lived, one-off process (unlike the worker, which keeps the pool
    # open for its whole lifetime) — close it explicitly or aioodbc logs "Unclosed
    # connection" warnings for every connection in the pool on exit.
    pool.close()
    await pool.wait_closed()


if __name__ == "__main__":
    asyncio.run(check())
