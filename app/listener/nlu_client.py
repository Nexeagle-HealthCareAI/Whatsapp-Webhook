import json
import logging

import httpx

from app.config import settings
from app.listener.model_config import FALLBACK_NLU, PRIMARY_NLU
from app.listener.nlu_config import SYSTEM_PROMPT
from app.listener.nlu_validator import validate_nlu_response
from app.decision_maker.normalizer import normalize_datetime_to_date, normalize_time_of_day

logger = logging.getLogger("nlu_client")


def _get_api_key(config: dict) -> str | None:
    """Reads whichever Settings field this provider's config points at (config["api_key_setting"]),
    instead of every call site hardcoding `settings.sarvam_api_key` -- that hardcoding is
    exactly what made swapping providers look like a one-line API-key change when it wasn't:
    the key alone doesn't carry the endpoint or the header format with it. "test" is the
    placeholder value test fixtures use for required-but-irrelevant settings fields (see
    tests' os.environ.setdefault blocks) -- treated as "not configured", same as unset."""
    key = getattr(settings, config.get("api_key_setting", ""), None)
    return key if key and key != "test" else None


def _auth_headers(config: dict, api_key: str) -> dict:
    """Builds the auth header this provider expects. config["auth_header"] names a custom
    header (Sarvam's "api-subscription-key"); omitting it falls back to the standard
    "Authorization: Bearer <key>" most OpenAI-compatible chat-completions APIs use."""
    header_name = config.get("auth_header")
    if header_name:
        return {header_name: api_key, "Content-Type": "application/json"}
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

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


def _postprocess_entities(result: dict) -> None:
    """Resolves any raw date/time-of-day text the model returned into normalized values, in
    place. Shared between primary and fallback classification below -- this resolution logic
    doesn't depend on which provider produced the entities."""
    entities = result.get("entities")
    if not entities:
        return
    if "datetime" in entities:
        raw_datetime = entities["datetime"]
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
            del entities["time_of_day"]


async def classify_message(client: httpx.AsyncClient, text: str) -> dict:
    """Classifies incoming text messages, trying PRIMARY_NLU then FALLBACK_NLU in turn --
    same generic dispatch for both, since both are just provider config dicts (see
    model_config.py); nothing here branches on which provider either one is. Enforces schema
    matching via validate_nlu_response.

    `client` is the caller's shared httpx.AsyncClient (worker.py owns one connection pool for
    the process) — reusing it avoids a fresh TCP+TLS handshake on every message.
    """
    for label, config in (("Primary", PRIMARY_NLU), ("Fallback", FALLBACK_NLU)):
        if not config:
            continue
        api_key = _get_api_key(config)
        if not api_key:
            continue
        try:
            logger.info(
                "Attempting %s NLU classification using %s for utterance: %r",
                label, config["model"], text,
            )
            result = await _query_nlu_classification(client, text, api_key, config)
            if result:
                _postprocess_entities(result)
                validated = validate_nlu_response(
                    result, raw_text=text, model_name=config["model"]
                )
                logger.info("%s NLU classification successful: %s", label, validated)
                return validated
        except Exception as exc:
            logger.warning("%s NLU classification failed: %s", label, exc)

    # Hard fallback if all attempts fail
    logger.error(
        "NLU classification failed for utterance: %r. Returning out_of_scope fallback.",
        text,
    )
    return {
        "intent": "out_of_scope",
        "entities": {},
        "confidence": "low",
        "detected_language": None,
        "language_confidence": None,
        "_validated": True,
        "_had_hallucination": False,
    }


async def generate_conversational_response(
    client: httpx.AsyncClient, step: str, context: dict, user_message: str
) -> str | None:
    """Generates a natural-sounding response using the LLM based on the current step and history context.

    Returns the generated response string, or None if the LLM query fails.
    """
    lang = context.get("lang") or "en"
    lang_labels = {
        "en": "English",
        "hi": "Hindi",
        "bn": "Bengali",
        "hg": "Hinglish (Hindi written in English alphabets/Latin script)",
    }
    lang_label = lang_labels.get(lang, "Hinglish")

    # Render system prompt
    sys_prompt = RECEPTIONIST_SYSTEM_PROMPT.format(
        context=json.dumps({k: v for k, v in context.items() if k != "_history"}),
        user_message=user_message,
        step=step,
        lang_label=lang_label,
    )

    user_prompt = f'Patient says: "{user_message}"\nNext Step needed: {step}'

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

