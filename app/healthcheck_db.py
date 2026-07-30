import asyncio

from app import db


async def check() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1")
        await cur.fetchone()


if __name__ == "__main__":
    asyncio.run(check())
