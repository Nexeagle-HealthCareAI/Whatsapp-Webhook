import json
import logging
import httpx
from app.config import settings
from app.nlu_config import SYSTEM_PROMPT
from app.nlu_validator import validate_nlu_response
from app.model_config import PRIMARY_NLU, FALLBACK_NLU

logger = logging.getLogger("nlu_client")

RECEPTIONIST_SYSTEM_PROMPT = """
You are a warm, friendly, and helpful medical receptionist at NexEagle clinic.
Your job is to talk to the patient and guide them naturally to the next step of booking an appointment.

The patient's current conversation state context: {context}
The patient's last message: "{user_message}"
The next step we need them to do is: {step}
The user's preferred language is: {lang_label} (Write your response strictly in this language/dialect/script style).

Guide details for steps:
- "choosing_language": Greet the user warmly and ask them to choose their preferred language (English, Hindi, Bengali, Hinglish).
- "confirming_language": Confirm if they want to continue in the auto-detected language.
- "choosing_location": Politely ask them to share their location/GPS or type their city name so we can find doctors nearby.
- "choosing_search_mode": Ask if they want to search doctors by name, specialty, or browse all categories.
- "awaiting_symptom": Ask them to describe their symptoms so we can recommend a specialist.
- "choosing_doctor": Present the list of matching doctors and ask them to select one.
- "choosing_slot": Prompt them to select a preferred day and time shift for their appointment.
- "awaiting_patient_details": Politely ask them to type the patient's full name, age, and gender.
- "confirming": Summarize the booking details and ask them to confirm if everything looks correct.
- "general_chat": Warmly answer their general questions or chit-chat, and then guide them back to booking.

Guidelines:
1. Keep the response short, warm, and natural (1-3 sentences maximum).
2. Avoid looking like a robot. Speak like a real human receptionist on WhatsApp.
3. Respond in the style of the target language. For example, if it is Hinglish, write in warm, natural Hinglish using English characters (Latin script).
4. Strictly do NOT mention variables, technical keys, JSON structures, or internal steps.
"""

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

async def classify_message(client: httpx.AsyncClient, text: str) -> dict:
    """Classifies incoming text messages using configured Primary NLU, with optional configured Fallback NLU.

    Enforces schema matching via validate_nlu_response.

    `client` is the caller's shared httpx.AsyncClient (worker.py owns one connection pool for
    the process) — reusing it avoids a fresh TCP+TLS handshake to Sarvam on every message.
    """
    sarvam_api_key = getattr(settings, "sarvam_api_key", None)

    # 1. Attempt Primary classification
    if PRIMARY_NLU["provider"] == "sarvam" and sarvam_api_key and sarvam_api_key != "test":
        try:
            logger.info("Attempting NLU classification using %s for utterance: %r", PRIMARY_NLU["model"], text)
            result = await _query_sarvam(client, text, sarvam_api_key, PRIMARY_NLU)
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

async def generate_conversational_response(client: httpx.AsyncClient, step: str, context: dict, user_message: str) -> str | None:
    """Generates a natural-sounding response using the LLM based on the current step and history context.

    Returns the generated response string, or None if the LLM query fails.
    """
    lang = context.get("lang") or "en"
    lang_labels = {"en": "English", "hi": "Hindi", "bn": "Bengali", "hg": "Hinglish (Hindi written in English alphabets/Latin script)"}
    lang_label = lang_labels.get(lang, "Hinglish")
    
    # Render system prompt
    sys_prompt = RECEPTIONIST_SYSTEM_PROMPT.format(
        context=json.dumps({k: v for k, v in context.items() if k != "_history"}),
        user_message=user_message,
        step=step,
        lang_label=lang_label
    )
    
    # We strip history or metadata from context inside user prompt to keep it short
    user_prompt = f"Patient says: \"{user_message}\"\nNext Step needed: {step}"
    
    return await _query_llm_text(client, sys_prompt, user_prompt, PRIMARY_NLU)

async def _query_llm_text(client: httpx.AsyncClient, system_prompt: str, user_prompt: str, config: dict) -> str | None:
    sarvam_api_key = getattr(settings, "sarvam_api_key", None)
    if not sarvam_api_key or sarvam_api_key == "test":
        return None

    url = config["endpoint"]
    headers = {
        "api-subscription-key": sarvam_api_key,
        "Content-Type": "application/json"
    }
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ],
        "model": config["model"],
        "temperature": 0.5,
        "max_tokens": 150,
        "reasoning_effort": None
    }

    try:
        resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("LLM text generation failed: %s", exc)
    return None

async def _query_sarvam(client: httpx.AsyncClient, text: str, api_key: str, config: dict, retry: bool = True) -> dict | None:
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
            return await _query_sarvam_retry(client, text, api_key, config)
        return None

async def _query_sarvam_retry(client: httpx.AsyncClient, text: str, api_key: str, config: dict) -> dict | None:
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
    resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
    if resp.status_code == 200:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except Exception:
            pass
    return None
