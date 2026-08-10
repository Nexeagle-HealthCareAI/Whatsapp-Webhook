"""
app/safety.py
-------------
Safety Interceptor Gateway for detecting clinical red flags at the entry of the pipeline.
Provides a clean, modular structure to deflect medical emergencies before any slot updates,
designed to scale to full clinical triage integrations.
"""

import re
import logging

logger = logging.getLogger("safety")

# Baseline emergency triggers across supported languages (English, Hindi, Hinglish, Bengali)
# Sorted logically by clinical risk profiles.
EMERGENCY_TRIGGERS = {
    "cardiac": [
        # Chest Pain / Heart Attack
        r"\bchest\s*pain\b",
        r"\bheart\s*attack\b",
        r"\bseene\s*me\s*dard\b",
        r"\bchhati\s*te\s*batha\b",
        r"\bchhati\s*te\s*byatha\b",
        r"\bseena\s*dard\b",
        r"\bcardiac\s*arrest\b"
    ],
    "respiratory": [
        # Difficulty Breathing / Suffocation
        r"\bbreathlessness\b",
        r"\bdifficulty\s*breathing\b",
        r"\bsaans\s*lene\s*me\s*taklif\b",
        r"\bsaans\s*phulna\b",
        r"\bniswas\s*nite\s*kosto\b",
        r"\bsuffocation\b"
    ],
    "trauma": [
        # Severe bleeding, accidents, cuts
        r"\bheavy\s*bleeding\b",
        r"\bsevere\s*bleeding\b",
        r"\bhaath\s*kat\s*gaya\b",
        r"\baccident\s*hua\b",
        r"\bblood\s*loss\b",
        r"\bhypovolemic\b"
    ],
    "neurological": [
        # Unconsciousness, strokes, fits
        r"\bunconscious\b",
        r"\bbehosh\b",
        r"\bfainted\b",
        r"\bstroke\b",
        r"\bseizure\b",
        r"\bfit\s*aana\b",
        r"\bmirgi\b"
    ]
}

# Pre-packaged localized emergency responses.
EMERGENCY_MESSAGES = {
    "en": (
        "⚠️ EMERGENCY WARNING: If you are experiencing a medical emergency (such as severe chest pain, "
        "breathing difficulty, severe bleeding, or sudden weakness), please do not wait for this bot. "
        "Call an ambulance immediately or proceed to the nearest Emergency Room (ER)!"
    ),
    "hi": (
        "⚠️ आपातकालीन चेतावनी (Emergency Warning): यदि आप किसी चिकित्सीय आपात स्थिति (जैसे गंभीर छाती में दर्द, "
        "सांस लेने में कठिनाई, अत्यधिक रक्तस्राव, या अचानक कमजोरी) का सामना कर रहे हैं, तो कृपया इस बॉट की "
        "प्रतीक्षा न करें। तुरंत एम्बुलेंस को कॉल करें या निकटतम आपातकालीन कक्ष (ER) में जाएं!"
    ),
    "hg": (
        "⚠️ Emergency Warning: Agar aapko koi medical emergency hai (jaise chest me tez dard, saans lene "
        "me taklif, bahut zyada bleeding, ya achanak kamzori), toh please is bot ka wait na karein. "
        "Turant ambulance ko call karein ya nearest Emergency Room (ER) jayein!"
    ),
    "bn": (
        "⚠️ জরুরি সতর্কতা (Emergency Warning): আপনি যদি কোনো জরুরি চিকিৎসার সম্মুখীন হন (যেমন বুকে তীব্র ব্যথা, "
        "শ্বাসকষ্ট, অতিরিক্ত রক্তপাত বা হঠাৎ দুর্বলতা), অনুগ্রহ করে এই বটের জন্য অপেক্ষা করবেন না। "
        "অবçalves একটি অ্যাম্বুলেন্স কল করুন বা নিকটস্থ জরুরি কক্ষে (ER) যান!"
    )
}


def check_safety_triage(text: str, lang: str = "en") -> dict | None:
    """Scans the user text for medical emergency triggers.
    
    If an emergency keyword/pattern is matched, returns a structured payload.
    Otherwise, returns None.
    
    Provides space for future scaling (e.g. calling an external safety triage API,
    fine-tuned NLU safety model, etc.).
    """
    clean_text = (text or "").strip().lower()
    if not clean_text:
        return None

    # Check matches across category regexes
    for category, patterns in EMERGENCY_TRIGGERS.items():
        for pattern in patterns:
            if re.search(pattern, clean_text):
                logger.warning(
                    "Safety Interceptor triggered! Category: %s, Pattern: %r matched in text: %r",
                    category, pattern, text
                )
                
                # Fetch localized alert message
                alert_msg = EMERGENCY_MESSAGES.get(lang) or EMERGENCY_MESSAGES["en"]
                
                return {
                    "is_emergency": True,
                    "trigger_matched": pattern,
                    "escalation_type": category,
                    "alert_message": alert_msg
                }

    return None
