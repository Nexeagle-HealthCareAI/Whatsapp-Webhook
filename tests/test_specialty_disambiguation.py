"""
tests/test_specialty_disambiguation.py
-----------------------------------------
Covers app.conversation.specialty_browsing.resolve_specialty_category and
app.listener.nlu_client.disambiguate_specialty -- the AI-assisted fallback for when a
patient misspells a specialty ("kardio", "cardeo" for "cardio") and the deterministic
exact/substring match (symptom_client.match_category) finds nothing.

Run directly: python3 tests/test_specialty_disambiguation.py
"""

import asyncio
import os
import sys
import types

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Stub aioodbc/redis before importing app.* -- same convention as every other test file.
_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object
async def _create_pool(*a, **k):
    raise NotImplementedError
_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")
class _StubRedis:
    @classmethod
    def from_url(cls, *a, **k):
        return cls()
_fake_redis_mod.asyncio.Redis = _StubRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

from app import nlu_client  # noqa: E402
from app.conversation.specialty_browsing import resolve_specialty_category  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


CATEGORIES = ["Cardiologist (Heart)", "Dentist", "Gynaecologist", "General Physician"]


def test_correct_spelling_never_calls_the_ai():
    """The common case (correct spelling, or a shorthand that's a real substring) must
    stay on the fast, free, deterministic path -- the AI call should never even be
    attempted, since the extra latency/cost is only meant to be paid on a genuine miss."""
    original = nlu_client.disambiguate_specialty
    called = []
    async def spy(*a, **k):
        called.append(True)
        return "should not be reached"
    nlu_client.disambiguate_specialty = spy
    try:
        result = asyncio.run(resolve_specialty_category(None, "cardio", CATEGORIES))
        check(result == "Cardiologist (Heart)", f"correct spelling still matches directly, got {result!r}")
        check(called == [], "the AI fallback must NOT be called when the deterministic match already succeeded")
    finally:
        nlu_client.disambiguate_specialty = original


def test_misspelling_falls_back_to_ai_and_gets_validated():
    """A genuine misspelling ('kardio') fails the deterministic match -- the AI fallback
    should be tried, and if it returns something that's actually in the real category
    list, that becomes the answer."""
    original = nlu_client.disambiguate_specialty
    async def mock_ai(client, query, categories):
        check(query == "kardio", f"the AI is asked about the exact misspelled text, got {query!r}")
        check(categories == CATEGORIES, "the AI is given the exact real category list, not a guess at one")
        return "Cardiologist (Heart)"
    nlu_client.disambiguate_specialty = mock_ai
    try:
        result = asyncio.run(resolve_specialty_category(None, "kardio", CATEGORIES))
        check(result == "Cardiologist (Heart)", f"a misspelling should resolve via the AI fallback, got {result!r}")
    finally:
        nlu_client.disambiguate_specialty = original


def test_ai_hallucination_is_rejected():
    """If the AI returns something that ISN'T actually one of the real categories
    (a hallucination, or it just got it wrong), the caller must reject it rather than
    trust it blindly -- this is the re-validation that makes the whole design safe."""
    original = nlu_client.disambiguate_specialty
    async def hallucinating_ai(client, query, categories):
        return "Neurosurgeon (made up, not in the real list)"
    nlu_client.disambiguate_specialty = hallucinating_ai
    try:
        result = asyncio.run(resolve_specialty_category(None, "kardio", CATEGORIES))
        check(result is None, f"an AI answer not in the real category list must be rejected, got {result!r}")
    finally:
        nlu_client.disambiguate_specialty = original


def test_ai_case_insensitive_match_still_validated():
    """The AI's answer is matched case-insensitively against the real list (LLMs don't
    reliably preserve exact casing), but the RETURNED value is still the real list's own
    casing, never the AI's -- so downstream code always sees a known-good string."""
    original = nlu_client.disambiguate_specialty
    async def mock_ai(client, query, categories):
        return "cardiologist (heart)"  # lowercase, unlike the real "Cardiologist (Heart)"
    nlu_client.disambiguate_specialty = mock_ai
    try:
        result = asyncio.run(resolve_specialty_category(None, "kardio", CATEGORIES))
        check(result == "Cardiologist (Heart)", f"should match case-insensitively but return the real list's casing, got {result!r}")
    finally:
        nlu_client.disambiguate_specialty = original


def test_ai_none_response_means_no_match():
    """disambiguate_specialty itself returns None when the model says 'none' -- confirm
    the caller treats that the same as any other non-match, not as an error."""
    original = nlu_client.disambiguate_specialty
    async def mock_ai(client, query, categories):
        return None
    nlu_client.disambiguate_specialty = mock_ai
    try:
        result = asyncio.run(resolve_specialty_category(None, "completely unrelated text", CATEGORIES))
        check(result is None, f"no AI match should mean no result at all, got {result!r}")
    finally:
        nlu_client.disambiguate_specialty = original


def test_disambiguate_specialty_returns_none_instead_of_raising_on_failure():
    """Sanity check on the real (unmocked) function with a deliberately broken client
    (None): whether that fails because no Sarvam key is configured, or because the
    request itself blows up, the function must return None, never raise -- same
    hard-fallback posture as every other Sarvam-calling function in this codebase.
    (This repo's own .env may have a real SARVAM_API_KEY set locally, same reason
    test_hospital_search.py is flaky elsewhere -- either way, this must not raise.)"""
    result = asyncio.run(nlu_client.disambiguate_specialty(None, "kardio", CATEGORIES))
    check(result is None, f"a broken client (or no key) should return None, not raise or guess, got {result!r}")


if __name__ == "__main__":
    test_correct_spelling_never_calls_the_ai()
    test_misspelling_falls_back_to_ai_and_gets_validated()
    test_ai_hallucination_is_rejected()
    test_ai_case_insensitive_match_still_validated()
    test_ai_none_response_means_no_match()
    test_disambiguate_specialty_returns_none_instead_of_raising_on_failure()

    print("\n" + "=" * 50)
    if failures:
        print(f"SPECIALTY DISAMBIGUATION TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL SPECIALTY DISAMBIGUATION TESTS PASSED")
