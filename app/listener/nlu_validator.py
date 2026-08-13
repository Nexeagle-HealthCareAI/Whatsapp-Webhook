"""
Step 2 — Code-level validation layer
---------------------------------------
Yeh file hi hamari "model independence" ka asli implementation hai.

Idea simple hai: chahe Sarvam bole, Gemini bole, ya kal koi third model
bole — jo bhi intent/entity wapas aaye, wo humari khud ki VALID_INTENTS /
VALID_ENTITIES list (nlu_config.py mein defined) ke against check hoga.
Business logic (kaunse intents/entities exist karte hain) hamesha humare
control mein rehta hai, kisi bhi LLM ke control mein nahi.

Is file ko future nlu_client.py (Sarvam + Gemini fallback wala) mein
seedha import karke use karenge — dono providers ka output isi ek
gate se guzregaa.
"""

import logging
from app.listener.nlu_config import VALID_INTENTS, VALID_ENTITIES, VALID_LANGUAGES

logger = logging.getLogger("nlu_validator")
logging.basicConfig(level=logging.INFO)


def validate_nlu_response(parsed: dict, raw_text: str = "", model_name: str = "") -> dict:
    """
    Model se aaye parsed JSON ({intent, entities, confidence}) ko
    humari apni schema ke against validate/sanitize karta hai.

    - Agar intent VALID_INTENTS mein nahi hai -> force out_of_scope
      (aur entities clear kar di jaati hain, kyunki invalid intent ke
      saath entities ka koi matlab nahi)
    - Har entity key VALID_ENTITIES mein honi chahiye, warna wo key
      drop ho jaati hai (poori response reject nahi hoti, sirf wo
      ek galat key)
    - Har hallucination (invalid intent ya entity) LOG hoti hai —
      taaki hum baad mein dekh sakein kaunse real-world messages pe
      model confuse ho raha hai, aur usi data se nlu_config.py ke
      examples improve kar sakein
    """
    intent = parsed.get("intent", "out_of_scope")
    entities = parsed.get("entities", {}) or {}
    confidence = parsed.get("confidence", "unknown")

    hallucinated_intent = intent not in VALID_INTENTS
    if hallucinated_intent:
        logger.warning(
            "HALLUCINATED INTENT | model=%s | text=%r | got_intent=%r -> forcing out_of_scope",
            model_name, raw_text, intent,
        )
        intent = "out_of_scope"
        entities = {}

    clean_entities = {}
    for key, value in entities.items():
        if key in VALID_ENTITIES:
            clean_entities[key] = value
        else:
            logger.warning(
                "HALLUCINATED ENTITY KEY | model=%s | text=%r | dropped_key=%r",
                model_name, raw_text, key,
            )

    # Dropped (left None) rather than forced to a default -- callers treat a missing/invalid
    # value the same as the model not having offered a language opinion at all, and fall back
    # to their own local detection (see app.conversation._confirm_or_start_language).
    detected_language = parsed.get("detected_language")
    if detected_language not in VALID_LANGUAGES:
        if detected_language is not None:
            logger.warning(
                "HALLUCINATED LANGUAGE | model=%s | text=%r | got=%r",
                model_name, raw_text, detected_language,
            )
        detected_language = None

    language_confidence = parsed.get("language_confidence")
    if language_confidence not in ("high", "low"):
        language_confidence = None

    return {
        "intent": intent,
        "entities": clean_entities,
        "confidence": confidence,
        "detected_language": detected_language,
        "language_confidence": language_confidence,
        "_validated": True,
        "_had_hallucination": hallucinated_intent or (len(clean_entities) != len(entities)),
    }
