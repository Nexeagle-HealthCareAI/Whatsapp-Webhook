import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import settings

# Canonical values match app/conversation.py's _SHIFT_FALLBACK and the shift_name values
# hms_client actually returns ("Morning"/"Afternoon"/"Evening") — so a normalized
# time_of_day can be matched directly against an offered slot's shift_name with no further
# translation at the call site.
_TIME_OF_DAY_MAP = {
    "subah": "Morning",
    "savere": "Morning",
    "morning": "Morning",
    "sunrise": "Morning",
    "dopahar": "Afternoon",
    "dopeher": "Afternoon",
    "afternoon": "Afternoon",
    "noon": "Afternoon",
    "shaam": "Evening",
    "sham": "Evening",
    "evening": "Evening",
    "raat": "Evening",
    "night": "Evening",
}


def normalize_datetime_to_date(text: str) -> str | None:
    if not text:
        return None
    text_clean = text.lower().strip()

    tz = ZoneInfo(settings.clinic_timezone)
    now = datetime.now(tz).date()

    if text_clean in ("today", "aaj", "ab", "abhi", "now"):
        return now.isoformat()
    if text_clean in ("tomorrow", "kal", "tomorrow morning", "kal subah", "kal sham"):
        return (now + timedelta(days=1)).isoformat()
    if text_clean in ("day after tomorrow", "parso", "day after"):
        return (now + timedelta(days=2)).isoformat()

    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text_clean)
    if match:
        return match.group(0)

    match_dmy = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", text_clean)
    if match_dmy:
        d, m, y = match_dmy.groups()
        return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"

    days_of_week = {
        "monday": 0,
        "somvar": 0,
        "tuesday": 1,
        "mangalvar": 1,
        "wednesday": 2,
        "budhvar": 2,
        "thursday": 3,
        "guruvar": 3,
        "friday": 4,
        "shukravar": 4,
        "saturday": 5,
        "shanivar": 5,
        "sunday": 6,
        "ravivar": 6,
    }
    for day_name, target_weekday in days_of_week.items():
        if day_name in text_clean:
            current_weekday = now.weekday()
            days_ahead = target_weekday - current_weekday
            if days_ahead <= 0:
                days_ahead += 7
            return (now + timedelta(days=days_ahead)).isoformat()

    return None


def normalize_time_of_day(text: str) -> str | None:
    if not text:
        return None
    normalized = text.strip().lower()
    for keyword, canonical in _TIME_OF_DAY_MAP.items():
        if keyword in normalized:
            return canonical
    return None
