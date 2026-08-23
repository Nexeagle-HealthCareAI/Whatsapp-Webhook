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

from app import main
from app.config import settings
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


if __name__ == "__main__":
    tests = [test_verify_webhook, test_verify_signature_validation, test_qr_redirects]
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
