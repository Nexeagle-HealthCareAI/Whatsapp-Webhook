import os
import sys
import types
from urllib.parse import unquote

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub aioodbc before importing app.* — the native ODBC driver isn't needed for these checks
_fake = types.ModuleType("aioodbc")
_fake.Pool = object


async def _create_pool(*a, **k):
    raise NotImplementedError


_fake.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake)

# Stub redis before importing app.*
_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")


class MockRedis:
    def __init__(self):
        self.data = {}

    @classmethod
    def from_url(cls, *args, **kwargs):
        return _mock_redis_instance

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        return True

    async def lpush(self, key, value):
        self.data.setdefault(key, []).append(value)
        return True


_mock_redis_instance = MockRedis()
_fake_redis_mod.asyncio.Redis = MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

# Now it is safe to import TestClient and app components
import asyncio
import hashlib
import hmac
import json
import time

from fastapi.testclient import TestClient

from app import db, main
from app.config import settings
from app.front_door import hms_events as hms_events_module
from app.messengers import hms_client
from app.messengers.hms_client import HmsApiError

client = TestClient(main.app)
failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def test_verify_webhook():
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.whatsapp_verify_token,
            "hub.challenge": "123456789",
        },
    )
    check(response.status_code == 200, f"Expected 200, got {response.status_code}")
    check(response.text == "123456789", f"Expected challenge back, got {response.text}")

    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "123456789",
        },
    )
    check(response.status_code == 403, f"Expected 403 for invalid token, got {response.status_code}")


def test_verify_signature_validation():
    payload = {"object": "whatsapp_business_account", "entry": []}
    body_bytes = json.dumps(payload).encode("utf-8")

    sig = hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"),
        body_bytes,
        hashlib.sha256,
    ).hexdigest()

    response = client.post(
        "/webhook",
        content=body_bytes,
        headers={"X-Hub-Signature-256": f"sha256={sig}"},
    )
    check(response.status_code == 200, f"Expected 200, got {response.status_code}")

    response = client.post(
        "/webhook",
        content=body_bytes,
        headers={"X-Hub-Signature-256": "sha256=invalid-signature-hash"},
    )
    check(response.status_code == 401, f"Expected 401, got {response.status_code}")


def _signed_post(payload: dict):
    body_bytes = json.dumps(payload).encode("utf-8")
    sig = hmac.new(settings.whatsapp_app_secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()
    return client.post("/webhook", content=body_bytes, headers={"X-Hub-Signature-256": f"sha256={sig}"})


def _message_payload(phone_number_id, message_id):
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "metadata": {"phone_number_id": phone_number_id},
                    "contacts": [{"wa_id": "919876543210", "profile": {"name": "Test"}}],
                    "messages": [{
                        "id": message_id, "from": "919876543210", "type": "text",
                        "text": {"body": "hello"},
                    }],
                },
            }],
        }],
    }


def test_webhook_for_a_different_phone_number_id_is_ignored():
    print("\n--- Live-reported: a Meta App dashboard pointed at the wrong environment's callback URL must not get processed/replied to here ---")
    _mock_redis_instance.data.pop(settings.booking_jobs_key, None)

    response = _signed_post(_message_payload("some-other-environments-phone-number-id", "wamid.other-env-1"))
    check(response.status_code == 200, f"still acks 200 (Meta shouldn't see this as a failure to retry), got {response.status_code}")
    check(
        settings.booking_jobs_key not in _mock_redis_instance.data,
        f"does NOT enqueue a message meant for a different phone_number_id, got {_mock_redis_instance.data.get(settings.booking_jobs_key)!r}",
    )


def test_webhook_for_this_servers_own_phone_number_id_still_works():
    print("\n--- Sanity: a genuine message for THIS server's own configured number is unaffected ---")
    _mock_redis_instance.data.pop(settings.booking_jobs_key, None)

    response = _signed_post(_message_payload(settings.whatsapp_phone_number_id, "wamid.own-env-1"))
    check(response.status_code == 200, f"Expected 200, got {response.status_code}")
    check(
        settings.booking_jobs_key in _mock_redis_instance.data and len(_mock_redis_instance.data[settings.booking_jobs_key]) == 1,
        f"still enqueues a genuine message for this server's own number, got {_mock_redis_instance.data.get(settings.booking_jobs_key)!r}",
    )


