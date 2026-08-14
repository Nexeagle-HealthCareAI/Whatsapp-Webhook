"""
app/booking_slots.py
---------------------
Yeh file "clipboard" hai — jo track karta hai booking ke liye kya pata hai aur kya
abhi blank hai, aur agla move kya hona chahiye yeh decide karta hai.

Extract (NLU) aur Resolve (DB/API lookup) dono se independent hai: yeh sirf pehle
se resolved values leta hai aur unka state maintain karta hai. Poora state ek plain
dict hai jo conversation_state.context ke andar "booking" key ke through save/load
hota hai — koi naya DB column ya table nahi chahiye, existing context_json me hi
fit ho jaata hai.

Ek hi function (next_action) decide karta hai "ab kya karna hai" — conversation.py
ke fixed current_step if/elif chain ki jagah yeh ek priority-ordered walk leta hai:
pehla slot jo filled nahi hai, wahi agla move hai. User agar ek message me 3 slots
bhar de, to bot seedha 4th slot pe pahunch jaata hai — koi fixed sequence nahi.
"""

from __future__ import annotations

# Order matters: next_action() walks this list top-to-bottom and stops at the first
# slot that isn't "filled". This list IS the conversation's priority order — there is
# no separate step machine to keep in sync with it.
SLOT_ORDER = ["lang", "location", "doctor", "date", "shift", "patient"]

# A slot invalidates the slots listed here when it's overwritten with a *different*
# value (not when first filled from blank) — e.g. picking a different doctor voids
# whatever date/shift was chosen against the old doctor's availability.
INVALIDATES: dict[str, list[str]] = {
    "location": ["doctor", "date", "shift"],
    "doctor": ["date", "shift"],
    "date": ["shift"],
}

# Every slot here EXCEPT `patient` can come back ambiguous or not-found, so all of them
# get the full blank/ambiguous/notfound/filled treatment. `patient` is the one exception:
# it arrives atomically from a completed WhatsApp Flow form (or a 4-part comma string),
# so it is either fully known or not known at all — never "these three people matched".
#
# This includes `lang` and `location`, which look like closed sets in today's code but
# are not in the design this file targets:
#   - lang: moves from the hand-maintained keyword list in conversation._detect_language()
#     to LLM detection. The result still maps to en/hi/hg/bn, but the model can be
#     *unsure* — that uncertainty is what the existing confirming_language step handles,
#     and here it is a single-candidate "ambiguous" rather than a "filled".
#   - location: a location-detection API is landing shortly that returns MULTIPLE matching
#     options for typed input when the patient hasn't shared GPS, so this genuinely goes
#     to N candidates, not just the 0/1 that city_index.match_typed_city() collapses to.
_VALID_STATUSES = {"blank", "ambiguous", "notfound", "filled"}


def _blank_slot() -> dict:
    return {"value": None, "raw": None, "candidates": [], "status": "blank", "source": None}


def empty() -> dict:
    """A fresh clipboard — every slot blank."""
    return {name: _blank_slot() for name in SLOT_ORDER}


def fill(slots: dict, name: str, value, *, raw: str | None = None, source: str = "user") -> None:
    """Sets a slot to a single resolved value and cascades invalidation to any slot
    whose value depended on it. Safe to call on an already-filled slot — that's the
    "user changed their mind" path (e.g. "nahi Sharma nahi, Verma"), and is exactly
    when the cascade needs to fire."""
    slots[name] = {"value": value, "raw": raw, "candidates": [], "status": "filled", "source": source}
    for dependent in INVALIDATES.get(name, []):
        slots[dependent] = _blank_slot()


def mark_ambiguous(slots: dict, name: str, candidates: list, *, raw: str | None = None) -> None:
    """Resolver found more than one match. Nothing to cascade yet — no value was
    actually chosen, so nothing downstream could have depended on it."""
    slots[name] = {"value": None, "raw": raw, "candidates": candidates, "status": "ambiguous", "source": "user"}


def mark_notfound(slots: dict, name: str, *, raw: str | None = None) -> None:
    """Resolver found zero matches for what the user said — distinct from "blank"
    (never asked) so the next prompt can say "couldn't find that" instead of asking
    the question fresh, as if it were being asked for the first time."""
    slots[name] = {"value": None, "raw": raw, "candidates": [], "status": "notfound", "source": "user"}


def next_action(slots: dict) -> tuple[str, str | None]:
    """Returns (action, slot_name):
      ("disambiguate", name) - resolver found multiple candidates; show them, don't ask a question
      ("retry", name)        - resolver found zero candidates for what the user said
      ("ask", name)          - slot is blank, ask for it
      ("confirm", None)      - every slot is filled, show the booking summary
    """
    for name in SLOT_ORDER:
        status = slots[name]["status"]
        if status not in _VALID_STATUSES:
            raise ValueError(f"Unknown slot status {status!r} for slot {name!r}")
        if status == "ambiguous":
            return ("disambiguate", name)
        if status == "notfound":
            return ("retry", name)
        if status == "blank":
            return ("ask", name)
    return ("confirm", None)


