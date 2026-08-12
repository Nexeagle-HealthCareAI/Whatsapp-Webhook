"""
app/flow_policy.py
-------------------
Single arbitration point for "does THIS message override whatever local/sub-state
currently thinks is happening?"

Why this exists
----------------
app/intent_router.py keeps its own multi-turn accumulation state in Redis
(nlu:session:<wa_id>) so a patient can answer a follow-up question ("gyno" ... "kal")
across several messages. That state has real turn-taking power -- when it's still
waiting on a slot, route_intent() returns action="ask_followup" and
conversation.handle_message() returns immediately on that turn (see the call site
right before intent_router.route_intent() is invoked).

That state machine was built independently of conversation.py's own contract that
certain intents -- cancel_appointment, navigate_back, greeting -- always work, from
any step, unconditionally (see the "Prioritize NLU global intents" block in
handle_message). Nothing enforced that a NEW local sub-state couldn't quietly
reintroduce a narrower, incomplete version of that same contract.

That's exactly what happened: intent_router's awaiting_clarification branch matches
the current message against two small keyword lists (reschedule words, new-booking
words) and, on no match, just repeats its own question -- forever, for up to the
Redis session's 15-minute TTL, regardless of what the patient actually said. A bare
"hi" or "cancel" isn't in either list, so both got stuck replaying "You already have
an appointment on {date}. Would you like to book a new one, or reschedule it?"
instead of being greeted or having their booking cancelled.

The fix is not another keyword list -- it's making sure this class of bug can't recur.
This module is the one place that decides "is this message a global override", and
every caller with turn-taking power (today, just intent_router's Redis session; any
future one) is expected to check it BEFORE trusting its own local state.
"""

# Intents that must always be honoured on the message that names them, no matter what
# any local/sub-state (an in-progress multi-turn NLU accumulation, a pending
# clarification question, a stale follow-up session, ...) currently thinks is
# happening. Deliberately the same three intents conversation.py's own
# "Prioritize NLU global intents" block already treats as always-available -- this
# module doesn't invent a new contract, it just makes the existing one enforceable
# from outside conversation.py too.
GLOBAL_INTENTS = {"cancel_appointment", "navigate_back", "greeting"}

# A global override still has to be reasonably confident about what it heard -- a
# low-confidence guess of "greeting" on an ambiguous message shouldn't be allowed to
# blow away a genuinely in-progress multi-turn slot-filling session on a coin flip.
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}
_OVERRIDE_CONFIDENCE_FLOOR = "medium"


def is_global_override(intent: str | None, confidence: str | None) -> bool:
    """True when THIS message's own classification should take priority over any
    pending local/sub-state and be handled as the global intent it is, instead of
    being swallowed by whatever that sub-state currently expects to hear.

    `intent`/`confidence` are the raw, single-message NLU result for the CURRENT
    turn -- not anything merged with prior turns. That's deliberate: the whole point
    is to check what this message means on its own, before it gets folded into (and
    potentially lost inside) accumulated state from earlier turns."""
    if intent not in GLOBAL_INTENTS:
        return False
    return _CONFIDENCE_RANK.get(confidence, 0) >= _CONFIDENCE_RANK[_OVERRIDE_CONFIDENCE_FLOOR]


if __name__ == "__main__":
    # Pure logic, no I/O -- same style as app/booking_slots.py's own self-test.
    # Run directly: python3 app/flow_policy.py
    failures = []
    total = 0

    def check(condition, message):
        global total
        total += 1
        if not condition:
            failures.append(message)
            print(f"FAIL: {message}")
        else:
            print(f"PASS: {message}")

    for intent in ("cancel_appointment", "navigate_back", "greeting"):
        check(is_global_override(intent, "high"), f"{intent} at high confidence should override")
        check(is_global_override(intent, "medium"), f"{intent} at medium confidence should override")
        check(not is_global_override(intent, "low"), f"{intent} at low confidence should NOT override -- too unsure to blow away local state")

    for intent in ("book_appointment", "check_availability", "describe_symptom", "out_of_scope", None):
        check(not is_global_override(intent, "high"), f"{intent!r} is not a global intent, must never override regardless of confidence")

    check(not is_global_override("greeting", None), "missing confidence must not be treated as an override")
    check(not is_global_override("greeting", "unknown_value"), "an unrecognised confidence string must not be treated as an override")

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
    else:
        print(f"PASSED — {total} checks, all passed")
