"""
Regression test for a real bug: age/gender/guardian collected via the WhatsApp patient-
details form never showed up on the patient record in 1HMS, because hms_client.book_appointment
used to fold them into the appointment's free-text `reason` field instead of 1HMS's own
dedicated Patient.Age/AgeUnit/Sex/GuardianName fields (confirmed against 1HMSAPI-temp's
PublicBookAppointmentRequestModel + AppointmentBookingHelpers.FindOrCreatePatientAsync, which
only ever persists those onto the patient record when present as structured fields -- text
buried in Reason is never parsed back out). This asserts the actual JSON body book_appointment
sends, via an httpx.MockTransport capturing the outgoing request -- no pytest, run directly:
python3 test_hms_client.py
"""

import asyncio
import json
import os
import sys
from datetime import date
from unittest.mock import patch

import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app.messengers import hms_client  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


def _mock_client(captured: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "appointmentId": "appt-1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://hms.test")


def test_age_gender_guardian_sent_as_structured_patient_fields():
    print("\n--- book_appointment sends age/gender/guardian as real Patient fields ---")
    captured: dict = {}

    async def _run():
        with patch.object(hms_client, "_get_client", lambda: _mock_client(captured)):
            await hms_client.book_appointment(
                "Riya", "919876543210", "doc-1", date(2026, 9, 1), "morning",
                patient_age=8, patient_gender="Female", patient_guardian="Rajesh",
            )

    run(_run())
    patient = captured["body"].get("patient", {})
    check(patient.get("age") == 8, f"age sent as a real Patient.age field, got {patient!r}")
    check(patient.get("ageUnit") == "Y", f"ageUnit defaults to years, got {patient!r}")
    check(patient.get("sex") == "Female", f"gender sent as Patient.sex, got {patient!r}")
    check(patient.get("guardianName") == "Rajesh", f"guardian sent as Patient.guardianName, got {patient!r}")
    reason = captured["body"].get("reason", "")
    check("8" not in reason and "Female" not in reason and "Rajesh" not in reason,
          f"age/gender/guardian are NOT also duplicated into the free-text reason, got {reason!r}")


def test_missing_age_gender_guardian_omitted_not_sent_as_nulls():
    print("\n--- book_appointment omits age/gender/guardian entirely when not provided ---")
    captured: dict = {}

    async def _run():
        with patch.object(hms_client, "_get_client", lambda: _mock_client(captured)):
            await hms_client.book_appointment(
                "Aquib", "919876543210", "doc-1", date(2026, 9, 1), "morning",
            )

    run(_run())
    patient = captured["body"].get("patient", {})
    check("age" not in patient and "ageUnit" not in patient, f"no age/ageUnit keys when age wasn't provided, got {patient!r}")
    check("sex" not in patient, f"no sex key when gender wasn't provided, got {patient!r}")
    check("guardianName" not in patient, f"no guardianName key when guardian wasn't provided, got {patient!r}")
    check(patient.get("fullName") == "Aquib" and patient.get("mobile") == "919876543210",
          f"fullName/mobile still sent as before, got {patient!r}")


def test_list_doctors_at_hospital_retries_a_transient_5xx_and_succeeds():
    print("\n--- Live-reported: 1HMS's /public/doctors intermittently 503s -- confirmed transient, a manual retry seconds later succeeded ---")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"success": True, "doctors": [{"doctorId": "d1", "fullName": "Dr. A"}]})

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://hms.test")
        with patch.object(hms_client, "_get_client", lambda: client):
            return await hms_client.list_doctors_at_hospital("hosp-1")

    doctors = run(_run())
    check(calls["count"] == 2, f"retries once after the transient 503, got {calls['count']} call(s)")
    check(doctors == [{"doctorId": "d1", "fullName": "Dr. A"}], f"succeeds with the real doctor list once the retry lands, got {doctors!r}")


def test_list_doctors_at_hospital_does_not_retry_a_4xx():
    print("\n--- A 4xx (client error, e.g. a bad hospitalId) must NOT be retried -- retrying won't fix a bad request ---")
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(404, text="Not Found")

    async def _run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://hms.test")
        with patch.object(hms_client, "_get_client", lambda: client):
            await hms_client.list_doctors_at_hospital("hosp-1")

    raised = False
    try:
        run(_run())
    except httpx.HTTPStatusError:
        raised = True
    check(raised, "a 4xx still raises")
    check(calls["count"] == 1, f"must NOT retry a 4xx -- exactly one call, got {calls['count']}")


if __name__ == "__main__":
    test_age_gender_guardian_sent_as_structured_patient_fields()
    test_missing_age_gender_guardian_omitted_not_sent_as_nulls()
    test_list_doctors_at_hospital_retries_a_transient_5xx_and_succeeds()
    test_list_doctors_at_hospital_does_not_retry_a_4xx()

    print("\n" + "=" * 50)
    if failures:
        print(f"HMS CLIENT TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL HMS CLIENT TESTS PASSED")
