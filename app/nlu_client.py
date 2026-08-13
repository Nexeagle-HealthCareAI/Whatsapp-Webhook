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

from app.normalizer import normalize_datetime_to_date, normalize_time_of_day

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
            if result and "entities" in result:
                entities = result["entities"]
                if "datetime" in entities:
                    raw_datetime = entities["datetime"]
                    # Sarvam may still fuse a shift qualifier into the datetime string
                    # despite the prompt asking for it separately (e.g. "kal subah" as one
                    # value) — recover it here before normalize_datetime_to_date collapses
                    # the string to a bare ISO date and throws the qualifier away.
                    if "time_of_day" not in entities:
                        fused_shift = normalize_time_of_day(raw_datetime)
                        if fused_shift:
                            entities["time_of_day"] = fused_shift
                    normalized_date = normalize_datetime_to_date(raw_datetime)
                    if normalized_date:
                        entities["datetime"] = normalized_date
                if "time_of_day" in entities:
                    normalized_shift = normalize_time_of_day(entities["time_of_day"])
                    if normalized_shift:
                        entities["time_of_day"] = normalized_shift
                    else:
                        # Couldn't map to a canonical shift — drop rather than pass raw
                        # text through to code that expects Morning/Afternoon/Evening.
                        del entities["time_of_day"]
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
        "detected_language": None,
        "language_confidence": None,
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

STEP_PROMPT_SYSTEM = """You are a warm, friendly medical receptionist at NexEagle clinic, talking to a patient on WhatsApp.

Write ONLY the next thing you would say to move the booking forward. This is the message the patient will see.

What you need from them now: {goal}
Details already collected (never ask for any of these again): {known}
Write strictly in this language/script: {lang_label}

Rules:
1. One or two short sentences. WhatsApp, not a letter.
2. Sound like a real person, not a form. No greetings if the conversation is already underway.
3. NEVER invent or mention a doctor's name, a fee, a date, a time, or a hospital. Those are shown separately — you only write the question.
4. Never mention variables, JSON, internal step names, or that you are an AI.
5. Do not add options or lists — buttons are attached separately by the system.
"""

# What each step actually needs from the patient, in plain language. Steps whose message
# carries real booking data (the confirmation summary, doctor lists, slot labels) are
# deliberately absent — those must never be model-written. See _phrase() in
# app/conversation.py.
STEP_GOALS = {
    "choosing_location": "their city, area, or a shared location, so nearby doctors can be found",
    "choosing_search_mode": "whether they want to search by symptom, by doctor name, or browse specialities",
    "awaiting_symptom": "a description of what they are feeling, so the right specialist can be suggested",
    "awaiting_doctor_name": "the name of the doctor they want to see",
    "awaiting_patient_details": "the patient's full name, age, gender and guardian name, typed as one comma-separated line",
    "search_doctor_miss": "a gentle note that no doctor matched what they typed, and an invitation to try another way",
}

# Shorter than the classification timeout: a phrasing call always has a ready template
# fallback, so waiting the full classification budget would make the patient wait longer
# for a message we could already have sent.
_PHRASE_TIMEOUT_SECONDS = 2.5


async def generate_step_prompt(
    client: httpx.AsyncClient, step: str, lang: str | None, known: dict | None = None
) -> str | None:
    """Model-written wording for a step's prompt, or None if unavailable/too slow.

    Callers MUST have a template fallback ready — see _phrase() in app/conversation.py.
    Only steps present in STEP_GOALS can be phrased; anything else returns None so a new
    step can't silently start getting model-written copy."""
    goal = STEP_GOALS.get(step)
    if goal is None:
        return None

    lang_labels = {
        "en": "English",
        "hi": "Hindi (Devanagari script)",
        "bn": "Bengali (Bengali script)",
        "hg": "Hinglish (Hindi written in English alphabets/Latin script)",
    }
    system_prompt = STEP_PROMPT_SYSTEM.format(
        goal=goal,
        known=json.dumps(known or {}, ensure_ascii=False) if known else "nothing yet",
        lang_label=lang_labels.get(lang or "en", lang_labels["en"]),
    )
    return await _query_llm_text(
        client, system_prompt, "Write that message now.", PRIMARY_NLU,
        timeout=_PHRASE_TIMEOUT_SECONDS,
    )


async def _query_llm_text(
    client: httpx.AsyncClient, system_prompt: str, user_prompt: str, config: dict,
    timeout: float | None = None,
) -> str | None:
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
        resp = await client.post(
            url, headers=headers, json=body, timeout=timeout or config["timeout"]
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        logger.warning("LLM text generation returned status %d", resp.status_code)
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
