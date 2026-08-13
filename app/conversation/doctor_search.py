"""
app/conversation/doctor_search.py
------------------------------------
Doctor/hospital-name search domain. Currently holds only the pure query-classifier.
_search_doctors_flow, _handle_doctor_search_miss, _search_hospitals_flow,
_resolve_hospital_search_match, _handle_choosing_hospital_from_search, and
_handle_awaiting_doctor_name stay in __init__.py -- they touch
db/whatsapp_client/_advance_booking_flow/_render_doctor_list and would need the
lazy-import discipline (see docs/architecture.md) to move safely. Not attempted
in this phase.
"""
import re


def _is_doctor_search_query(text: str) -> bool:
    normalized = text.strip().lower()
    match = re.search(r'\b(?:dr\.?|doctor)[.,\s]*\s*([a-zA-Z]+)', normalized)
    if match:
        next_word = match.group(1)
        forbidden = {
            "btao", "chahiye", "dikhao", "dikhayein", "hai", "ho", "kr", "raha", "se", "milna",
            "ko", "me", "ka", "ki", "ke", "kya", "kuch", "appoint", "appointment", "book",
            "booking", "list", "search", "find", "with", "for", "an", "to", "please", "pls",
            "help", "consult", "hai", "tha", "thi", "hu", "hua", "gaya", "gayi", "liye"
        }
        if next_word not in forbidden:
            return True
    return False
