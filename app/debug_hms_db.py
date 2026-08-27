import re
import pyodbc
from app.config import settings

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
        print("LAST 5 PATIENTS IN HMS DATABASE:")
        for r in rows:
            print(f"ID: {r[0]} | Name: {r[1]} | Age: {r[2]} {r[3]} | Sex: {r[4]} | Guardian: {r[5]} | RegisteredAt: {r[6]}")
        conn.close()
    except Exception as e:
        print(f"Error querying HMS database: {e}")

if __name__ == "__main__":
    main()
