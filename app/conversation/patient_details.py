"""
app/conversation/patient_details.py
--------------------------------------
Patient details form: text parsing/validation. Pure, no I/O, never
monkeypatched. _send_patient_details_flow (a mutated name) and
_handle_awaiting_patient_details stay in __init__.py -- see docs/architecture.md
and the approved plan.
"""


def _parse_details(text: str, expected: int) -> list[str] | None:
    """'Riya, 8, Daughter' -> ['Riya', '8', 'Daughter'] when expected=3.

    Deliberately lenient about the age: free text typed on a phone keyboard will have
    inconsistent spacing and casing, and someone may well write "8 yrs" or "8 saal". The
    only hard requirement is the right number of non-empty comma-separated parts. Age is
    sanity-checked separately by _looks_like_age rather than parsed strictly here."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) != expected or not all(parts):
        return None
    return parts


def _looks_like_age(value: str) -> bool:
    """Catches a swapped 'Age, Name' or a stray phone number before it reaches the record.
    Accepts '8', '8 yrs', '8 saal' — anything whose leading digits land in 0-120."""
    digits = ""
    for char in value.strip():
        if char.isdigit():
            digits += char
        elif digits:
            break
    return bool(digits) and 0 < int(digits) <= 120
