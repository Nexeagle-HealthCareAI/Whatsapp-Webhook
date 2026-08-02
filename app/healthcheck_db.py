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
            
            # Query DoctorDepartments columns
            await cur.execute(f"SELECT COLUMN_NAME FROM [{hms_db}].INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = 'DoctorDepartments'")
            cols = [r[0] for r in await cur.fetchall()]
            print(f"DoctorDepartments columns: {cols}")
            
            # Query rows for Doctor 1ac0a7d9-83ee-4ea7-a545-969499498657
            await cur.execute(f"SELECT * FROM [{hms_db}].dbo.DoctorDepartments WHERE DoctorID = '1ac0a7d9-83ee-4ea7-a545-969499498657'")
            rows_1ac = await cur.fetchall()
            print(f"DoctorDepartments rows for 1ac0a7d9-83ee-4ea7-a545-969499498657: {rows_1ac}")
            
            # Query rows for Doctor a496cbcb-b45f-4df9-9649-6d2117ae1005
            await cur.execute(f"SELECT * FROM [{hms_db}].dbo.DoctorDepartments WHERE DoctorID = 'a496cbcb-b45f-4df9-9649-6d2117ae1005'")
            rows_a49 = await cur.fetchall()
            print(f"DoctorDepartments rows for a496cbcb-b45f-4df9-9649-6d2117ae1005: {rows_a49}")
            
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to inspect columns: {e}")

if __name__ == "__main__":
    asyncio.run(check())
