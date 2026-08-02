import asyncio
import os
import re
import aioodbc
from app import db

async def check() -> None:
    # First do standard check
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1")
        await cur.fetchone()
    pool.close()
    await pool.wait_closed()
    print("Standard DB check passed.")

    # Now let's inspect the databases
    conn_str = os.environ.get("SQLSERVER_CONN_STRING", "")
    print(f"Connection string length: {len(conn_str)}")

    # Connect to inspect databases and tables
    master_conn_str = re.sub(r"Database=[^;]+", "Database=master", conn_str, flags=re.IGNORECASE)
    
    try:
        master_pool = await aioodbc.create_pool(dsn=master_conn_str, autocommit=True)
        async with master_pool.acquire() as conn, conn.cursor() as cur:
            # 1. List databases
            await cur.execute("SELECT name FROM sys.databases")
            databases = [row[0] for row in await cur.fetchall()]
            print(f"DATABASES: {databases}")
            
            # Find the EasyHMS database (not master, tempdb, model, msdb, WhatsAppBookingDev)
            hms_db = None
            for db_name in databases:
                if db_name not in ["master", "tempdb", "model", "msdb", "WhatsAppBookingDev"]:
                    hms_db = db_name
                    break
            
            print(f"Detected EasyHMS DB: {hms_db}")
            
            if hms_db:
                # 2. Check doctors in HMS database
                print(f"--- QUERYING Tables inside {hms_db} ---")
                await cur.execute(f"SELECT TABLE_NAME FROM [{hms_db}].INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE = 'BASE TABLE'")
                tables = [row[0] for row in await cur.fetchall()]
                print(f"Tables in {hms_db}: {tables}")
                
                # Check Doctor table details
                doctor_table = next((t for t in tables if t.lower() == "doctors"), None)
                if doctor_table:
                    await cur.execute(f"SELECT COUNT(*) FROM [{hms_db}].dbo.[{doctor_table}]")
                    cnt = (await cur.fetchone())[0]
                    print(f"Total doctors in [{doctor_table}]: {cnt}")
                    
                    # Fetch details of DoctorId: 1ac0a7d9-83ee-4ea7-a545-969499498657
                    await cur.execute(f"SELECT COLUMN_NAME FROM [{hms_db}].INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = '{doctor_table}'")
                    cols = [row[0] for row in await cur.fetchall()]
                    print(f"Doctor columns: {cols}")
                    
                    await cur.execute(f"SELECT * FROM [{hms_db}].dbo.[{doctor_table}] WHERE DoctorID = '1ac0a7d9-83ee-4ea7-a545-969499498657'")
                    row = await cur.fetchone()
                    print(f"Doctor 1ac0a7d9-83ee-4ea7-a545-969499498657: {row}")
                    
                    await cur.execute(f"SELECT * FROM [{hms_db}].dbo.[{doctor_table}] WHERE DoctorID = 'a496cbcb-b45f-4df9-9649-6d2117ae1005'")
                    row_anil = await cur.fetchone()
                    print(f"Doctor a496cbcb-b45f-4df9-9649-6d2117ae1005: {row_anil}")
                    
                    if row:
                        doc_dict = dict(zip(cols, row))
                        user_id = doc_dict.get("UserID")
                        print(f"Doctor UserID: {user_id}")
                        
                        user_table = next((t for t in tables if t.lower() == "users"), None)
                        if user_table and user_id:
                            await cur.execute(f"SELECT * FROM [{hms_db}].dbo.[{user_table}] WHERE UserID = '{user_id}'")
                            user_row = await cur.fetchone()
                            print(f"User for doctor: {user_row}")
                            
                        profile_table = next((t for t in tables if t.lower() in ["userprofile", "userprofiles"]), None)
                        if profile_table and user_id:
                            await cur.execute(f"SELECT * FROM [{hms_db}].dbo.[{profile_table}] WHERE UserID = '{user_id}'")
                            profile_rows = await cur.fetchall()
                            print(f"UserProfile rows: {profile_rows}")

                # Let's inspect Hospitals
                hosp_table = next((t for t in tables if t.lower() == "hospitals"), None)
                if hosp_table:
                    await cur.execute(f"SELECT HospitalID, Name, IsPubliclyListed FROM [{hms_db}].dbo.[{hosp_table}]")
                    hosp_rows = await cur.fetchall()
                    print(f"Hospitals: {hosp_rows}")

        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to inspect databases: {e}")

if __name__ == "__main__":
    asyncio.run(check())
