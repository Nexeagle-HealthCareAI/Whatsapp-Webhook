"""
app/conversation/booking_confirmation.py
-------------------------------------------
Confirmation & booking domain. Currently holds only the pure line-formatting
helpers used to build the confirmation summary. _handle_confirming stays in
__init__.py -- it touches db/whatsapp_client/_send_patient_details_flow and
would need the lazy-import discipline (see docs/architecture.md) to move
safely. Not attempted in this phase.
"""
from app.i18n import t
from app.conversation.doctor_list import _doctor_distance_km


def _clinic_line(context: dict, lang: str | None) -> str:
    """Where the patient actually has to travel to, as one line.

    Worth spelling out at confirm time: the search now reaches up to 75km, so the chosen
    doctor may well be in a different town from the patient. Previously this only became
    apparent after confirming, when the map pin arrived."""
    parts = [p for p in (context.get("hospital_name"), context.get("hospital_city")) if p]
    where = ", ".join(parts) or t("clinic_unknown", lang)
    distance = _doctor_distance_km(
        {"latitude": context.get("hospital_lat"), "longitude": context.get("hospital_lng")},
        context.get("patient_lat"), context.get("patient_lng"),
    )
    if distance != float("inf"):
        where += f" · {distance:.0f} km"
    return where


def _patient_line(context: dict, lang: str | None) -> str:
    name = context.get("patient_display_name") or t("you", lang)
    age = context.get("patient_age")
    gender = context.get("patient_gender")
    guardian = context.get("patient_guardian")

    parts = [name]
    if age:
        parts.append(str(age))
    if gender:
        parts.append(gender)
    line = ", ".join(parts)
    if guardian:
        line += f" (Guardian: {guardian})"
    return line
