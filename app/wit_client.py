from datetime import datetime
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from app.config import settings

WIT_API_URL = "https://api.wit.ai/message"
WIT_VERSION = "20260807"

@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=4))
async def parse_message_intent(text: str) -> dict:
    if not settings.wit_server_token or settings.wit_server_token == "test":
        return {
            "intent": "unknown",
            "confidence": 0.0,
            "doctor_name": None,
            "specialty": None,
            "symptom": None,
            "formatted_date": None
        }
    headers = {"Authorization": f"Bearer {settings.wit_server_token}"}
    params = {"v": WIT_VERSION, "q": text}
    
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(WIT_API_URL, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        
    intents = data.get("intents", [])
    top_intent = intents[0] if intents else {"name": "unknown", "confidence": 0.0}
    entities = data.get("entities", {})
    
    # Handle entity aliases (e.g. new_doctor_name vs doctor_name)
    doctor_val = _extract_body(entities, ["new_doctor_name:new_doctor_name", "doctor_name:doctor_name"])
    specialty_val = _extract_body(entities, ["specialty:specialty"])
    symptom_val = _extract_body(entities, ["symptom:symptom"])
    raw_date = _extract_value(entities, ["wit/datetime:datetime"])
    
    # Normalize ISO datetime to YYYY-MM-DD for easyHMSAPI
    formatted_date = None
    if raw_date:
        try:
            formatted_date = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            formatted_date = raw_date[:10]  # Fallback to date slice

    return {
        "intent": top_intent["name"],
        "confidence": top_intent["confidence"],
        "doctor_name": doctor_val,
        "specialty": specialty_val,
        "symptom": symptom_val,
        "formatted_date": formatted_date
    }

def _extract_body(entities: dict, keys: list) -> str | None:
    for key in keys:
        if key in entities and len(entities[key]) > 0:
            return entities[key][0].get("body")
    return None

def _extract_value(entities: dict, keys: list) -> str | None:
    for key in keys:
        if key in entities and len(entities[key]) > 0:
            return entities[key][0].get("value")
    return None
