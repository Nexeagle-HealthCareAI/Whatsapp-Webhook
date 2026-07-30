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


@_retry_network_errors
async def route_symptom(query: str) -> list[str]:
    """Returns the raw internal specialist labels (e.g. "Cardiologist (Heart)"), already
    aligned to easyHMSAPI's PatientFacingCategory taxonomy per the NLP service's own README
    — NOT its specialtyIds field, which maps to NexEagleWebsite's unrelated slug taxonomy."""
    async with httpx.AsyncClient(base_url=settings.symptom_api_base_url, timeout=10) as client:
        response = await client.post("/route-symptom", json={"query": query})
    response.raise_for_status()
    data = response.json()
    return data.get("raw", {}).get("specialists", [])


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
