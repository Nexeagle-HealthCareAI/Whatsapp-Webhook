"""
app/safety.py
-------------
Safety Interceptor Gateway for detecting clinical red flags at the entry of the pipeline.
Provides a clean, modular structure to deflect medical emergencies before any slot updates,
designed to scale to full clinical triage integrations.

Two ways a category expresses its triggers:
- "standalone": a phrase that is a COMPLETE emergency signal on its own (e.g. "unconscious",
  "behosh", "seizure") -- checked as a plain regex against the whole message.
- "pairs": (group_a, group_b) -- two sets of interchangeable words that, TOGETHER, describe
  one emergency concept (a body-part/location word + a distress word, e.g. "chest" + "pain").
  Neither word alone is emergency-specific ("dard"/pain alone covers every routine booking --
  knee pain, tooth pain -- so it must never fire by itself), but the pair together is.
  Matched by PROXIMITY (both words present within PROXIMITY_WINDOW_CHARS of each other,
  either order) rather than a rigid adjacent phrase -- live phrasing varies too much to
  enumerate as literal phrases ("chest me bohot dard", "pain in my chest", "dard ho raha hai
  chest mein" all describe the same thing). A wider net here is the deliberately safer
  failure mode: an extra false-positive warning costs nothing, a missed real emergency does.

Devanagari/Bengali-script vocabulary below was pulled from this file's own EMERGENCY_MESSAGES
(already-chosen terminology for this exact use case) wherever a term existed there, rather
than translated fresh -- still worth a native-speaker pass before this ships, flagged
separately, not a blocker for the English/Hinglish coverage this batch is really about.
"""

import re
import logging

logger = logging.getLogger("safety")

# How far apart (in characters) two paired keywords can be and still count as one signal.
# ~40 chars covers a short clause either way ("mera chest bahut zyada pain kar raha hai")
# without stretching across two unrelated sentences in a longer message.
PROXIMITY_WINDOW_CHARS = 40

# Baseline emergency triggers across supported languages (English, Hindi, Hinglish, Bengali).
# Sorted logically by clinical risk profiles.
EMERGENCY_TRIGGERS = {
    "cardiac": {
        "pairs": [
            (
                ["chest", "seene", "seena", "chhati", "chhaati",
                 "छाती", "सीने", "सीना", "বুক", "বুকে"],
                ["pain", "dard", "dukh", "batha", "byatha",
                 "दर्द", "दुख", "दुःख", "ব্যথা"],
            ),
        ],
        "standalone": [
            r"\bheart\s*attack\b",
            r"\bcardiac\s*arrest\b",
            r"दिल\s*का\s*दौरा",
            r"হার্ট\s*অ্যাটাক",
        ],
    },
    "respiratory": {
        "pairs": [
            (
                ["saans", "sans", "niswas", "breathing",
                 "श्वास", "सांस", "শ্বাস", "নিশ্বাস"],
                ["taklif", "difficulty", "phulna", "problem", "kosto",
                 "तकलीफ", "तकलीफ़", "फूलना", "কষ্ট"],
            ),
        ],
        "standalone": [
            r"\bbreathlessness\b",
            r"\bsuffocation\b",
            r"दम\s*घुटना",
            r"শ্বাসকষ্ট",
            r"দম\s*বন্ধ",
        ],
    },
    "trauma": {
        "pairs": [
            (
                ["blood", "bleeding", "khoon", "khun",
                 "खून", "ख़ून", "রক্ত"],
                ["heavy", "severe", "loss", "bahut", "zyada", "jyada",
                 "ज्यादा", "अधिक", "প্রচুর"],
            ),
        ],
        "standalone": [
            r"\bhaath\s*kat\s*gaya\b",
            r"\baccident\s*hua\b",
            r"\bhypovolemic\b",
            r"एक्सीडेंट",
            r"দুর্ঘটনা",
        ],
    },
    "neurological": {
        "pairs": [],
        "standalone": [
            r"\bunconscious\b",
            r"\bbehosh\b",
            r"\bfainted\b",
            r"\bstroke\b",
            r"\bseizure\b",
            r"\bfit\s*aana\b",
            r"\bmirgi\b",
            r"बेहोश",
            r"मिर्गी",
            r"दौरा\s*पड़",
            r"অজ্ঞান",
            r"খিঁচুনি",
        ],
    },
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
        "অবিলম্বে একটি অ্যাম্বুলেন্স কল করুন বা নিকটস্থ জরুরি বিভাগে (ER) যান!"
    )
}


def _keyword_positions(text: str, keywords: list[str]) -> list[int]:
    """Character start-positions where any of `keywords` appears in `text`. ASCII keywords
    (English/Hinglish) are matched with a \\b word boundary so a substring collision like
    "pain" inside "Spain" can't fire -- Devanagari/Bengali keywords are matched as plain
    substrings, since \\b's Unicode word-boundary behaviour isn't reliable enough across
    scripts to depend on, and false-positive risk from an accidental substring hit in those
    scripts is negligible in practice (the phrases are distinctive multi-character words)."""
    positions = []
    for kw in keywords:
        if kw.isascii():
            for m in re.finditer(r"\b" + re.escape(kw) + r"\b", text):
                positions.append(m.start())
        else:
            start = 0
            while (idx := text.find(kw, start)) != -1:
                positions.append(idx)
                start = idx + 1
    return positions


def _proximity_match(text: str, group_a: list[str], group_b: list[str]) -> bool:
    """True if a word from group_a and a word from group_b both appear in `text`, within
    PROXIMITY_WINDOW_CHARS of each other, in either order."""
    pos_a = _keyword_positions(text, group_a)
    if not pos_a:
        return False
    pos_b = _keyword_positions(text, group_b)
    return any(abs(i - j) <= PROXIMITY_WINDOW_CHARS for i in pos_a for j in pos_b)


def _emergency_result(category: str, matched: str, lang: str) -> dict:
    logger.warning("Safety Interceptor triggered! Category: %s, Matched: %r", category, matched)
    alert_msg = EMERGENCY_MESSAGES.get(lang) or EMERGENCY_MESSAGES["en"]
    return {
        "is_emergency": True,
        "trigger_matched": matched,
        "escalation_type": category,
        "alert_message": alert_msg,
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

    for category, spec in EMERGENCY_TRIGGERS.items():
        for pattern in spec.get("standalone", []):
            if re.search(pattern, clean_text):
                return _emergency_result(category, pattern, lang)

        for group_a, group_b in spec.get("pairs", []):
            if _proximity_match(clean_text, group_a, group_b):
                return _emergency_result(category, f"{group_a[0]}~{group_b[0]}", lang)

    return None