def test_webhook_with_no_phone_number_id_metadata_still_works():
    print("\n--- Fails open when metadata.phone_number_id is simply absent from the payload, same as every other malformed/missing-data case in this codebase ---")
    _mock_redis_instance.data.pop(settings.booking_jobs_key, None)

    payload = _message_payload(None, "wamid.no-metadata-1")
    del payload["entry"][0]["changes"][0]["value"]["metadata"]
    response = _signed_post(payload)
    check(response.status_code == 200, f"Expected 200, got {response.status_code}")
    check(
        settings.booking_jobs_key in _mock_redis_instance.data,
        f"still enqueues when phone_number_id metadata is simply missing, got {_mock_redis_instance.data.get(settings.booking_jobs_key)!r}",
    )


def test_qr_redirects():
    original_display = settings.whatsapp_display_number
    original_get_hospital = hms_client.get_hospital_by_code
    original_get_doctor = hms_client.get_doctor_by_id
    original_get_discharge = hms_client.get_discharge_summary_url

    settings.whatsapp_display_number = "12345"

    async def mock_get_hospital(code):
        if code == "HOSP1":
            return {"hospitalId": "h1", "name": "Test Hospital"}
        raise HmsApiError("Not found")

    hms_client.get_hospital_by_code = mock_get_hospital

    async def mock_get_doctor(doc_id):
        if doc_id == "DOC1":
            return {"doctorId": "DOC1", "fullName": "Dr. Smith"}
        raise HmsApiError("Not found")

    hms_client.get_doctor_by_id = mock_get_doctor

    async def mock_resolver(code):
        if code == "DOC_CODE":
            return "https://doc_url"
        raise HmsApiError("Not found")

    hms_client.get_discharge_summary_url = mock_resolver

    try:
        # Checkin
        response = client.get("/c/HOSP1", follow_redirects=False)
        check(response.status_code == 307, f"Expected 307, got {response.status_code}")
        check("https://wa.me/12345" in response.headers.get("location", ""), "Location link missing display number")

        response = client.get("/c/INVALID", follow_redirects=False)
        check(response.status_code == 404, f"Expected 404, got {response.status_code}")

        # Hospital booking -- payload is human-readable (names the hospital), not just the
        # bare code, with "QR ID of this hospital is {code}" kept as a still-parseable
        # trailing phrase (see HospitalBookingRedirectHandler.build_wa_payload's own comment).
        response = client.get("/h/HOSP1", follow_redirects=False)
        check(response.status_code == 307, f"Expected 307, got {response.status_code}")
        location = unquote(response.headers.get("location", ""))
        check("Test Hospital" in location, f"Location text doesn't name the scanned hospital: {location!r}")
        check(location.endswith("QR ID of this hospital is HOSP1"), f"Location text doesn't end with the parseable phrase: {location!r}")

        response = client.get("/h/INVALID", follow_redirects=False)
        check(response.status_code == 404, f"Expected 404, got {response.status_code}")

        # Doctor booking
        response = client.get("/doc/DOC1", follow_redirects=False)
        check(response.status_code == 307, f"Expected 307, got {response.status_code}")
        check("DRBOOK%20DOC1" in response.headers.get("location", ""), "Location query missing doctor ID")

        response = client.get("/doc/INVALID", follow_redirects=False)
        check(response.status_code == 404, f"Expected 404, got {response.status_code}")

        # Discharge
        response = client.get("/d/DOC_CODE", follow_redirects=False)
        check(response.status_code == 307, f"Expected 307, got {response.status_code}")
        check("DISCHARGE%20DOC_CODE" in response.headers.get("location", ""), "Location query missing discharge token")

        # Start (no-auth redirect)
        response = client.get("/start", follow_redirects=False)
        check(response.status_code == 307, f"Expected 307, got {response.status_code}")
        check("https://wa.me/12345" in response.headers.get("location", ""), "Location redirect failed")

        response = client.get("/d/INVALID", follow_redirects=False)
        check(response.status_code == 404, f"Expected 404, got {response.status_code}")
    finally:
        settings.whatsapp_display_number = original_display
        hms_client.get_hospital_by_code = original_get_hospital
        hms_client.get_doctor_by_id = original_get_doctor
        hms_client.get_discharge_summary_url = original_get_discharge


