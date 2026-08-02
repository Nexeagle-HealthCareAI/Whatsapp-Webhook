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
            
            # Query indexes for PatientRegistrations
            await cur.execute(f"""
                SELECT 
                    i.name AS IndexName,
                    col.name AS ColumnName,
                    i.is_unique AS IsUnique,
                    i.is_primary_key AS IsPrimaryKey
                FROM [{hms_db}].sys.indexes i
                INNER JOIN [{hms_db}].sys.index_columns ic ON  i.object_id = ic.object_id AND i.index_id = ic.index_id
                INNER JOIN [{hms_db}].sys.columns col ON ic.object_id = col.object_id AND ic.column_id = col.column_id
                INNER JOIN [{hms_db}].sys.tables t ON i.object_id = t.object_id
                WHERE t.name = 'PatientRegistrations'
            """)
            rows = await cur.fetchall()
            print("Indexes on PatientRegistrations:")
            for r in rows:
                print(f"Index: {r[0]}, Column: {r[1]}, Unique: {r[2]}, PK: {r[3]}")
                
            # Query existing patients with mobile '918319694497'
            await cur.execute(f"SELECT PatientID, FullName, Mobile, HospitalID FROM [{hms_db}].dbo.PatientRegistrations WHERE Mobile = '918319694497'")
            patients = await cur.fetchall()
            print(f"Existing patients with mobile 918319694497: {patients}")
            
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to inspect: {e}")

if __name__ == "__main__":
    asyncio.run(check())
