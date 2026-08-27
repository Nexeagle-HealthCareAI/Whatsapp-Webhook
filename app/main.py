import logging

from fastapi import FastAPI

from app.front_door import router as webhook_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="WhatsApp Appointment Booking")
app.include_router(webhook_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug-db")
async def debug_db():
    import pyodbc
    import re
    from app.config import settings
    
    conn_str = settings.sqlserver_conn_string
    conn_str_master = re.sub(r"(?i)DATABASE=[^;]+", "DATABASE=master", conn_str)
    
    try:
        conn = pyodbc.connect(conn_str_master)
        cur = conn.cursor()
        
        cur.execute("SELECT name FROM sys.databases")
        db_names = [r[0] for r in cur.fetchall()]
        
        hms_db = next((name for name in db_names if "hms" in name.lower()), "easyHMSDatabase")
        
        cur.execute(f"USE [{hms_db}]")
        cur.execute(
            "SELECT TOP 5 PatientId, FullName, Age, AgeUnit, Sex, GuardianName "
            "FROM dbo.PatientRegistrations ORDER BY RegisteredAt DESC"
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            result.append({
                "PatientId": r[0],
                "FullName": r[1],
                "Age": r[2],
                "AgeUnit": r[3],
                "Sex": r[4],
                "GuardianName": r[5]
            })
        conn.close()
        return {"success": True, "database": hms_db, "data": result}
    except Exception as e:
        return {"success": False, "error": str(e)}
