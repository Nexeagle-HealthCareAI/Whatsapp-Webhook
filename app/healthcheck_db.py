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
            print(f"inspecting {hms_db} Appointments columns details...")
            
            # Query column details
            await cur.execute(f"""
                SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE
                FROM [{hms_db}].INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = 'Appointments'
            """)
            rows = await cur.fetchall()
            for r in rows:
                print(f"Column: {r[0]}, Type: {r[1]}, MaxLength: {r[2]}, Nullable: {r[3]}")
                
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to inspect columns: {e}")

if __name__ == "__main__":
    asyncio.run(check())
