"""
Dedicated coverage for app.safety.check_safety_triage -- previously the only coverage of
this file at all was one indirect Hinglish case inside test_specialty_groups.py's
test_safety_triage_interception (which drives it through conversation.handle_message, not
this module directly, and never exercised Devanagari/Bengali script or the pair-matching
logic added for the "chest pain in unexpected phrasing" gap).

No DB/Redis dependency -- app.safety has none, so no stubs needed here either.
Run directly: python3 tests/test_safety.py
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.safety import check_safety_triage  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def _triggers(text, lang="en"):
    result = check_safety_triage(text, lang)
    return bool(result and result.get("is_emergency"))


def test_every_category_still_matches_its_original_pre_fix_phrasing():
    """Every literal phrase this module matched BEFORE today's fix must still match --
    this is the regression guard for the pairs-refactor of cardiac/respiratory/trauma."""
    originals = [
        "I have severe chest pain",
        "that sounds like a heart attack",
        "seene me dard ho raha hai",
        "chhati te batha",
        "chhati te byatha",
        "seena dard",
        "cardiac arrest ho gaya",
        "breathlessness ho raha hai",
        "difficulty breathing since morning",
        "saans lene me taklif ho rahi hai",
        "saans phulna shuru ho gaya",
        "niswas nite kosto hocche",
        "suffocation feel ho raha hai",
        "heavy bleeding ho rahi hai",
        "severe bleeding start ho gayi",
        "haath kat gaya",
        "accident hua abhi",
        "blood loss zyada ho raha hai",
        "hypovolemic shock jaisa lag raha hai",
        "patient unconscious hai",
        "behosh ho gaye",
        "fainted ho gaya",
        "stroke aaya hai",
        "seizure ho raha hai",
        "fit aana shuru hua",
        "mirgi ka daura",
    ]
    for text in originals:
        check(_triggers(text), f"original phrase must still trigger: {text!r}")


def test_existing_conversation_level_hinglish_case_still_matches():
    """The exact sentence test_specialty_groups.py's test_safety_triage_interception relies
    on -- must never regress, since that test asserts on the downstream WhatsApp reply."""
    check(
        _triggers("mere papa behosh ho gaye hain aur saans lene me taklif hai", "hg"),
        "the existing conversation-level Hinglish emergency sentence still triggers",
    )


def test_devanagari_script_triggers_across_categories():
    cases = [
        ("सीने में दर्द हो रहा है", "cardiac pair, Devanagari"),
        ("दिल का दौरा पड़ा है", "cardiac standalone, Devanagari"),
        ("सांस लेने में तकलीफ हो रही है", "respiratory pair, Devanagari"),
        ("दम घुटना शुरू हो गया", "respiratory standalone, Devanagari"),
        ("बहुत ज्यादा खून बह रहा है", "trauma pair, Devanagari"),
        ("एक्सीडेंट हो गया अभी", "trauma standalone, Devanagari"),
        ("मरीज बेहोश है", "neuro standalone, Devanagari (बेहोश)"),
        ("मिर्गी का दौरा आया", "neuro standalone, Devanagari (मिर्गी)"),
    ]
    for text, label in cases:
        check(_triggers(text, "hi"), f"Devanagari must trigger -- {label}: {text!r}")


def test_bengali_script_triggers_across_categories():
    cases = [
        ("আমার বুকে ব্যথা হচ্ছে", "cardiac pair, Bengali"),
        ("হার্ট অ্যাটাক হয়েছে", "cardiac standalone, Bengali"),
        ("আমার শ্বাসকষ্ট হচ্ছে", "respiratory standalone, Bengali (exact EMERGENCY_MESSAGES term)"),
        ("দম বন্ধ হয়ে যাচ্ছে", "respiratory standalone, Bengali"),
        ("প্রচুর রক্তপাত হচ্ছে", "trauma pair, Bengali"),
        ("দুর্ঘটনা ঘটেছে", "trauma standalone, Bengali"),
        ("সে অজ্ঞান হয়ে গেছে", "neuro standalone, Bengali"),
        ("খিঁচুনি শুরু হয়েছে", "neuro standalone, Bengali"),
    ]
    for text, label in cases:
        check(_triggers(text, "bn"), f"Bengali script must trigger -- {label}: {text!r}")


def test_proximity_matching_catches_reordered_and_non_adjacent_phrasing():
    """The actual gap this batch closes: real patients don't phrase things as the exact
    rigid "bodypart[space]symptom" adjacency the old patterns required."""
    cases = [
        "mera chest bahut pain kar raha hai",
        "pain in my chest since this morning",
        "chest me bohot dard hai",  # English body-part + Hindi symptom, same message
        "I've been having a lot of pain, it's in my chest",
    ]
    for text in cases:
        check(_triggers(text), f"reordered/non-adjacent phrasing must still trigger: {text!r}")


def test_routine_booking_text_does_not_false_positive():
    """A pair match requires BOTH halves of the concept -- a symptom word alone (pain/dard)
    must never fire on its own, since that covers every routine specialty visit."""
    cases = [
        "mujhe ghutne me dard hai",       # knee pain
        "daant me dard hai doctor chahiye",  # tooth pain
        "mujhe gynaecologist se milna hai",
        "book appointment for tomorrow please",
        "kya aapke paas cardiologist hai",   # mentions cardiac specialty, not a symptom
        "",
        "   ",
    ]
    for text in cases:
        check(not _triggers(text), f"must NOT trigger on routine text: {text!r}")


def test_substring_collision_does_not_false_positive():
    """"pain" is a substring of "Spain" -- ASCII keyword matching must use a word boundary,
    not naive substring containment, or any place-name containing a trigger word anywhere
    in the message would falsely alarm."""
    check(
        not _triggers("I want to book an appointment with a cardiologist in Spain"),
        "'Spain' must not falsely match the 'pain' keyword via substring containment",
    )


def test_alert_message_matches_the_requested_language():
    result_en = check_safety_triage("severe chest pain", "en")
    result_hi = check_safety_triage("severe chest pain", "hi")
    result_bn = check_safety_triage("severe chest pain", "bn")
    check(result_en is not None and "EMERGENCY WARNING" in result_en["alert_message"], "en alert text")
    check(result_hi is not None and "आपातकालीन चेतावनी" in result_hi["alert_message"], "hi alert text")
    check(result_bn is not None and "জরুরি সতর্কতা" in result_bn["alert_message"], "bn alert text")


if __name__ == "__main__":
    test_every_category_still_matches_its_original_pre_fix_phrasing()
    test_existing_conversation_level_hinglish_case_still_matches()
    test_devanagari_script_triggers_across_categories()
    test_bengali_script_triggers_across_categories()
    test_proximity_matching_catches_reordered_and_non_adjacent_phrasing()
    test_routine_booking_text_does_not_false_positive()
    test_substring_collision_does_not_false_positive()
    test_alert_message_matches_the_requested_language()

    print("\n" + "=" * 50)
    if failures:
        print(f"SAFETY TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL SAFETY TESTS PASSED")