def _token_called_payload(event_id="evt-1", appointment_id="appt-1", token=5):
    return {"eventId": event_id, "appointmentId": appointment_id, "currentToken": token}


def test_token_called_rejects_a_missing_or_wrong_internal_token():
    print("\n--- Peer-review T3: /events/token-called had ZERO test coverage of its own "
          "auth -- a wrong/missing X-Internal-Token must be rejected before anything else runs ---")
    original_get_appt = db.get_appointment_by_hms_id
    calls = {"lookup": 0}

    async def _spy_get_appt(hms_appointment_id):
        calls["lookup"] += 1
        return None

    db.get_appointment_by_hms_id = _spy_get_appt
    try:
        response = client.post("/events/token-called", json=_token_called_payload())
        check(response.status_code == 401, f"missing token -> 401, got {response.status_code}")

        response = client.post(
            "/events/token-called", json=_token_called_payload(),
            headers={"X-Internal-Token": "definitely-wrong"},
        )
        check(response.status_code == 401, f"wrong token -> 401, got {response.status_code}")

        check(calls["lookup"] == 0, "auth must be checked BEFORE any appointment lookup runs -- 0 calls expected")
    finally:
        db.get_appointment_by_hms_id = original_get_appt


def test_token_called_accepts_the_real_internal_token():
    print("\n--- Sanity: the real token still works, for an appointment this bot doesn't know about ---")
    original_get_appt = db.get_appointment_by_hms_id

    async def _unknown(hms_appointment_id):
        return None

    db.get_appointment_by_hms_id = _unknown
    try:
        response = client.post(
            "/events/token-called", json=_token_called_payload(event_id="evt-2", appointment_id="unknown-appt"),
            headers={"X-Internal-Token": settings.internal_events_token},
        )
        check(response.status_code == 200, f"correct token -> 200, got {response.status_code}")
        check(response.json() == {"status": "ok"}, f"acks even for an unknown appointment, got {response.json()!r}")
    finally:
        db.get_appointment_by_hms_id = original_get_appt


def test_token_called_pushes_a_queue_update_for_a_known_appointment():
    print("\n--- First-ever coverage of the actual happy path: a known appointment gets notified ---")
    original_get_appt = db.get_appointment_by_hms_id
    original_save_status = db.save_queue_status
    original_send_text = hms_events_module.send_text
    sent = []

    async def _known(hms_appointment_id):
        return {"phone_number": "919876543210", "preferred_language": "en", "patient_display_name": "Riya"}

    async def _save_status(appointment_id, current_token, estimated_wait_minutes):
        pass

    async def _fake_send_text(client, to, body):
        sent.append((to, body))

    db.get_appointment_by_hms_id = _known
    db.save_queue_status = _save_status
    hms_events_module.send_text = _fake_send_text
    try:
        response = client.post(
            "/events/token-called",
            json=_token_called_payload(event_id="evt-3", appointment_id="appt-known", token=12),
            headers={"X-Internal-Token": settings.internal_events_token},
        )
        check(response.status_code == 200, f"Expected 200, got {response.status_code}")
        check(len(sent) == 1, f"sends exactly one queue-update text, got {sent!r}")
        check(sent[0][0] == "919876543210", "sent to the appointment's own phone number")
        check("12" in sent[0][1], f"names the current token in the message, got {sent[0][1]!r}")
    finally:
        db.get_appointment_by_hms_id = original_get_appt
        db.save_queue_status = original_save_status
        hms_events_module.send_text = original_send_text


if __name__ == "__main__":
    tests = [
        test_verify_webhook, test_verify_signature_validation,
        test_webhook_for_a_different_phone_number_id_is_ignored,
        test_webhook_for_this_servers_own_phone_number_id_still_works,
        test_webhook_with_no_phone_number_id_metadata_still_works,
        test_qr_redirects,
        test_token_called_rejects_a_missing_or_wrong_internal_token,
        test_token_called_accepts_the_real_internal_token,
        test_token_called_pushes_a_queue_update_for_a_known_appointment,
    ]
    for test in tests:
        test()
        print(f"  ran {test.__name__}")
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"PASSED — {len(tests)} checks, Front Door routes verified")