STEP_GOALS = {
    "choosing_location": "their city, area, or a shared location, so nearby doctors can be found",
    "choosing_search_mode": "whether they want to search by symptom, by doctor name, or browse specialities",
    "awaiting_symptom": "a description of what they are feeling, so the right specialist can be suggested",
    "awaiting_doctor_name": "the name of the doctor they want to see",
    "awaiting_patient_details": "the patient's full name, age, gender and guardian name, typed as one comma-separated line",
    "search_doctor_miss": "a gentle note that no doctor matched what they typed, and an invitation to try another way",
}

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
        client,
        system_prompt,
        "Write that message now.",
        PRIMARY_NLU,
        timeout=_PHRASE_TIMEOUT_SECONDS,
    )


async def _query_llm_text(
    client: httpx.AsyncClient,
    system_prompt: str,
    user_prompt: str,
    config: dict,
    timeout: float | None = None,
) -> str | None:
    api_key = _get_api_key(config)
    if not api_key:
        return None

    url = config["endpoint"]
    headers = _auth_headers(config, api_key)
    body = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "model": config["model"],
        "temperature": 0.5,
        "max_tokens": 150,
    }
    body.update(config.get("extra_body", {}))

    try:
        resp = await client.post(
            url, headers=headers, json=body, timeout=timeout or config["timeout"]
        )
        if resp.status_code == 200:
            data = resp.json()
            return data["choices"][0]["message"]["content"].strip()
        logger.warning("LLM text generation returned status %d: %s", resp.status_code, resp.text[:500])
    except Exception as exc:
        logger.warning("LLM text generation failed: %s", exc)
    return None


async def _query_nlu_classification(
    client: httpx.AsyncClient,
    text: str,
    api_key: str,
    config: dict,
    retry: bool = True,
) -> dict | None:
    url = config["endpoint"]
    headers = _auth_headers(config, api_key)
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
    }
    body.update(config.get("extra_body", {}))

    resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
    if resp.status_code != 200:
        logger.warning(
            "%s API returned non-200 status code: %d: %s",
            config.get("provider", "NLU"), resp.status_code, resp.text[:500],
        )
        return None

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"].strip()
        return json.loads(content)
    except (KeyError, ValueError, json.JSONDecodeError) as err:
        logger.warning("Failed to parse %s response as JSON: %s", config.get("provider", "NLU"), err)
        if retry:
            logger.info("Retrying %s call with JSON reminder...", config.get("provider", "NLU"))
            return await _query_nlu_classification_retry(client, text, api_key, config)
        return None


async def _query_nlu_classification_retry(
    client: httpx.AsyncClient, text: str, api_key: str, config: dict
) -> dict | None:
    url = config["endpoint"]
    headers = _auth_headers(config, api_key)
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text + "\nReminder: respond with JSON only"},
        ],
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": config["max_tokens"],
    }
    body.update(config.get("extra_body", {}))
    resp = await client.post(url, headers=headers, json=body, timeout=config["timeout"])
    if resp.status_code == 200:
        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
            return json.loads(content)
        except Exception:
            pass
    return None


async def disambiguate_specialty(client: httpx.AsyncClient, query: str, valid_categories: list[str]) -> str | None:
    """Fallback for when a deterministic string match (symptom_client.match_category)
    finds nothing -- typically because the patient misspelled the specialty ("kardio",
    "cardeo" for "cardio"). Asks the model to PICK one of the real, already-known-valid
    categories, never to invent one -- deliberately a closed-set choice, not free
    generation, so it can't hallucinate a specialty that doesn't exist in this clinic's
    actual list. The caller (app.conversation.specialty_browsing.resolve_specialty_category)
    re-validates the answer against valid_categories before trusting it regardless -- same
    "never trust a raw LLM answer" discipline as everywhere else this bot talks to Sarvam.

    Only called when the fast, free, deterministic match already failed, so this stays off
    the hot path -- the extra latency/cost is paid only for the rare misspelled case."""
    if not valid_categories:
        return None
    options = "\n".join(f"- {c}" for c in valid_categories)
    system_prompt = (
        "A patient typed a medical specialty, possibly misspelled or shortened. Pick the ONE "
        "option below that they most likely meant. Reply with that option's exact text and "
        "nothing else -- no explanation, no punctuation. If none of the options are a "
        "reasonable match, reply with exactly: none\n\n"
        f"Options:\n{options}"
    )
    answer = await _query_llm_text(
        client, system_prompt, f'Patient typed: "{query}"', PRIMARY_NLU, timeout=3.0
    )
    if not answer:
        return None
    answer = answer.strip()
    if answer.lower() == "none":
        return None
    return answer
