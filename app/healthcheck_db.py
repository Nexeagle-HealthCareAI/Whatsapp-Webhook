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
            
            # Start transaction
            await cur.execute("BEGIN TRANSACTION")
            try:
                # 1. Test patient insert
                print("Simulating Patient insert...")
                reg_id = "B5A5F6E3-1C7D-4F2A-8514-A28D053F0BFB"
                patient_id = "PTID99999999"
                hosp_id = "E061EEE0-8F6C-44C7-9CD8-D6AA39D0BA01" # Star Hospital
                doc_id = "1AC0A7D9-83EE-4EA7-A545-969499498657"
                
                await cur.execute(f"""
                    INSERT INTO dbo.PatientRegistrations (
                        RegistrationId, HospitalID, PatientID, FullName, Mobile, Age, Sex, MarketingConsent, RegisteredAt
                    ) VALUES (
                        '{reg_id}', '{hosp_id}', '{patient_id}', 'Test Patient', '918319694497', 30, 'M', 0, GETUTCDATE()
                    )
                """)
                print("Patient insert succeeded!")
                
                # 2. Test appointment insert
                print("Simulating Appointment insert...")
                await cur.execute(f"""
                    INSERT INTO dbo.Appointments (
                        ApptId, HospitalID, DoctorID, PatientID, ApptDate, StartAt, EndAt,
                        CurrentStatusCode, StatusHistoryJson, LastStatusCodeAt, CreatedAt
                    ) VALUES (
                        NEWID(), '{hosp_id}', '{doc_id}', '{patient_id}', '2026-08-03', '2026-08-03 10:00:00', '2026-08-03 10:15:00',
                        'PRE_APPOINTMENT', '[]', GETUTCDATE(), GETUTCDATE()
                    )
                """)
                print("Appointment insert succeeded!")
                
            except Exception as inner_e:
                print(f"SQL Simulation FAILED: {inner_e}")
            finally:
                await cur.execute("ROLLBACK TRANSACTION")
                print("Transaction rolled back successfully.")
                
        master_pool.close()
        await master_pool.wait_closed()
    except Exception as e:
        print(f"Failed to run inspection: {e}")

if __name__ == "__main__":
    asyncio.run(check())
