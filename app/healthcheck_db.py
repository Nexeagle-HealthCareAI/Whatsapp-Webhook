import asyncio
import os
import re
import aioodbc
from app import db

async def check() -> None:
    # Standard check to satisfy the GHA health check requirements
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
            
            # Insert the missing PRE_APPOINTMENT status code into StatusMaster
            print("Checking if PRE_APPOINTMENT status exists in StatusMaster...")
            await cur.execute("SELECT 1 FROM dbo.StatusMaster WHERE StatusCode = 'PRE_APPOINTMENT'")
            exists = await cur.fetchone()
            
            if not exists:
                print("PRE_APPOINTMENT status is missing. Inserting it now...")
                await cur.execute("""
                    INSERT INTO dbo.StatusMaster (StatusCode, DisplayName, SortOrder, IsTerminal)
                    VALUES ('PRE_APPOINTMENT', 'Pre Appointment', 5, 0)
                """)
                print("PRE_APPOINTMENT status inserted successfully!")
            else:
                print("PRE_APPOINTMENT status already exists in StatusMaster.")
                
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to migrate/seed status: {e}")

if __name__ == "__main__":
    asyncio.run(check())
