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
from app.nlu_config import VALID_INTENTS, VALID_ENTITIES

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

    return {
        "intent": intent,
        "entities": clean_entities,
        "confidence": confidence,
        "_validated": True,
        "_had_hallucination": hallucinated_intent or (len(clean_entities) != len(entities)),
    }


if __name__ == "__main__":
    # Chhote sanity examples — koi API call nahi, sirf validator ka logic test
    examples = [
        {"intent": "book_appointment", "entities": {"datetime": "kal"}, "confidence": "high"},
        {"intent": "order_pizza", "entities": {"item": "margherita"}, "confidence": "high"},  # hallucinated intent
        {"intent": "check_availability", "entities": {"specialty": "gyno", "phone_number": "9876543210"}, "confidence": "high"},  # hallucinated entity key
    ]

    for ex in examples:
        result = validate_nlu_response(ex, raw_text="(sanity test)", model_name="sarvam-105b")
        print(f"\nInput  : {ex}")
        print(f"Output : {result}")