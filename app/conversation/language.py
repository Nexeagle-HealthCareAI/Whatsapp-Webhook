"""
app/conversation/language.py
------------------------------
Language detection & confirmation: script/keyword-based detection, the
confirm-or-start entry point, the language chooser, and the confirm-language
step handler.

Cross-references back into app/conversation/__init__.py (whatsapp_client and db,
two of the 9 mutated names; _get_or_create_clipboard, _advance_booking_flow,
_transition_to, _trigger_step_prompt, still defined there) go through a
function-body-local `from app import conversation` + `conversation.<name>(...)`
-- see docs/architecture.md and app/conversation/checkin.py's module docstring.
_confirm_or_start_language and _handle_confirming_language both call _start,
which lives in this same file -- plain same-module call, no lazy import needed
for that one.
"""
import re
from uuid import uuid4

from app.i18n import LANGUAGE_LABELS, LANG_PROMPT, t
from app import booking_slots
from app.conversation.shared import _match_choice
from app.types import ConversationContext


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


async def _confirm_or_start_language(
    client, phone: str, context: ConversationContext, input_value: str, nlu_hint: dict | None = None,
) -> None:
    from app import conversation

    detected_lang, is_high_confidence = _detect_language(input_value) if input_value else (None, False)

    # Script-based detection (Devanagari/Bengali) above is already deterministic and free --
    # never second-guessed. Everything else is a keyword-overlap guess that's capped at
    # low-confidence by design (a handful of shared words is a weak signal on its own). Sarvam
    # already read this exact message for intent/entity extraction, so if it also offered a
    # confident language opinion, that's a free upgrade -- no extra API call, and it judges the
    # sentence as a whole rather than word-by-word.
    if not is_high_confidence and nlu_hint:
        sarvam_lang = nlu_hint.get("detected_language")
        sarvam_confidence = nlu_hint.get("language_confidence")
        if sarvam_lang:
            if sarvam_confidence == "high":
                detected_lang, is_high_confidence = sarvam_lang, True
            elif detected_lang is None:
                detected_lang = sarvam_lang

    if detected_lang:
        if is_high_confidence:
            context["lang"] = detected_lang
            booking = conversation._get_or_create_clipboard(context)
            booking_slots.fill(booking, "lang", detected_lang, source="user")
            await conversation.whatsapp_client.send_text(client, phone, t("welcome_banner", detected_lang))
            await conversation._advance_booking_flow(client, phone, context, booking)
        else:
            confirm_context = {
                **context,
                "guess_lang": detected_lang,
            }
            await conversation._transition_to(phone, "confirming_language", confirm_context, None)
            await conversation._trigger_step_prompt(client, phone, "confirming_language", confirm_context)
    else:
        await _start(client, phone, context)


async def _start(client, phone: str, init_context: ConversationContext | None = None) -> None:
    from app import conversation

    await conversation.whatsapp_client.send_text(client, phone, t("welcome_multilang", None))
    await conversation.whatsapp_client.send_list(
        client, phone, LANG_PROMPT, "Choose / चुनें",
        [(code, label) for code, label in LANGUAGE_LABELS.items()],
        "Languages",
    )
    ctx = init_context or {}
    ctx["session_id"] = str(uuid4())
    await conversation._transition_to(phone, "choosing_language", ctx, None)


async def _handle_choosing_language(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    lang = _match_choice(input_type, input_value, list(LANGUAGE_LABELS.keys()))
    if lang is None:
        # Note: this hint is unavoidably English-only — we don't know the language yet,
        # that's exactly what's being asked.
        await conversation.whatsapp_client.send_text(client, phone, "Please tap one of the language options above.")
        return
    context["lang"] = lang
    booking = conversation._get_or_create_clipboard(context)
    booking_slots.fill(booking, "lang", lang, source="user")
    await conversation.whatsapp_client.send_text(client, phone, t("greeting", lang))
    await conversation._advance_booking_flow(client, phone, context, booking)


async def _handle_confirming_language(client, phone, input_type, input_value, context: ConversationContext) -> None:
    from app import conversation

    guess_lang = context.get("guess_lang", "en")

    # Try direct button reply or exact match ID matching first
    choice = _match_choice(input_type, input_value, ["lang_confirm_yes", "lang_confirm_change"])

    # Fallback to colloquial text input matching
    if not choice and input_type == "text" and input_value.strip():
        val = input_value.strip().lower()
        # Yes indicators:
        yes_indicators = {
            "yes", "y", "haan", "ha", "haa", "confirm", "ok", "okay", "haji", "ji", "yes, continue", "continue",
            "হ্যাঁ", "হ্যা", "হ্যাঁ, চালিয়ে যান", "কন্টিনিউ", "হ্যাঁ কন্টিনিউ", "হ্যাঁ, চালিয়ে যাও"
        }
        # Change / No indicators:
        change_indicators = {
            "no", "change", "n", "no, change", "badlo", "badlein", "language change", "change language",
            "না", "না, পরিবর্তন করুন", "ভাষা পরিবর্তন", "ভাষা বদলান"
        }

        # Add dynamic localized button label strings to the sets
        for lang_code in LANGUAGE_LABELS.keys():
            yes_indicators.add(t("confirm_yes", lang_code).lower())
            change_indicators.add(t("confirm_change", lang_code).lower())

        if val in yes_indicators:
            choice = "lang_confirm_yes"
        elif val in change_indicators:
            choice = "lang_confirm_change"

    if choice == "lang_confirm_yes":
        lang = guess_lang
        context["lang"] = lang
        booking = conversation._get_or_create_clipboard(context)
        booking_slots.fill(booking, "lang", lang, source="user")
        await conversation._advance_booking_flow(client, phone, context, booking)
    elif choice == "lang_confirm_change":
        init_context = {}
        if "search_doctor_query" in context:
            init_context["search_doctor_query"] = context["search_doctor_query"]
        await conversation.db.clear_conversation_state(phone)
        init_context.pop("booking", None)
        await _start(client, phone, init_context)
    else:
        # Prompt them again
        prompt = t("confirm_lang_prompt", guess_lang)
        buttons = [
            ("lang_confirm_yes", t("confirm_yes", guess_lang)),
            ("lang_confirm_change", t("confirm_change", guess_lang))
        ]
        await conversation.whatsapp_client.send_buttons(client, phone, prompt, buttons)
