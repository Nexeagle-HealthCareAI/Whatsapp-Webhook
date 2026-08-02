import asyncio
import os
import httpx
from datetime import date
from app import db

async def check() -> None:
    # Standard DB pool check
    pool = await db.get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1")
        await cur.fetchone()
    pool.close()
    await pool.wait_closed()

    # Now simulate a public booking request via HTTP to the staging HMS API
    api_url = "https://1hms-dev-api.nexeagle.com/public/appointments"
    body = {
        "patient": {"fullName": "Test Patient", "mobile": "918319694497"},
        "doctorId": "1ac0a7d9-83ee-4ea7-a545-969499498657",
        "preferredDate": date.today().isoformat(),
        "reason": "WhatsApp booking — preferred Evening",
    }
    
    print(f"Testing public booking POST request to: {api_url}")
    print(f"Payload: {body}")
    
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(api_url, json=body)
            print(f"Response status: {response.status_code}")
            print(f"Response body: {response.text}")
            if response.status_code == 200:
                print("SUCCESS: Public booking test request succeeded!")
            else:
                print(f"FAILED: Public booking test request failed with status {response.status_code}")
    except Exception as e:
        print(f"FAILED to call public booking endpoint: {e}")

if __name__ == "__main__":
    asyncio.run(check())
