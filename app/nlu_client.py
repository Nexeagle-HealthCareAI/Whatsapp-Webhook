import json
import logging
import httpx
from app.config import settings
from app.nlu_config import SYSTEM_PROMPT
from app.nlu_validator import validate_nlu_response
from app.model_config import PRIMARY_NLU, FALLBACK_NLU

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
    """Classifies incoming text messages using configured Primary NLU, with optional configured Fallback NLU.

    Enforces schema matching via validate_nlu_response.
    """
    sarvam_api_key = getattr(settings, "sarvam_api_key", None)

    # 1. Attempt Primary classification
    if PRIMARY_NLU["provider"] == "sarvam" and sarvam_api_key and sarvam_api_key != "test":
        try:
            logger.info("Attempting NLU classification using %s for utterance: %r", PRIMARY_NLU["model"], text)
            result = await _query_sarvam(text, sarvam_api_key, PRIMARY_NLU)
            if result:
                if "entities" in result and "datetime" in result["entities"]:
                    normalized = normalize_datetime_to_date(result["entities"]["datetime"])
                    if normalized:
                        result["entities"]["datetime"] = normalized
                validated = validate_nlu_response(result, raw_text=text, model_name=PRIMARY_NLU["model"])
                logger.info("Primary NLU classification successful: %s", validated)
                return validated
        except Exception as exc:
            logger.warning("Primary NLU classification failed: %s", exc)

    # 2. Attempt Fallback classification if configured
    if FALLBACK_NLU:
        # In the future, fallback providers can be implemented here (e.g. OpenAI GPT-4)
        logger.info("Fallback NLU is configured but currently unsupported or skipped.")

    # 3. Hard fallback if all attempts fail
    logger.error("NLU classification failed for utterance: %r. Returning out_of_scope fallback.", text)
    return {
        "intent": "out_of_scope",
        "entities": {},
        "confidence": "low",
        "_validated": True,
        "_had_hallucination": False
    }

async def _query_sarvam(text: str, api_key: str, config: dict, retry: bool = True) -> dict | None:
    url = config["endpoint"]
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json"
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
        "reasoning_effort": None
    }
    
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
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
                return await _query_sarvam_retry(text, api_key, config)
            return None

async def _query_sarvam_retry(text: str, api_key: str, config: dict) -> dict | None:
    url = config["endpoint"]
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json"
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text + "\nReminder: respond with JSON only"}
        ],
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
        "reasoning_effort": None
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
        if resp.status_code == 200:
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"].strip()
                return json.loads(content)
            except Exception:
                pass
        return None
