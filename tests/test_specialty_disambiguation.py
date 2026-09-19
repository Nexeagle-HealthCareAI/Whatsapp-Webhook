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

from app import i18n, nlu_client  # noqa: E402
from app.conversation.specialty_browsing import resolve_specialty_category, _groups_with_live_categories  # noqa: E402
from app.decision_maker.symptom_matcher import match_category  # noqa: E402

failures = []

# 1HMS's prod /public/specialties, confirmed live -- short department-style names, not the
# richer patient-facing labels SPECIALTY_GROUPS/symptom_client are written against.
PROD_CATEGORIES = [
    "General Surgery", "Anesthesiology", "Critical Care", "Dentistry", "ENT",
    "Gynaecologist", "Gynecology", "Neurology", "Obstetrics", "Orthopedics",
    "Paediatrician", "Pediatrics", "Urology",
]


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


def test_stomach_pain_no_longer_coincidentally_matches_ent():
    """Live prod bug: 'Gastroenterologist' (the label routed for stomach-pain symptoms)
    contains the letters "ent" mid-word, and the old unrestricted substring check matched
    that against prod's "ENT" category -- a medically nonsensical result. The prefix-only
    fallback must not make this mistake."""
    result = match_category("Gastroenterologist", PROD_CATEGORIES)
    check(result != "ENT", f"'Gastroenterologist' must not coincidentally match 'ENT', got {result!r}")


def test_urologist_matches_prods_short_urology_category():
    """Live prod bug: 'Kidney main pain' resolves to the 'Urologist' label, but prod's
    actual category is the short 'Urology' -- neither an exact nor a prefix match, so this
    needs the curated CATEGORY_ALIASES table to resolve at all."""
    result = match_category("Urologist", PROD_CATEGORIES)
    check(result == "Urology", f"'Urologist' should resolve to prod's 'Urology' category via the alias table, got {result!r}")


def test_shorthand_prefix_matching_still_works_on_prod_style_names():
    """The alias table only covers known synonyms -- a real substring/prefix shorthand
    ('dent' for 'Dentistry') must still resolve via the prefix fallback."""
    result = match_category("dent", PROD_CATEGORIES)
    check(result == "Dentistry", f"prefix shorthand should still match, got {result!r}")


def test_browse_groups_surface_more_than_two_on_prod_style_categories():
    """Live prod bug: the 'Pick an area' list only ever showed 2 of 9 groups because
    _groups_with_live_categories did exact-string membership against prod's short names.
    With the alias table, groups like bones/eyes-ent-skin should now populate too."""
    specialties = [{"category": c, "displayName": c} for c in PROD_CATEGORIES]
    paired = _groups_with_live_categories(specialties)
    non_other_groups = [g for g, _members in paired if g["id"] != i18n.OTHER_GROUP["id"]]
    check(
        len(non_other_groups) > 2,
        f"expected more than 2 real groups to populate on prod's category list, got {len(non_other_groups)}: {[g['id'] for g in non_other_groups]}",
    )
    bones_group = next((g for g, members in paired if g["id"] == "grp_bones"), None)
    check(bones_group is not None, "grp_bones (Orthopedics -> Orthopaedic Surgeon (Bone)) should now populate on prod")


if __name__ == "__main__":
    test_correct_spelling_never_calls_the_ai()
    test_misspelling_falls_back_to_ai_and_gets_validated()
    test_ai_hallucination_is_rejected()
    test_ai_case_insensitive_match_still_validated()
    test_ai_none_response_means_no_match()
    test_disambiguate_specialty_returns_none_instead_of_raising_on_failure()
    test_stomach_pain_no_longer_coincidentally_matches_ent()
    test_urologist_matches_prods_short_urology_category()
    test_shorthand_prefix_matching_still_works_on_prod_style_names()
    test_browse_groups_surface_more_than_two_on_prod_style_categories()

    print("\n" + "=" * 50)
    if failures:
        print(f"SPECIALTY DISAMBIGUATION TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL SPECIALTY DISAMBIGUATION TESTS PASSED")
