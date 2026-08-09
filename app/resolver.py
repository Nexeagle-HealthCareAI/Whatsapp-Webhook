"""
app/resolver.py
-----------------
Yeh "register" hai — jo user ki boli hui strings (jaise "Sharma") ko asli records
me resolve karta hai aur batata hai 0, 1, ya N matches mile (Resolution.status).
Extraction (NLU/LLM, app/nlu_client.py) se poori tarah alag hai: yahan koi LLM
call nahi hota, sirf deterministic matching/filtering.

Pure functions, koi I/O nahi — caller (abhi conversation.py, wiring ke baad
booking_slots.py-driven flow) already-fetched data (doctors list, city index)
pass karta hai. Isse testing ke liye koi network/DB mock nahi chahiye, aur
"fetch" (city_index.py, hms_client.py) "decide" (yeh file) se alag rehta hai —
booking_slots.py jaisa hi design.

Confidence ("kitna sure hai") aur cardinality ("kitne options fit karte hain")
alag axes hain — ek high-confidence NLU extraction bhi "Sharma" jaisi query pe 3
doctors se match kar sakti hai. Yahi is file ka poora point hai: Extract (LLM)
sirf ek raw string deta hai, Resolve (yeh file) decide karta hai wo string kitni
cheezon ko point karti hai.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.city_index import match_typed_city, nearest_city
from app.config import settings
from app.geo import haversine_km


@dataclass
class Resolution:
    """status: "zero" | "one" | "many".

    `value` is set only when status == "one" — callers should never read it
    otherwise. `candidates` always holds the full match list regardless of status
    (empty, one item, or many), so a caller building a "did you mean...?" list has
    it without a second lookup."""
    status: str
    value: Any = None
    candidates: list = field(default_factory=list)


def _classify(matches: list) -> Resolution:
    if not matches:
        return Resolution(status="zero", candidates=[])
    if len(matches) == 1:
        return Resolution(status="one", value=matches[0], candidates=matches)
    return Resolution(status="many", candidates=matches)


def _doctor_distance_km(doctor: dict, lat: float | None, lng: float | None) -> float:
    d_lat, d_lng = doctor.get("latitude"), doctor.get("longitude")
    if lat is None or lng is None or d_lat is None or d_lng is None:
        return float("inf")
    return haversine_km(lat, lng, d_lat, d_lng)


def match_doctor_by_query(query: str, doctors: list[dict]) -> list[dict]:
    """Fuzzy free-text match against doctor full names — tolerates "Dr.", greetings,
    and booking-phrase filler ("book appointment with..."). Returns every doctor
    whose name overlaps the cleaned query, in no particular order; resolve_doctor()
    below is what turns this into a single decision.

    Moved here unchanged from app/conversation.py's old _match_doctor_by_query —
    conversation.py now imports it from here instead of defining it locally."""
    normalized = query.lower()
    normalized = re.sub(r'[^a-z0-9\s]', ' ', normalized)

    for greeting in ["hi", "hello", "hey", "hlo"]:
        normalized = re.sub(r'\b' + greeting + r'\b', ' ', normalized)

    for prefix in [
        "book appointment at", "book appointment with", "appointment at", "appointment with",
        "book appointment", "appointment", "want to book", "book", "dr", "doctor"
    ]:
        normalized = normalized.replace(prefix, " ")

    normalized = re.sub(r'\s+', ' ', normalized).strip()
    if not normalized:
        return []

    matches = []
    for doc in doctors:
        name = (doc.get("fullName") or "").lower()
        name_clean = re.sub(r'[^a-z0-9\s]', ' ', name)
        name_clean = re.sub(r'\s+', ' ', name_clean).strip()

        if normalized in name_clean or name_clean in normalized:
            matches.append(doc)
            continue

        name_parts = name_clean.split()
        query_parts = normalized.split()
        matched_parts = 0
        for qp in query_parts:
            if len(qp) >= 3 and any(qp in np for np in name_parts):
                matched_parts += 1
        if matched_parts > 0:
            matches.append(doc)

    return matches


def resolve_doctor(
    raw_query: str,
    doctors_pool: list[dict],
    *,
    city: str | None = None,
    patient_lat: float | None = None,
    patient_lng: float | None = None,
) -> Resolution:
    """Name match, then narrow by location the same way
    conversation._search_doctors_flow already does — a name overlapping several
    doctors nationwide should resolve against the ones actually reachable, not
    force the patient to disambiguate strangers three states away. Falls back to
    the unnarrowed match list if location narrows it to nothing (a stale/wrong
    city shouldn't hide an otherwise-good name match)."""
    matches = match_doctor_by_query(raw_query, doctors_pool)
    if not matches:
        return Resolution(status="zero", candidates=[])

    local = matches
    if patient_lat is not None and patient_lng is not None:
        max_radius = settings.doctor_search_radii_km[-1]
        local = [d for d in matches if _doctor_distance_km(d, patient_lat, patient_lng) <= max_radius]
    elif city:
        city_clean = city.strip().lower()
        local = [d for d in matches if (d.get("city") or "").strip().lower() == city_clean]

    if local:
        matches = local

    return _classify(matches)


def resolve_location_from_gps(index: dict, lat: float, lng: float) -> Resolution:
    """GPS resolves to exactly one nearest city by construction — "nearest" cannot
    be ambiguous. Always status "zero" (empty index) or "one", never "many"."""
    city, _distance_km = nearest_city(index, lat, lng)
    if city is None:
        return Resolution(status="zero", candidates=[])
    value = {"city": city, "lat": lat, "lng": lng}
    return Resolution(status="one", value=value, candidates=[value])


def resolve_location_from_text(index: dict, text: str) -> Resolution:
    """Wraps city_index.match_typed_city(), which today collapses exact/substring/
    prefix matches down to a single best answer or None — it structurally cannot
    return "many" yet. This is a real, temporary gap versus the target design: a
    location-match API returning MULTIPLE candidates for typed input is expected
    shortly. When it lands, only this function's body changes to call it and
    return status="many" with real candidates — the signature and Resolution
    contract stay the same, so nothing calling resolve_location_from_text needs to
    change."""
    city = match_typed_city(index, text)
    if city is None:
        return Resolution(status="zero", candidates=[])
    value = {"city": city}
    return Resolution(status="one", value=value, candidates=[value])
