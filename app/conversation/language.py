"""
app/conversation/language.py
------------------------------
Language detection (pure). The rest of the language domain (confirm/choose/start
handlers) stays in __init__.py for now -- they touch whatsapp_client and
_advance_booking_flow, both monkeypatched by tests, and moving them requires the
lazy-import discipline documented in __init__.py / docs/architecture.md. This file
holds only the part that's genuinely I/O-free and never patched.
"""
import re


def _detect_language(text: str) -> tuple[str | None, bool]:
    if not text:
        return None, False

    # Normalize: lowercase, strip, and strip common punctuation
    normalized = text.strip().lower()
    normalized = re.sub(r'[^\w\sऀ-ॿঀ-৿]', '', normalized)

    # Generic greetings: return None to present open language selection prompt
    greetings = {"hi", "hello", "hey", "hola", "namaste", "pranam", "helo", "hlo"}
    if normalized in greetings:
        return None, False

    # Devanagari Hindi check
    if re.search(r'[ऀ-ॿ]', text):
        return "hi", True

    # Bengali check
    if re.search(r'[ঀ-৿]', text):
        return "bn", True

    # Hinglish keywords/typos
    hinglish_keywords = {
        "mujhe", "muje", "mjhe", "mjh", "mje",
        "chahiye", "chahie", "cahiye", "chaye", "chaiye", "chahye",
        "karna", "krna", "karana", "krne", "karne", "krni", "karni", "karo", "kro",
        "hai", "bhejo", "dikhao", "dikho", "dikhayein", "dikhaye",
        "parcha", "parchi", "pacha", "dawa", "dawae", "dawai", "dawo", "hona"
    }

    # English keywords/typos
    english_keywords = {
        "book", "bok", "boke", "buk",
        "appointment", "apointment", "apointmint", "apointmet", "apointmnt", "apointement", "appontment", "appoiment", "appoinment",
        "doctor", "doctur", "doc", "dr", "docter", "dctr",
        "find", "show", "search", "prescription", "prescribtion", "prescrip", "download", "downlod", "dawnload",
        "rx", "medicine", "list", "get"
    }

    # Benglish (romanized Bengali) keywords/typos
    benglish_keywords = {
        "amar", "amr", "amake",
        "lagbe", "lagba", "lagbo",
        "chai", "chay", "dorkar", "drkar", "proyojon",
        "daktar", "daktarer", "dekhate", "dekhabo", "dekha",
        "korte", "korbo", "korate", "krte", "ashish", "ashis",
        "oushodh", "oushadh", "oshudh", "oshud", "osudh", "osud"
    }

    words = re.findall(r'\b\w+\b', normalized)
    if not words:
        return None, False

    # Calculate scores: unambiguous keywords (Hinglish/Benglish) get higher weight
    # than English keywords, which are frequently used as loanwords in other languages.
    hi_score = sum(2 for w in words if w in hinglish_keywords)
    bn_score = sum(2 for w in words if w in benglish_keywords)
    en_score = sum(1 for w in words if w in english_keywords)

    if hi_score == 0 and bn_score == 0 and en_score == 0:
        return None, False

    max_score = max(hi_score, bn_score, en_score)

    if hi_score == max_score and hi_score > 0 and hi_score > bn_score:
        return "hg", False
    elif bn_score == max_score and bn_score > 0 and bn_score > hi_score:
        return "bn", False
    elif en_score == max_score and en_score > hi_score and en_score > bn_score:
        return "en", False
    elif hi_score == en_score and hi_score > bn_score and hi_score > 0:
        return "hg", False
    elif bn_score == en_score and bn_score > hi_score and bn_score > 0:
        return "bn", False
    else:
        return None, False
