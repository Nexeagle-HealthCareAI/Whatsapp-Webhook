import asyncio
import os
import re
import aioodbc
from app import db

async def check() -> None:
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1")
        await cur.fetchone()
    pool.close()
    await pool.wait_closed()

    conn_str = os.environ.get("SQLSERVER_CONN_STRING", "")
    master_conn_str = re.sub(r"Database=[^;]+", "Database=master", conn_str, flags=re.IGNORECASE)
    
    try:
        master_pool = await aioodbc.create_pool(dsn=master_conn_str, autocommit=True)
        async with master_pool.acquire() as conn, conn.cursor() as cur:
            hms_db = "EasyHMSDatabase"
            await cur.execute(f"USE [{hms_db}]")
            
            # Select all rows from StatusMaster
            await cur.execute("SELECT StatusCode, StatusName FROM dbo.StatusMaster")
            rows = await cur.fetchall()
            print("StatusMaster rows:")
            for r in rows:
                print(f"Code: {r[0]}, Name: {r[1]}")
                
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed: {e}")

if __name__ == "__main__":
    asyncio.run(check())
