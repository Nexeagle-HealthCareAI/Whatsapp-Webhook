import logging
from datetime import date as date_type
from functools import lru_cache
from typing import Any

import httpx
from pydantic import BaseModel, Field
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


@lru_cache
def _get_client() -> httpx.AsyncClient:
    """One shared, reused connection to 1HMS for the process's whole lifetime, instead of a
    fresh TCP+TLS handshake on every single call — same lazy-singleton pattern as
    app.messengers.redis_client.get_redis(). Default timeout is 10s (matching most calls
    below); the handful of calls that need longer (booking, cancel/reschedule, check-in)
    override it per-request instead of per-client."""
    return httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=10)


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.hms_api_key:
        headers["X-Api-Key"] = settings.hms_api_key
    return headers


@_retry_network_errors
async def list_specialties() -> list[dict[str, Any]]:
    client = _get_client()
    response = await client.get("/public/specialties", headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch specialties")
    return data.get("specialties", [])


@_retry_network_errors
async def list_hospitals() -> list[dict[str, Any]]:
    """Platform-wide, publicly-listed hospitals -- resolver.match_hospital_by_query fuzzy-matches
    a patient's free text against this list. There's no bulk hospital-listing endpoint elsewhere
    in this file (get_hospital_by_code is a single exact-code lookup only)."""
    client = _get_client()
    response = await client.get("/public/hospitals", headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch hospitals")
    return data.get("hospitals", [])


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
    client = _get_client()
    response = await client.get("/public/doctors", params=params, headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch doctors")
    return data.get("doctors", [])


@_retry_network_errors
async def list_doctors_at_hospital(hospital_id: str, page_size: int = 10) -> list[dict[str, Any]]:
    """Doctors at one specific hospital, no specialty filter -- used by the hospital-name-search
    flow (conversation.py's _search_hospitals_flow) once a hospital match resolves, to show what
    a patient can book there. A separate function from list_doctors rather than making
    specialty_category optional on it, so every existing list_doctors call site keeps its
    current (required-specialty) contract unchanged."""
    params: dict[str, Any] = {"hospitalId": hospital_id, "pageSize": page_size}
    client = _get_client()
    response = await client.get("/public/doctors", params=params, headers=_headers())
    response.raise_for_status()
    data = response.json()
    if not data.get("success", True):
        raise HmsApiError(data.get("message") or "Failed to fetch doctors")
    return data.get("doctors", [])


@_retry_network_errors
async def get_doctor_availability(doctor_id: str, on_date: date_type) -> dict[str, Any]:
    params = {"date": on_date.isoformat()}
    client = _get_client()
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
    client = _get_client()
    response = await client.post("/public/appointments", json=body, headers=_headers(), timeout=15)
    if response.status_code >= 500:
        response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise HmsApiError(data.get("message") or "Booking failed")
    return data


class AppointmentDetail(BaseModel):
    """Typed boundary for GetPublicAppointmentHandler's response -- the ONE place that knows
    HMS's actual wire field names (the `alias`es below). Every caller works with these
    stable, Pythonic names instead; if HMS ever renames a field on the wire, only the alias
    here needs to change, not every call site. Deliberately narrow (only the fields this bot
    actually uses) -- add more as real features need them, not speculatively."""

    model_config = {"populate_by_name": True}

    appointment_id: str = Field(alias="appointmentId")
    doctor_name: str = Field(alias="doctorName")
    appt_date: str = Field(alias="apptDate")
    status_code: str = Field(alias="statusCode")


@_retry_network_errors
async def get_appointment(appointment_id: str) -> AppointmentDetail | None:
    """Minimal, unauthenticated "what's the status of this appointment" lookup (see
    GetPublicAppointmentHandler) -- used to re-verify a locally-cached booked appointment is
    still live before offering it as a cancel/reschedule candidate (staff could have
    changed/cancelled it from the hospital side since this bot last touched it). Returns None
    on success:false (e.g. unknown id) -- a normal outcome here, not raised."""
    client = _get_client()
    response = await client.get(f"/public/appointments/{appointment_id}", headers=_headers())
    if response.status_code >= 500:
        response.raise_for_status()
    data = response.json()
    if not data.get("success") or not data.get("appointment"):
        return None
    return AppointmentDetail.model_validate(data["appointment"])


@_retry_network_errors
async def cancel_appointment(
    appointment_id: str, mobile: str, reason: str | None = None
) -> dict[str, Any]:
    """Cancels a real, already-booked HMS appointment (see PublicCancelAppointmentHandler).
    appointment_id (the caller must already know it -- see db.get_booked_appointments_for_phone)
    is the primary gate; mobile is cross-checked server-side against the appointment's patient
    record. Returns the raw response dict rather than raising on success:false (unlike
    book_appointment/get_hospital_by_code) -- same reasoning as resolve_checkin: the caller needs
    the API's own `message` (e.g. "already cancelled", or the generic "not found" a mobile
    mismatch also returns) to relay back to the patient, not just a pass/fail."""
    body: dict[str, Any] = {"mobile": mobile, "reason": reason}
    client = _get_client()
    response = await client.patch(
        f"/public/appointments/{appointment_id}/cancel", json=body, headers=_headers(), timeout=15
    )
    if response.status_code >= 500:
        response.raise_for_status()
    return response.json()


@_retry_network_errors
async def reschedule_appointment(
    appointment_id: str,
    mobile: str,
    to_date: date_type,
    to_time: str | None = None,
) -> dict[str, Any]:
    """Reschedules a real, already-booked HMS appointment (see
    PublicRescheduleAppointmentHandler). to_time, if given, must be "HH:MM" (24h) -- ToStartAt is
    a full .NET DateTime? on the wire, not a bare TimeSpan, so it's combined with to_date before
    sending. Same "return the raw dict, don't raise on success:false" posture as
    cancel_appointment above."""
    body: dict[str, Any] = {"mobile": mobile, "toApptDate": to_date.isoformat()}
    if to_time:
        body["toStartAt"] = f"{to_date.isoformat()}T{to_time}:00"
    client = _get_client()
    response = await client.patch(
        f"/public/appointments/{appointment_id}/reschedule", json=body, headers=_headers(), timeout=15
    )
    if response.status_code >= 500:
        response.raise_for_status()
    return response.json()


@_retry_network_errors
async def get_hospital_by_code(hospital_code: str) -> dict[str, Any]:
    """Resolves a scanned OPD QR code to a hospital. Raises HmsApiError (never returns a
    falsy/empty dict) if the code doesn't resolve — callers should treat that as "this QR
    isn't valid" rather than distinguishing 404 from other failures."""
    client = _get_client()
    response = await client.get(f"/public/hospitals/by-code/{hospital_code}", headers=_headers())
    if response.status_code == 404:
        raise HmsApiError("Hospital code not found")
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise HmsApiError(data.get("message") or "Hospital code not found")
    return data


async def _resolve_public_redirect(path: str, not_found_message: str) -> str:
    """Shared by the three "scan/DM a code, get a document back" resolvers below. Each hits
    a [AllowAnonymous] controller (DischargeSummaryPublicController /
    PrescriptionAttachmentPublicController / VisitSummaryPublicController) that itself
    responds with an HTTP redirect to a freshly presigned document URL on success, or a 404
    if the id/token doesn't resolve — this just reads the Location header instead of
    following it, since callers need the URL itself to hand to WhatsApp's send_document, not
    the bytes. Raises HmsApiError (never returns a falsy/empty string) on anything
    unresolved, matching get_hospital_by_code's posture."""
    client = _get_client()
    response = await client.get(path, headers=_headers(), follow_redirects=False)
    if response.status_code in (301, 302, 303, 307, 308):
        location = response.headers.get("location")
        if location:
            return location
    elif response.status_code != 404:
        response.raise_for_status()
    raise HmsApiError(not_found_message)


@_retry_network_errors
async def get_discharge_summary_url(access_token: str) -> str:
    """Resolves a "DISCHARGE <token>" code to a fresh discharge-summary document URL."""
    return await _resolve_public_redirect(
        f"/public-discharge/{access_token}", "Discharge summary not available"
    )


@_retry_network_errors
async def get_prescription_attachment_url(attachment_id: str) -> str:
    """Resolves a "RX <id>" code (InkRx/manual prescription uploads) to a fresh document URL."""
    return await _resolve_public_redirect(
        f"/public-prescription/{attachment_id}", "Prescription not available"
    )


@_retry_network_errors
async def get_visit_summary_url(appointment_id: str) -> str:
    """Resolves a "RXV <id>" code (structured EPrescriptionPad e-prescriptions) to a fresh
    document URL."""
    return await _resolve_public_redirect(
        f"/public-visit-summary/{appointment_id}", "Prescription not available"
    )


@_retry_network_errors
async def get_doctor_by_id(doctor_id: str) -> dict[str, Any]:
    """Resolves a scanned doctor QR code (DRBOOK <doctorId> -- see conversation.py) to that
    exact doctor -- deterministic, no fuzzy name matching involved, unlike the free-text
    doctor-name search path (resolver.py). Returns the same {doctorId, fullName, ...} shape
    as list_doctors' entries (both come from the same /public/doctors data). Raises
    HmsApiError if the id doesn't resolve (deleted, delisted, or never existed) -- callers
    should treat that as "this doctor isn't bookable right now"."""
    client = _get_client()
    response = await client.get(f"/public/doctors/{doctor_id}", headers=_headers())
    if response.status_code == 404:
        raise HmsApiError("Doctor not found")
    response.raise_for_status()
    data = response.json()
    if not data.get("success") or not data.get("doctor"):
        raise HmsApiError(data.get("message") or "Doctor not found")
    return data["doctor"]


@_retry_network_errors
async def resolve_checkin(
    hospital_id: str, mobile: str, latitude: float, longitude: float
) -> dict[str, Any]:
    """Walk-in check-in: resolves "this phone number's appointment today at this hospital"
    without the caller needing to already know an AppointmentId. Geofence-gated server-side
    (see ResolveCheckInHandler) — never a bare mobile lookup. success:false with a
    `candidates` list means more than one appointment matched; the caller must disambiguate
    and then call issue_queue_token() for the chosen one, same as a regular success:false
    with no candidates means "too far" or "nothing found today" (see `message`)."""
    body = {"hospitalId": hospital_id, "mobile": mobile, "latitude": latitude, "longitude": longitude}
    client = _get_client()
    response = await client.post("/public/checkin/resolve", json=body, headers=_headers(), timeout=15)
    if response.status_code >= 500:
        response.raise_for_status()
    return response.json()


@_retry_network_errors
async def issue_queue_token(appointment_id: str, latitude: float, longitude: float) -> dict[str, Any]:
    """Same endpoint Phase 1 built for the direct-AppointmentId check-in path — reused here
    once the patient has disambiguated which of several today's appointments they meant
    (see resolve_checkin's `candidates` case)."""
    body = {"appointmentId": appointment_id, "latitude": latitude, "longitude": longitude}
    client = _get_client()
    response = await client.post("/public/tokens", json=body, headers=_headers(), timeout=15)
    if response.status_code >= 500:
        response.raise_for_status()
    return response.json()


async def record_lead(
    *,
    hospital_id: str,
    doctor_id: str | None = None,
    lead_type: str,
    search_query: str | None = None,
    mobile: str | None = None,
    patient_name: str | None = None,
) -> None:
    """Best-effort hospital-scoped marketing-lead beacon for the Lead Generation page
    (easyHMSWeb) -- see /public/leads (RecordLeadHandler). Deliberately the one function in
    this module that does NOT propagate HmsApiError or retry on failure: every call site is a
    side effect logged alongside an already-in-progress conversation flow (a doctor/hospital
    search or booking), and a lead-logging hiccup must never interrupt that flow. Swallows and
    logs everything internally instead."""
    try:
        body = {
            "hospitalId": hospital_id,
            "doctorId": doctor_id,
            "source": "WhatsApp",
            "leadType": lead_type,
            "searchQuery": search_query,
            "mobile": mobile,
            "patientName": patient_name,
        }
        client = _get_client()
        await client.post("/public/leads", json=body, headers=_headers())
    except Exception:
        logger.warning("Failed to record lead (%s) for hospital %s", lead_type, hospital_id, exc_info=True)
