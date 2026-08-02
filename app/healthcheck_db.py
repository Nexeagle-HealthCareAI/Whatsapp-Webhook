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
            print(f"inspecting {hms_db} Appointments columns...")
            await cur.execute(f"SELECT COLUMN_NAME FROM [{hms_db}].INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'Appointments'")
            cols = [row[0] for row in await cur.fetchall()]
            print(f"Appointments table columns: {cols}")
            
            # Print if public booking columns exist
            target_cols = ["BookingSource", "BookingIpAddress", "BookingReferrerUrl", "BookingUtmCampaign", "BookedByMobile"]
            missing_cols = [c for c in target_cols if c not in cols]
            print(f"Missing columns: {missing_cols}")
            
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to inspect columns: {e}")

if __name__ == "__main__":
    asyncio.run(check())
