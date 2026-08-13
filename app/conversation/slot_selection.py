"""
app/conversation/slot_selection.py
-------------------------------------
Date & slot selection domain. Currently holds only the pure, never-monkeypatched
part. _clinic_now (a mutated name) and everything that calls it (_usable_shifts,
_get_offered_slots) stay in __init__.py for now, along with the async handlers
(_finalize_slot_selection, _send_slot_options, _handle_choosing_slot) -- see
docs/architecture.md and the approved plan for the lazy-import discipline they'll
need when they move here in a later phase.
"""
import logging
from datetime import time

from app.i18n import t

logger = logging.getLogger("conversation")


def _parse_shift_end(shift: dict) -> time | None:
    """The shift's finish time, e.g. "20:30:00" -> 20:30. Returns None if the API omits or
    malforms it, and callers then treat the shift as still open rather than hiding it —
    better to show a shift that has passed than to hide one that hasn't."""
    raw = shift.get("endTime")
    if not raw:
        return None
    try:
        return time.fromisoformat(str(raw))
    except ValueError:
        logger.warning("Unparseable shift endTime %r", raw)
        return None


def _format_slot_label(shift_name: str, is_today: bool, lang: str | None) -> str:
    # Get localized shift name
    shift_key = shift_name.lower()
    if shift_key == "afternoon":
        shift_key = "noon"

    localized_shift = t(f"shift_{shift_key}", lang) or shift_name

    # Get localized date label
    date_key = "date_today" if is_today else "date_tomorrow"
    localized_date = t(date_key, lang)

    return f"{localized_shift} ({localized_date})"


def _pick_matching_slot(slots: list[dict], preferred_date: str | None, time_of_day: str | None) -> dict | None:
    """Auto-select a slot already implied by what the patient said (e.g. "kal subah") so
    they aren't asked to re-pick a shift they already named. Deliberately conservative:
    with no time_of_day there's nothing to match on, and any ambiguity (more than one
    surviving candidate) falls through to the normal button prompt rather than guessing —
    this only fires when there's exactly one slot consistent with what was said."""
    if not time_of_day:
        return None
    candidates = [s for s in slots if s["shift_name"] == time_of_day]
    if preferred_date:
        candidates = [s for s in candidates if s["date"].isoformat() == preferred_date]
    return candidates[0] if len(candidates) == 1 else None
