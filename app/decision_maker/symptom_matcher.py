"""
app/decision_maker/symptom_matcher.py
--------------------------------------
Pure string matching, moved out of app/messengers/symptom_client.py (SOLID rebuild
Phase 2/24) — this has zero I/O and doesn't belong in an external-system adapter file.
route_symptom() (the actual NexEagleWebsite API call) stays in symptom_client.py; only
the label-matching decision moves here.
"""

from app.i18n import CATEGORY_ALIASES


def match_category(label: str, available_categories: list[str]) -> str | None:
    """Matches a router label like "Cardiologist (Heart)" against the live
    PatientFacingCategory list from hms_client.list_specialties() — the router's dataset
    targets that taxonomy conceptually, but isn't guaranteed to match the exact string
    (e.g. the parenthetical qualifier), so this is a best-effort match, not an exact lookup."""
    target = label.split("(")[0].strip().lower()
    if not target:
        return None
    for category in available_categories:
        if category.lower() == target:
            return category
    # A hospital exposing short department-style category names ("Urology") instead of our
    # richer labels ("Urologist") -- confirmed live on prod -- won't hit the exact match
    # above, but is a known, curated synonym rather than a guess.
    for category in available_categories:
        c = category.lower()
        if CATEGORY_ALIASES.get(c, "").lower() == target or CATEGORY_ALIASES.get(target, "").lower() == c:
            return category
    # Last resort: a real shorthand ("cardio" for "Cardiologist (Heart)") is always a
    # PREFIX of the real name, never merely contained somewhere inside it -- a plain
    # "contains anywhere" check previously matched "ENT" inside "Gastroenterologist" purely
    # by coincidence (medically unrelated), which this prefix restriction rules out.
    for category in available_categories:
        c = category.lower()
        if c.startswith(target) or target.startswith(c):
            return category
    return None
