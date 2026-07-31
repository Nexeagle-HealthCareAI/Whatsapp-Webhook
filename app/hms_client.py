import logging
from datetime import date as date_type
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger("hms_client")

_retry_network_errors = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    reraise=True,
)


class HmsApiError(Exception):
    """easyHMSAPI's public API returned success:false or an HTTP error status."""


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.hms_api_key:
        headers["X-Api-Key"] = settings.hms_api_key
    return headers


@_retry_network_errors
async def list_specialties() -> list[dict[str, Any]]:
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=10) as client:
        response = await client.get("/public/specialties", headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch specialties")
    return data.get("specialties", [])


@_retry_network_errors
async def list_doctors(
    specialty_category: str, page_size: int = 10, city: str | None = None
) -> list[dict[str, Any]]:
    """city narrows the search server-side. Without it this returns one page of doctors for
    the whole country, and any client-side "nearest" sort is then only sorting whatever
    arbitrary slice came back — which silently hides the genuinely nearest doctor as soon as
    a specialty has more doctors than fit on a page. `city` is matched case-insensitively
    but exactly by the API, so callers must pass a name that exists (see
    app/city_index.py) rather than raw patient text."""
    params: dict[str, Any] = {"specialtyCategory": specialty_category, "pageSize": page_size}
    if city:
        params["city"] = city
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=10) as client:
        response = await client.get("/public/doctors", params=params, headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch doctors")
    return data.get("doctors", [])


@_retry_network_errors
async def get_doctor_availability(doctor_id: str, on_date: date_type) -> dict[str, Any]:
    params = {"date": on_date.isoformat()}
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=10) as client:
        response = await client.get(
            f"/public/doctors/{doctor_id}/availability", params=params, headers=_headers()
        )
    response.raise_for_status()
    return response.json()


@_retry_network_errors
async def book_appointment(
    patient_name: str,
    patient_mobile: str,
    doctor_id: str,
    preferred_date: date_type,
    preferred_shift_label: str,
    extra_note: str | None = None,
) -> dict[str, Any]:
    # PreferredTime is a .NET TimeSpan? on the wire — deliberately left unset rather than
    # risk a serialization mismatch; PreferredDate + the shift noted in Reason is enough,
    # since neither is binding anyway (see PublicBookAppointmentRequestModel — a public
    # booking never claims a real slot, front desk picks the actual time at confirm time).
    #
    # extra_note: used for family/proxy bookings (conversation.py) to record who the visit
    # is actually for, e.g. "for Daughter (age 8)" — folded into this same free-text field
    # rather than a dedicated request field, since the public API's request schema wasn't
    # confirmed to have one; the front desk sees it either way. If 1HMS later exposes a
    # first-class relation/dependent field, switch to that instead of this text append.
    reason = f"WhatsApp booking — preferred {preferred_shift_label}"
    if extra_note:
        reason += f"; {extra_note}"
    body = {
        "patient": {"fullName": patient_name, "mobile": patient_mobile},
        "doctorId": doctor_id,
        "preferredDate": preferred_date.isoformat(),
        "reason": reason,
    }
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=15) as client:
        response = await client.post("/public/appointments", json=body, headers=_headers())
    if response.status_code >= 500:
        response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise HmsApiError(data.get("message") or "Booking failed")
    return data