def known_summary(slots: dict) -> dict:
    """Compact {slot: value} for filled slots only — handed to the LLM as context so
    it never re-asks for something already on the clipboard."""
    return {name: s["value"] for name, s in slots.items() if s["status"] == "filled" and s["value"] is not None}


if __name__ == "__main__":
    # Sanity checks — no I/O, pure logic, matches the style of app/nlu_validator.py's
    # own __main__ block. Run directly: python3 app/booking_slots.py
    failures = []

    def check(condition, message):
        if not condition:
            failures.append(message)
            print(f"FAIL: {message}")
        else:
            print(f"PASS: {message}")

    # 1. Fresh clipboard asks for the first slot
    s = empty()
    check(next_action(s) == ("ask", "lang"), "empty clipboard asks for lang first")

    # 2. Filling walks forward
    fill(s, "lang", "hg")
    check(next_action(s) == ("ask", "location"), "after lang, asks for location")
    fill(s, "location", "Kishanganj")
    check(next_action(s) == ("ask", "doctor"), "after location, asks for doctor")

    # 3. Ambiguous resolver result -> disambiguate, not a repeated question
    mark_ambiguous(s, "doctor", [{"id": "d1"}, {"id": "d2"}, {"id": "d3"}], raw="Sharma")
    check(next_action(s) == ("disambiguate", "doctor"), "ambiguous doctor -> disambiguate, not ask")

    # 4. Zero matches -> retry, distinct from blank
    mark_notfound(s, "doctor", raw="Xyzzy")
    check(next_action(s) == ("retry", "doctor"), "zero matches -> retry, not ask")

    # 5. Resolving to one candidate fills forward
    fill(s, "doctor", {"id": "d2", "fullName": "Dr. Sharma"}, raw="Sharma")
    check(next_action(s) == ("ask", "date"), "resolved doctor -> asks for date next")
    fill(s, "date", "2026-08-10")
    check(next_action(s) == ("ask", "shift"), "after date, asks for shift")
    fill(s, "shift", "Morning")
    check(next_action(s) == ("ask", "patient"), "after shift, asks for patient details")
    fill(s, "patient", {"name": "Riya", "age": "8", "gender": "F", "guardian": "Mother"})
    check(next_action(s) == ("confirm", None), "all slots filled -> confirm")

    # 6. Cascade invalidation: changing the doctor must void date + shift
    fill(s, "doctor", {"id": "d5", "fullName": "Dr. Verma"}, raw="Verma")
    check(s["date"]["status"] == "blank", "changing doctor invalidates date")
    check(s["shift"]["status"] == "blank", "changing doctor invalidates shift")
    check(next_action(s) == ("ask", "date"), "after doctor change, re-asks date not patient")

    # 7. Cascade invalidation: changing location must void doctor + date + shift
    fill(s, "doctor", {"id": "d5", "fullName": "Dr. Verma"}, raw="Verma")
    fill(s, "date", "2026-08-10")
    fill(s, "shift", "Evening")
    fill(s, "location", "Patna")
    check(s["doctor"]["status"] == "blank", "changing location invalidates doctor")
    check(s["date"]["status"] == "blank", "changing location invalidates date")
    check(s["shift"]["status"] == "blank", "changing location invalidates shift")
    check(s["patient"]["status"] == "filled", "changing location does NOT invalidate patient")

    # 8. known_summary only reports filled slots, never leaks ambiguous/blank internals
    s2 = empty()
    fill(s2, "lang", "en")
    mark_ambiguous(s2, "doctor", [{"id": "d1"}], raw="Sen")
    summary = known_summary(s2)
    check(summary == {"lang": "en"}, "known_summary includes only filled slots")

    # 9. lang is resolvable too: LLM detects a language but isn't sure. One candidate,
    # status "ambiguous" -> the confirming_language "continue in Hinglish?" path, NOT a
    # fresh language menu. (Under the old keyword detector this case couldn't exist.)
    s3 = empty()
    mark_ambiguous(s3, "lang", ["hg"], raw="my chest hurts")
    check(next_action(s3) == ("disambiguate", "lang"), "unsure LLM language -> confirm, not re-ask")

    # 10. location is resolvable too: the incoming location API returns several options
    # for typed input when no GPS was shared.
    s4 = empty()
    fill(s4, "lang", "hg")
    mark_ambiguous(s4, "location", ["Kishanganj", "Kishangarh", "Kishangarh Renwal"], raw="kishan")
    check(next_action(s4) == ("disambiguate", "location"), "multiple city matches -> disambiguate")

    # 11. A resolved language must not wipe anything — lang has no dependents, so a patient
    # switching language mid-booking keeps their doctor/date/shift.
    s5 = empty()
    fill(s5, "lang", "hg")
    fill(s5, "location", "Kishanganj")
    fill(s5, "doctor", {"id": "d1"})
    fill(s5, "date", "2026-08-10")
    fill(s5, "lang", "bn")
    check(s5["doctor"]["status"] == "filled", "changing language keeps the chosen doctor")
    check(s5["date"]["status"] == "filled", "changing language keeps the chosen date")

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED with {len(failures)} errors")
    else:
        print("ALL booking_slots checks PASSED")
