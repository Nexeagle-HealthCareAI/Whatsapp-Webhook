import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger("symptom_client")

_retry_network_errors = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(2),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=2),
    reraise=True,
)

# Reachable in dev at nexeagle-website-dev's own /api/search/parse, NOT the standalone
# NLP service's /route-symptom directly (that's not exposed/reachable this way in this
# environment) — this is NexEagleWebsite's own Next.js proxy route in front of it, and it
# returns NexEagleWebsite's specialtyId slugs (e.g. "cardiology"), not the underlying
# model's internal labels. Copied from the NLP Model repo's specialty_mapping.py
# (NEXEAGLE_SPECIALTY_ID_TO_LABEL) to translate back to a label before matching against
# easyHMSAPI's live PatientFacingCategory list.
_SPECIALTY_ID_TO_LABEL = {
    "general": "General Physician",
    "pediatrics": "Paediatrician",
    "cardiology": "Cardiologist (Heart)",
    "dermatology": "Dermatologist (Skin)",
    "orthopedics": "Orthopaedic Surgeon (Bone)",
    "gynecology": "Gynaecologist",
    "dentistry": "Dentist",
    "ent": "ENT Specialist",
    "ophthalmology": "Ophthalmologist (Eye)",
    "neurology": "Neurologist",
    "psychiatry": "Psychiatrist",
    "urology": "Urologist",
    "gastroenterology": "Gastroenterologist",
    "endocrinology": "Endocrinologist (Hormones/Diabetes)",
    "pulmonology": "Pulmonologist (Chest/Lungs)",
    "nephrology": "Nephrologist (Kidney)",
    "oncology": "Oncologist (Cancer)",
    "rheumatology": "Rheumatologist",
    "physiotherapy": "Physiotherapist / Rehab",
    "generalsurgery": "General Surgeon",
    "neurosurgery": "Neurosurgeon",
    "plasticsurgery": "Plastic Surgeon",
    "vascularsurgery": "Vascular Surgeon",
    "cardiothoracicsurgery": "Cardiothoracic Surgeon",
    "anesthesiology": "Anaesthesiologist",
    "radiology": "Radiologist",
    "pathology": "Pathologist",
    "emergencymedicine": "Emergency Medicine Specialist",
    "geriatrics": "Geriatrician",
    "sportsmedicine": "Sports Medicine Specialist",
}


@_retry_network_errors
async def route_symptom(query: str) -> list[str]:
    """Returns internal specialist labels (e.g. "Cardiologist (Heart)") translated from
    NexEagleWebsite's specialtyId slugs — see _SPECIALTY_ID_TO_LABEL above for why an extra
    translation step is needed here, unlike calling the NLP model directly."""
    async with httpx.AsyncClient(base_url=settings.symptom_api_base_url, timeout=10) as client:
        response = await client.post("/api/search/parse", json={"query": query})
    response.raise_for_status()
    data = response.json()
    slugs = data.get("specialtyIds") or ([data["specialtyId"]] if data.get("specialtyId") else [])
    labels = []
    for slug in slugs:
        label = _SPECIALTY_ID_TO_LABEL.get(slug)
        if label and label not in labels:
            labels.append(label)
    return labels


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
