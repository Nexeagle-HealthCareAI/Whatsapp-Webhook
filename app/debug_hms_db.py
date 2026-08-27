import re
import pyodbc
import logging
from app.config import settings

logger = logging.getLogger("debug_hms_db")

def main():
    conn_str = settings.sqlserver_conn_string
    # Route database from WhatsAppBookingDev to the main easyHMSDatabase
    conn_str = re.sub(r"(?i)DATABASE=[^;]+", "DATABASE=easyHMSDatabase", conn_str)
    try:
        conn = pyodbc.connect(conn_str)
        cur = conn.cursor()
        cur.execute(
            "SELECT TOP 5 PatientId, FullName, Age, AgeUnit, Sex, GuardianName, RegisteredAt "
            "FROM dbo.PatientRegistrations ORDER BY RegisteredAt DESC"
        )
        rows = cur.fetchall()
        logger.info("LAST 5 PATIENTS IN HMS DATABASE:")
        for r in rows:
            logger.info(f"ID: {r[0]} | Name: {r[1]} | Age: {r[2]} {r[3]} | Sex: {r[4]} | Guardian: {r[5]} | RegisteredAt: {r[6]}")
        conn.close()
    except Exception as e:
        logger.error(f"Error querying HMS database: {e}")

if __name__ == "__main__":
    main()
