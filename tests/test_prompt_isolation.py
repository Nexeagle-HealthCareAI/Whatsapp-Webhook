"""
Step 1 — Test the prompt in ISOLATION
---------------------------------------
Sirf Sarvam API + nlu_config.SYSTEM_PROMPT test karta hai. Koi webhook,
koi DB, koi business logic yahan involve nahi hai — sirf yeh confirm
karna hai ki prompt khud sahi kaam kar raha hai, baaki system banane
se pehle.

Teen categories test hoti hain:
1. TRAINING_TESTS      -> prompt ke apne few-shot examples jaise
2. NEW_TESTS            -> naye/unseen phrasings (generalization check)
3. OUT_OF_SCOPE_TESTS   -> adversarial — bot ko trick karne ki koshish
                           (movie/flight/restaurant/weather jaisa)
"""

import os
import sys
import requests
import json
import time

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.nlu_config import SYSTEM_PROMPT, VALID_INTENTS, VALID_ENTITIES
from app.nlu_validator import validate_nlu_response

# ---- CONFIG ----
# Key kabhi bhi yahan hardcode mat karna. Terminal mein pehle yeh chalao:
#   export SARVAM_API_KEY="apni_nayi_key_yahan"
# phir yeh script run karo.
SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "sk_8l4o228f_Tv0FXTttI1fzbReoZJ5Q3FWx")
MODEL = "sarvam-105b"   # humara Indic NLU model — yeh /v1 pe hi rehta hai
API_URL = "https://api.sarvam.ai/v1/chat/completions"


# 1) TRAINING DATA SE — prompt ke apne examples jaise
TRAINING_TESTS = [
    ("Hello", "greeting"),
    ("mujhe kal appointment chahiye", "book_appointment"),
    ("gyno specialist dekhna hai", "check_availability"),
    ("appointment cancel karna hai", "cancel_appointment"),
    ("pet me bahut dard ho raha hai", "describe_symptom"),
    ("Dr. Sen ki fees kitni hai?", "ask_pricing"),
    ("go back to doctor list", "navigate_back"),
    ("near kishanganj", "provide_location"),
]

# 2) NAYE/UNSEEN VARIATIONS — generalization check
NEW_TESTS = [
    ("kya Dr Sen abhi free hai", "check_availability"),
    ("mera appointment cancel kar do please", "cancel_appointment"),
    ("mujhe pet mein bahut dard ho raha hai, kya karu", "describe_symptom"),
    ("Dr Kapoor nahi Dr Sharma se milna hai", "change_selection"),
    ("parso ke liye booking chahiye", "reschedule_appointment"),
    ("orthopedic ka charge kitna hoga", "ask_pricing"),
    ("namaste", "greeting"),
    ("delhi mein koi dentist hai kya", "check_availability"),
]

# 3) OUT-OF-SCOPE — adversarial, alag domain mein trick karne ki koshish
OUT_OF_SCOPE_TESTS = [
    ("PVR mein movie ki ticket book karni hai", "out_of_scope"),
    ("train ticket booking chahiye delhi ke liye", "out_of_scope"),
    ("hotel room book karna hai gopalganj mein", "out_of_scope"),
    ("aaj ka cricket score kya hai", "out_of_scope"),
    ("mujhe ek joke sunao", "out_of_scope"),
    ("electricity bill kaise pay karu", "out_of_scope"),
    ("swiggy se khana order kardo", "out_of_scope"),
    ("what's the capital of Bihar", "out_of_scope"),
]


def query_sarvam(text):
    headers = {
        "api-subscription-key": SARVAM_API_KEY,
        "Content-Type": "application/json",
    }
    body = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "model": MODEL,
        "temperature": 0.2,
        "max_tokens": 300,
        "reasoning_effort": None,  # thinking mode OFF — not needed for classification
    }
    resp = requests.post(API_URL, headers=headers, json=body, timeout=10)
    return resp


def run_tests(test_cases, label):
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")

    passed = 0
    for text, expected_intent in test_cases:
        try:
            resp = query_sarvam(text)
            if resp.status_code != 200:
                print(f"\n❌ FAIL  \"{text}\"")
                print(f"   Status: {resp.status_code}")
                print(f"   Body  : {resp.text}")
                continue
            raw_content = resp.json()["choices"][0]["message"]["content"]

            try:
                parsed = json.loads(raw_content)
            except json.JSONDecodeError:
                print(f"\n❌ FAIL  \"{text}\"")
                print(f"   JSON parse error. Raw output: {raw_content!r}")
                continue

            actual_intent_raw = parsed.get("intent", "MISSING")
            confidence = parsed.get("confidence", "MISSING")

            validated = validate_nlu_response(parsed, raw_text=text, model_name=MODEL)
            actual_intent = validated["intent"]
            entities = validated["entities"]

            is_match = actual_intent == expected_intent
            status = "✅ PASS" if is_match else "❌ FAIL"
            if is_match:
                passed += 1

            print(f"\n{status}  \"{text}\"")
            print(f"   Expected : {expected_intent}")
            print(f"   Got      : {actual_intent}  (confidence: {confidence})")
            print(f"   Entities : {entities}")
            if validated["_had_hallucination"]:
                print(f"   ⚠️  Hallucination caught + corrected by validator (raw intent was {actual_intent_raw!r})")

        except requests.exceptions.RequestException as e:
            print(f"\n❌ FAIL  \"{text}\"")
            print(f"   Request error: {e}")

        time.sleep(0.3)  # rate-limit friendly

    print(f"\n---- {label}: {passed}/{len(test_cases)} passed ----")
    return passed, len(test_cases)


if __name__ == "__main__":
    p1, t1 = run_tests(TRAINING_TESTS, "TRAINING-STYLE TESTS")
    p2, t2 = run_tests(NEW_TESTS, "NEW / UNSEEN PHRASING TESTS")
    p3, t3 = run_tests(OUT_OF_SCOPE_TESTS, "OUT-OF-SCOPE ADVERSARIAL TESTS")

    print(f"\n{'='*70}")
    print(f"  FINAL SUMMARY")
    print(f"{'='*70}")
    print(f"  Training-style   : {p1}/{t1} passed")
    print(f"  New phrasing     : {p2}/{t2} passed")
    print(f"  Out-of-scope     : {p3}/{t3} passed")

    total_passed = p1 + p2 + p3
    total_tests = t1 + t2 + t3
    print(f"\n  Overall: {total_passed}/{total_tests}")

    if p3 < t3:
        print("\n⚠️  Kuch out-of-scope messages galat classify hue — prompt ke")
        print("   'Scope' section mein aur negative examples add karne padenge.")
    else:
        print("\n👍 Scope guardrail solid lag raha hai.")