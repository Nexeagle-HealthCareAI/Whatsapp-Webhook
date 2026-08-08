import json
import logging
import httpx
from app.config import settings
from app.nlu_config import SYSTEM_PROMPT
from app.nlu_validator import validate_nlu_response

logger = logging.getLogger("nlu_client")

def normalize_datetime_to_date(text: str) -> str | None:
    if not text:
        return None
    text_clean = text.lower().strip()
    
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(settings.clinic_timezone)
    now = datetime.now(tz).date()
    
    if text_clean in ("today", "aaj", "ab", "abhi", "now"):
        return now.isoformat()
    if text_clean in ("tomorrow", "kal", "tomorrow morning", "kal subah", "kal sham"):
        return (now + timedelta(days=1)).isoformat()
    if text_clean in ("day after tomorrow", "parso", "day after"):
        return (now + timedelta(days=2)).isoformat()
    
    import re
    match = re.search(r'(\d{4})-(\d{2})-(\d{2})', text_clean)
    if match:
        return match.group(0)
        
    match_dmy = re.search(r'(\d{1,2})[-/](\d{1,2})[-/](\d{4})', text_clean)
    if match_dmy:
        d, m, y = match_dmy.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
        
    days_of_week = {
        "monday": 0, "somvar": 0,
        "tuesday": 1, "mangalvar": 1,
        "wednesday": 2, "budhvar": 2,
        "thursday": 3, "guruvar": 3,
        "friday": 4, "shukravar": 4,
        "saturday": 5, "shanivar": 5,
        "sunday": 6, "ravivar": 6
    }
    for day_name, target_weekday in days_of_week.items():
        if day_name in text_clean:
            current_weekday = now.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7
            return (now + timedelta(days=days_ahead)).isoformat()
            
    return None

async def classify_message(text: str) -> dict:
    """Classifies incoming text messages using Sarvam AI (sarvam-105b) with Google Gemini as fallback.

    Enforces schema matching via validate_nlu_response.
    """
    sarvam_api_key = getattr(settings, "sarvam_api_key", None)
    gemini_api_key = getattr(settings, "gemini_api_key", None)

    # 1. Attempt Primary classification with Sarvam AI
    if sarvam_api_key and sarvam_api_key != "test":
        try:
            logger.info("Attempting Sarvam AI classification for utterance: %r", text)
            result = await _query_sarvam(text, sarvam_api_key)
            if result:
                if "entities" in result and "datetime" in result["entities"]:
                    normalized = normalize_datetime_to_date(result["entities"]["datetime"])
                    if normalized:
                        result["entities"]["datetime"] = normalized
                validated = validate_nlu_response(result, raw_text=text, model_name="sarvam-105b")
                logger.info("Sarvam NLU classification successful: %s", validated)
                return validated
        except Exception as exc:
            logger.warning("Sarvam AI classification failed or timed out: %s. Falling back to Gemini.", exc)

    # 2. Attempt Secondary/Fallback classification with Gemini
    if gemini_api_key and gemini_api_key != "test":
        try:
            logger.info("Attempting Gemini fallback classification for utterance: %r", text)
            result = await _query_gemini(text, gemini_api_key)
            if result:
                if "entities" in result and "datetime" in result["entities"]:
                    normalized = normalize_datetime_to_date(result["entities"]["datetime"])
                    if normalized:
                        result["entities"]["datetime"] = normalized
                validated = validate_nlu_response(result, raw_text=text, model_name="gemini-2.5-flash-lite")
                logger.info("Gemini NLU fallback classification successful: %s", validated)
                return validated
        except Exception as exc:
            logger.error("Gemini fallback classification failed: %s", exc)

    # 3. Hard fallback if both models fail
    logger.error("All NLU classification brains failed for utterance: %r. Returning out_of_scope fallback.", text)
    return {
        "intent": "out_of_scope",
        "entities": {},
        "confidence": "low",
        "_validated": True,
        "_had_hallucination": False
    }

async def _query_sarvam(text: str, api_key: str, retry: bool = True) -> dict | None:
    url = "https://api.sarvam.ai/v1/chat/completions"
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json"
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        "model": "sarvam-105b",
        "temperature": 0.2,
        "max_tokens": 300,
        "reasoning_effort": None
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body, timeout=5.0)
        if resp.status_code != 200:
            logger.warning("Sarvam API returned non-200 status code: %d", resp.status_code)
            return None
        
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except (KeyError, ValueError, json.JSONDecodeError) as err:
            logger.warning("Failed to parse Sarvam AI response as JSON: %s", err)
            if retry:
                logger.info("Retrying Sarvam AI call with JSON reminder...")
                return await _query_sarvam_retry(text, api_key)
            return None

async def _query_sarvam_retry(text: str, api_key: str) -> dict | None:
    url = "https://api.sarvam.ai/v1/chat/completions"
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json"
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text + "\nReminder: respond with JSON only"}
        ],
        "model": "sarvam-105b",
        "temperature": 0.2,
        "max_tokens": 300,
        "reasoning_effort": None
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body, timeout=5.0)
        if resp.status_code == 200:
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return json.loads(content)
            except Exception:
                pass
        return None

async def _query_gemini(text: str, api_key: str) -> dict | None:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-lite:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": f"{SYSTEM_PROMPT}\n\nUser: {text}"}
                ]
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json"
        }
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body, timeout=5.0)
        if resp.status_code != 200:
            logger.warning("Gemini API returned non-200 status code: %d", resp.status_code)
            return None
        
        try:
            data = resp.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            return json.loads(content)
        except Exception as err:
            logger.warning("Failed to parse Gemini response as JSON: %s", err)
            return None
