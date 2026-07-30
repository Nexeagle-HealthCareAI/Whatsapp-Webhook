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
async def list_doctors(specialty_category: str, page_size: int = 10) -> list[dict[str, Any]]:
    params = {"specialtyCategory": specialty_category, "pageSize": page_size}
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
) -> dict[str, Any]:
    # PreferredTime is a .NET TimeSpan? on the wire — deliberately left unset rather than
    # risk a serialization mismatch; PreferredDate + the shift noted in Reason is enough,
    # since neither is binding anyway (see PublicBookAppointmentRequestModel — a public
    # booking never claims a real slot, front desk picks the actual time at confirm time).
    body = {
        "patient": {"fullName": patient_name, "mobile": patient_mobile},
        "doctorId": doctor_id,
        "preferredDate": preferred_date.isoformat(),
        "reason": f"WhatsApp booking — preferred {preferred_shift_label}",
    }
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=15) as client:
        response = await client.post("/public/appointments", json=body, headers=_headers())
    if response.status_code >= 500:
        response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise HmsApiError(data.get("message") or "Booking failed")
    return data
