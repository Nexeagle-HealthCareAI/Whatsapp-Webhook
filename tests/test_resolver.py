"""
Sanity checks for app/decision_maker/resolver.py — pure decision logic (0/1/N
cardinality), no real network or DB. resolver.py itself has zero I/O dependency (it
imports match_typed_city/nearest_city from app.decision_maker.city_resolver, a sibling
pure module, not from the Messenger-layer app.messengers.city_index) -- these stubs are
kept as a defensive belt-and-suspenders in case that ever changes, matching
test_specialty_groups.py's convention. Run directly: python3 tests/test_resolver.py
"""

import os
import sys
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object
sys.modules.setdefault("aioodbc", _fake_odbc)

_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")


class _MockRedis:
    @classmethod
    def from_url(cls, *args, **kwargs):
        return cls()


_fake_redis_mod.asyncio.Redis = _MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app.decision_maker.resolver import resolve_doctor, resolve_location_from_gps, resolve_location_from_text  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


DOCTORS = [
    {"doctorId": "1", "fullName": "Dr. Amit Sharma", "city": "Kishanganj", "latitude": 26.10, "longitude": 87.95},
    {"doctorId": "2", "fullName": "Dr. Priya Sharma", "city": "Patna", "latitude": 25.61, "longitude": 85.14},
    {"doctorId": "3", "fullName": "Dr. Rajesh Verma", "city": "Kishanganj", "latitude": 26.11, "longitude": 87.94},
    {"doctorId": "4", "fullName": "Dr. Manoj Kumar", "city": "Kishanganj", "latitude": 26.10, "longitude": 87.95},
]


def test_doctor_resolution():
    # Multiple doctors share a surname -> many, nationwide
    r = resolve_doctor("Sharma", DOCTORS)
    check(r.status == "many", "two Sharmas nationwide -> many")
    check(len(r.candidates) == 2, "both Sharmas present as candidates")

    # Narrowing by city collapses it to one
    r = resolve_doctor("Sharma", DOCTORS, city="Kishanganj")
    check(r.status == "one", "Sharma + city=Kishanganj -> one")
    check(r.value["doctorId"] == "1", "resolves to the Kishanganj Sharma")

    # Narrowing by GPS behaves the same as narrowing by city
    r = resolve_doctor("Sharma", DOCTORS, patient_lat=26.10, patient_lng=87.95)
    check(r.status == "one", "Sharma + nearby GPS -> one")
    check(r.value["doctorId"] == "1", "GPS narrows to the Kishanganj Sharma")

    # A location that doesn't match anyone in the pool falls back rather than
    # hiding a genuinely good name match
    r = resolve_doctor("Sharma", DOCTORS, city="Ranchi")
    check(r.status == "many", "unmatched city -> falls back to unnarrowed match, not zero")

    # A unique name resolves immediately, no location needed
    r = resolve_doctor("Manoj", DOCTORS)
    check(r.status == "one", "unique name -> one, even with no location narrowing")
    check(r.value["doctorId"] == "4", "resolves to Manoj Kumar")

    # No match at all
    r = resolve_doctor("Xyzzy", DOCTORS)
    check(r.status == "zero", "unknown name -> zero")
    check(r.candidates == [], "zero status carries no candidates")


def test_location_resolution():
    index = {"Kishanganj": [[26.10, 87.95]], "Patna": [[25.61, 85.14]]}

    # GPS is single-valued by construction
    r = resolve_location_from_gps(index, 26.10, 87.95)
    check(r.status == "one", "GPS resolves to exactly one city")
    check(r.value["city"] == "Kishanganj", "GPS resolves to the nearest city")

    # Typed text: today's implementation can only ever answer zero or one — the
    # upcoming location-match API is what adds a real "many" here (see
    # resolve_location_from_text's docstring in app/resolver.py).
    r = resolve_location_from_text(index, "kishanganj")
    check(r.status == "one", "exact typed city name -> one")
    r = resolve_location_from_text(index, "nowhereville")
    check(r.status == "zero", "unknown typed city -> zero")


if __name__ == "__main__":
    test_doctor_resolution()
    test_location_resolution()

    print("\n" + "=" * 50)
    if failures:
        print(f"FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL resolver checks PASSED")
