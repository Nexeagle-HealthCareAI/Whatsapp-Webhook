"""
app/decision_maker/symptom_matcher.py
--------------------------------------
Pure string matching, moved out of app/messengers/symptom_client.py (SOLID rebuild
Phase 2/24) — this has zero I/O and doesn't belong in an external-system adapter file.
route_symptom() (the actual NexEagleWebsite API call) stays in symptom_client.py; only
the label-matching decision moves here.
"""


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
    for category in available_categories:
        if target in category.lower() or category.lower() in target:
            return category
    return None
