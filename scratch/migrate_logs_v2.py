import asyncio
import os
import sys

sys.path.append('/Users/mdaquib/Documents/Projects/Whatsapp-Webhook')

os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "DRIVER={ODBC Driver 18 for SQL Server};SERVER=host.docker.internal,1433;DATABASE=WhatsAppBookingDev;UID=sa;PWD=Nexeagle#2026;TrustServerCertificate=yes")

from app import db

async def migrate():
    pool = await db.get_pool()
    queries = [
        "DROP TABLE IF EXISTS dbo.nlu_logs;",
        """
        CREATE TABLE dbo.nlu_logs (
            id INT IDENTITY(1,1) PRIMARY KEY,
            phone_number NVARCHAR(20) NOT NULL,
            session_id NVARCHAR(100) NULL,
            utterance NVARCHAR(MAX) NOT NULL,
            nlu_brain NVARCHAR(50) NOT NULL,
            detected_intent NVARCHAR(100) NULL,
            confidence FLOAT NULL,
            doctor_name NVARCHAR(200) NULL,
            specialty NVARCHAR(200) NULL,
            symptom NVARCHAR(MAX) NULL,
            formatted_date NVARCHAR(20) NULL,
            routed_step NVARCHAR(50) NULL,
            is_correct BIT NULL,
            user_feedback NVARCHAR(100) NULL,
            created_at DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME()
        );
        """,
        "CREATE INDEX IX_nlu_logs_phone ON dbo.nlu_logs(phone_number, created_at DESC);",
        "CREATE INDEX IX_nlu_logs_session ON dbo.nlu_logs(session_id);"
    ]
    async with pool.acquire() as conn, conn.cursor() as cur:
        for q in queries:
            print(f"Executing: {q}")
            await cur.execute(q)
    print("Database updated successfully!")
    pool.close()
    await pool.wait_closed()

if __name__ == "__main__":
    asyncio.run(migrate())
