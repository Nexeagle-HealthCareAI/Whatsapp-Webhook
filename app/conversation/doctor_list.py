"""
app/conversation/doctor_list.py
---------------------------------
Doctor-list math/formatting: fee/rating/distance extraction, sorting, and the
compact one-line row description shown in doctor-selection lists. All pure
(no I/O, never monkeypatched -- only ever called by tests).

_fetch_doctors_near, _send_doctor_list, _render_doctor_list, and the async
handlers stay in __init__.py -- _fetch_doctors_near is one of the 9 mutated
names tests reassign directly, and _send_doctor_list/_render_doctor_list touch
db/whatsapp_client and would need the lazy-import discipline (see
docs/architecture.md) to move safely. Not attempted in this phase.
"""
from app.decision_maker.geo import haversine_km


def _doctor_fee(doctor: dict) -> float:
    fee = doctor.get("discountedFee")
    return fee if fee is not None else (doctor.get("fee") or float("inf"))


def _doctor_rating(doctor: dict) -> float:
    rating = doctor.get("rating")
    return rating if rating is not None else -1  # unrated doctors sort to the bottom, not the top


def _doctor_distance_km(doctor: dict, patient_lat: float | None, patient_lng: float | None) -> float:
    lat, lng = doctor.get("latitude"), doctor.get("longitude")
    if patient_lat is None or patient_lng is None or lat is None or lng is None:
        return float("inf")
    return haversine_km(patient_lat, patient_lng, lat, lng)


def _sort_doctors(doctors: list[dict], context: dict) -> list[dict]:
    sort_key = context.get("sort_key")
    patient_lat, patient_lng = context.get("patient_lat"), context.get("patient_lng")
    if sort_key == "rating":
        return sorted(doctors, key=lambda d: -_doctor_rating(d))
    if sort_key == "experience":
        return sorted(doctors, key=lambda d: -(d.get("experienceYears") or 0))
    if sort_key == "fee":
        return sorted(doctors, key=_doctor_fee)
    if sort_key == "nearest":
        if patient_lat is not None and patient_lng is not None:
            return sorted(doctors, key=lambda d: _doctor_distance_km(d, patient_lat, patient_lng))
        # No GPS, only a typed city — best-effort: exact city-name matches float to the
        # top, everything else keeps the API's original order. Not true distance sorting,
        # but there's no geocoding service wired up to turn "Kishanganj" into a lat/long
        # (would need a Maps API key + a new dependency — flagged as a possible follow-up,
        # not built here since it's outside what a typed city name alone can support).
        city = (context.get("location_text") or "").strip().lower()
        return sorted(doctors, key=lambda d: 0 if (d.get("city") or "").strip().lower() == city else 1)
    return doctors


def _clean_specialty(spec: str) -> str:
    if not spec:
        return ""
    spec = spec.replace("QA Dev Seed", "").strip()
    if spec.startswith("-"):
        spec = spec[1:].strip()
    spec = spec.split("/")[0].strip()
    spec = spec.split("(")[0].strip()
    spec = spec.split("-")[0].strip()
    return spec.strip()


def _clean_hospital(hosp: str) -> str:
    if not hosp:
        return ""
    hosp = hosp.replace("(QA Dev Seed)", "").replace("QA Dev Seed", "").strip()
    hosp = hosp.split("(")[0].strip()
    if hosp.endswith("-"):
        hosp = hosp[:-1].strip()
    return hosp.strip()


def _doctor_row_description(doctor: dict, context: dict) -> str:
    parts = []
    spec = (
        doctor.get("primaryMedicalSpecialityPatientFacingName")
        or doctor.get("primaryMedicalSpecialityCategory")
        or doctor.get("departmentName")
        or doctor.get("specialtyName")
        or doctor.get("specialtyCategory")
    )
    spec_cleaned = _clean_specialty(spec)
    if spec_cleaned:
        parts.append(spec_cleaned)
    distance = _doctor_distance_km(doctor, context.get("patient_lat"), context.get("patient_lng"))
    hosp = doctor.get("hospitalName") or doctor.get("city")
    hosp_cleaned = _clean_hospital(hosp)
    if hosp_cleaned:
        if distance != float("inf"):
            hosp_cleaned = f"{hosp_cleaned} ({distance:.0f}km)"
        parts.append(hosp_cleaned)
    elif distance != float("inf"):
        parts.append(f"{distance:.0f}km")

    if doctor.get("rating") is not None:
        parts.append(f"⭐{doctor['rating']}")
    fee = _doctor_fee(doctor)
    if fee != float("inf"):
        parts.append(f"₹{fee:.0f}")
    if doctor.get("experienceYears") is not None:
        parts.append(f"{doctor['experienceYears']}yrs")
    desc = " · ".join(parts)
    if len(desc) > 72:
        desc = desc[:69] + "..."
    return desc
