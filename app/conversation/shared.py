"""
app/conversation/shared.py
---------------------------
Cross-cutting pure helpers used by more than one conversation domain module.
No I/O, never monkeypatched by tests (only ever called, read-only) -- safe to
import normally anywhere, no lazy-import discipline needed here.
"""


def _match_choice(input_type: str, input_value: str, valid_ids: list[str]) -> str | None:
    """Accepts a button/list tap, or plain text typed by hand matching one of the choices —
    interactive messages can scroll out of easy reach, typing 'confirm' should still work."""
    if input_type in ("button_reply", "list_reply") and input_value in valid_ids:
        return input_value
    if input_type == "text":
        normalized = input_value.strip().lower()
        for valid in valid_ids:
            if normalized == valid.lower():
                return valid
    return None
