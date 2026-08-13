"""
test_specialty_groups.py
Credential-free checks for the two-level specialty browse and the WhatsApp size caps.
Run with:  python test_specialty_groups.py

Why this file exists: WhatsApp does NOT error when a message exceeds its limits — it
silently truncates. That's how 20 of 30 specialties became unreachable in the first place
(a 30-row list quietly became a 10-row one), and how "Family member ke liye" rendered as
"Family member ke liy". Nothing surfaces these but an explicit check, so here it is.

No DB, no network, no tokens — aioodbc is stubbed and the specialty list is a frozen copy
of a real GET /public/specialties response, so this runs anywhere.
"""

import os
import sys
import types

# Stub aioodbc before importing app.* — the native ODBC driver isn't needed for these checks
# and isn't present outside the Docker image.
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
    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True
    async def delete(self, key):
        self.data.pop(key, None)
        return True

_mock_redis_instance = MockRedis()
_fake_redis_mod.asyncio.Redis = MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

from datetime import date, timedelta  # noqa: E402

from app import city_index, conversation, i18n, nlu_client  # noqa: E402
from app.config import settings  # noqa: E402
from app.whatsapp_client import (  # noqa: E402
    _MAX_BUTTON_TITLE,
    _MAX_LIST_ROWS,
    _MAX_ROW_DESC,
    _MAX_ROW_TITLE,
)

LANGS = ("en", "hi", "hg", "bn")

# Frozen copy of a real GET /public/specialties response (1hms-dev-api.nexeagle.com).
# If 1HMS's category strings ever change, these tests fail loudly — which is the point:
# a renamed category would otherwise silently fall into the "Other" bucket.
LIVE_SPECIALTIES = [
    {"category": c, "displayName": d}
    for c, d in [
        ("Anaesthesiologist", "Anaesthetist"),
        ("Cardiologist (Heart)", "Cardiologist / Heart Specialist"),
        ("Cardiothoracic Surgeon", "Cardiothoracic Surgeon (CTVS)"),
        ("Dermatologist (Skin)", "Dermatologist / Skin Doctor"),
        ("Emergency Medicine Specialist", "Emergency Physician"),
        ("Endocrinologist (Hormones/Diabetes)", "Endocrinologist / Hormone Specialist"),
        ("ENT Specialist", "ENT Specialist"),
        ("Gastroenterologist", "Gastroenterologist"),
        ("General Physician", "Family Physician / General Physician"),
        ("General Surgeon", "General Surgeon"),
        ("Geriatrician", "Geriatrician"),
        ("GI/Surgical Gastroenterologist", "GI Surgeon"),
        ("Gynaecologist", "Gynaecologist / Obstetrician"),
        ("Nephrologist (Kidney)", "Nephrologist / Kidney Specialist"),
        ("Neurologist", "Neurologist"),
        ("Neurosurgeon", "Neurosurgeon"),
        ("Oncologist (Cancer)", "Surgical Oncologist"),
        ("Ophthalmologist (Eye)", "Eye Specialist / Ophthalmologist"),
        ("Orthopaedic Surgeon (Bone)", "Hand Surgeon"),
        ("Paediatrician", "Paediatrician / Child Specialist"),
        ("Pathologist", "Pathologist"),
        ("Physiotherapist / Rehab", "Physiatrist / Rehab Medicine Specialist"),
        ("Plastic Surgeon", "Plastic Surgeon"),
        ("Psychiatrist", "Psychiatrist"),
        ("Pulmonologist (Chest/Lungs)", "Pulmonologist / Chest Specialist"),
        ("Radiologist", "Radiologist"),
        ("Rheumatologist", "Rheumatologist"),
        ("Sports Medicine Specialist", "Sports Medicine Specialist"),
        ("Urologist", "Urologist"),
        ("Vascular Surgeon", "Vascular Surgeon"),
    ]
]

# Keys rendered as reply-button titles (cap 20) vs list action buttons (also 20)
# vs list row titles (cap 24). Kept explicit rather than inferred, so adding a string
# to the wrong bucket here is a deliberate act rather than an accident.
BUTTON_TITLE_KEYS = [
    "search_mode_symptom", "search_mode_browse",
    "date_today", "date_tomorrow", "confirm_btn", "cancel_btn", "search_wider_yes",
    "change_doctor_btn", "update_details_btn",
]
LIST_ACTION_KEYS = [
    "specialty_group_button", "specialty_list_button", "sort_button", "doctor_list_button",
]
ROW_TITLE_KEYS = ["sort_rating", "sort_nearest", "sort_experience", "sort_fee"]

failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def test_group_list_fits_one_message():
    paired = conversation._groups_with_live_categories(LIVE_SPECIALTIES)
    check(
        len(paired) <= _MAX_LIST_ROWS,
        f"group list has {len(paired)} rows, exceeds WhatsApp's {_MAX_LIST_ROWS} — "
        "groups would be silently dropped",
    )
    for group, members in paired:
        check(
            len(members) <= _MAX_LIST_ROWS,
            f"group {group['id']} has {len(members)} specialities, exceeds {_MAX_LIST_ROWS}",
        )


def test_every_live_category_is_reachable():
    paired = conversation._groups_with_live_categories(LIVE_SPECIALTIES)
    reachable = {s["category"] for _, members in paired for s in members}
    missing = {s["category"] for s in LIVE_SPECIALTIES} - reachable
    check(not missing, f"unreachable specialities (patient can never book these): {sorted(missing)}")


def test_no_category_in_two_groups():
    paired = conversation._groups_with_live_categories(LIVE_SPECIALTIES)
    seen = [s["category"] for _, members in paired for s in members]
    dupes = sorted({c for c in seen if seen.count(c) > 1})
    check(not dupes, f"specialities appearing in more than one group: {dupes}")


def test_unknown_category_falls_into_other():
    """A specialty 1HMS adds later must still reach patients, not vanish."""
    future = LIVE_SPECIALTIES + [{"category": "Dentist", "displayName": "Dental Surgeon"}]
    paired = conversation._groups_with_live_categories(future)
    other = [members for group, members in paired if group["id"] == "grp_other"]
    check(
        other and any(s["category"] == "Dentist" for s in other[0]),
        "a new/unmapped specialty did not land in the Other bucket",
    )


def test_empty_groups_are_hidden():
    """Only two categories live -> only the groups containing them should be offered."""
    sparse = [
        {"category": "Gynaecologist", "displayName": "Gynaecologist / Obstetrician"},
        {"category": "General Physician", "displayName": "Family Physician"},
    ]
    paired = conversation._groups_with_live_categories(sparse)
    ids = {group["id"] for group, _ in paired}
    check(
        ids == {"grp_general", "grp_women_children"},
        f"expected only the two non-empty groups, got {sorted(ids)}",
    )


def test_strings_fit_whatsapp_caps():
    for key in BUTTON_TITLE_KEYS + LIST_ACTION_KEYS:
        for lang in LANGS:
            text = i18n.t(key, lang)
            check(
                len(text) <= _MAX_BUTTON_TITLE,
                f"{key}[{lang}] is {len(text)} chars, over the {_MAX_BUTTON_TITLE} button cap: {text!r}",
            )
    for key in ROW_TITLE_KEYS:
        for lang in LANGS:
            text = i18n.t(key, lang)
            check(
                len(text) <= _MAX_ROW_TITLE,
                f"{key}[{lang}] is {len(text)} chars, over the {_MAX_ROW_TITLE} row cap: {text!r}",
            )


def test_group_labels_fit_whatsapp_caps():
    for group in i18n.SPECIALTY_GROUPS + [i18n.OTHER_GROUP]:
        for lang in LANGS:
            title, desc = i18n.group_label(group, lang)
            check(
                len(title) <= _MAX_ROW_TITLE,
                f"{group['id']}.title[{lang}] is {len(title)} chars, over {_MAX_ROW_TITLE}: {title!r}",
            )
            check(
                len(desc) <= _MAX_ROW_DESC,
                f"{group['id']}.desc[{lang}] is {len(desc)} chars, over {_MAX_ROW_DESC}: {desc!r}",
            )


def test_specialty_row_titles_fit():
    for specialty in LIVE_SPECIALTIES:
        _, title, _ = conversation._specialty_row(specialty)
        check(
            len(title) <= _MAX_ROW_TITLE,
            f"specialty row title {title!r} is {len(title)} chars, over {_MAX_ROW_TITLE}",
        )


# --- city index -------------------------------------------------------------------------
# Shaped exactly like live /public/doctors records, and deliberately including the real dirt
# observed in the dev dataset: "Kishanganj" carries three coordinate pairs, one of which is a
# Delhi-area point (28.7, 77.3) filed under the Bihar town's name; one record has null
# coordinates; one has a junk city value. These are the cases that break a naive
# city-to-centre mapping, so they belong in the fixture rather than a clean invented one.
DIRTY_DOCTORS = [
    {"city": "Kishanganj", "latitude": 26.1035, "longitude": 87.9477},
    {"city": "Kishanganj", "latitude": 28.7, "longitude": 77.3},
    {"city": "Kishanganj", "latitude": 25.77, "longitude": 87.49},
    {"city": "Purnea", "latitude": 25.7772, "longitude": 87.4743},
    {"city": "Purnea", "latitude": None, "longitude": None},
    {"city": "Bengaluru", "latitude": 12.971599, "longitude": 77.594566},
    {"city": "Chennai", "latitude": 13.08268, "longitude": 80.270721},
    {"city": "Delhi NCR", "latitude": 28.613939, "longitude": 77.209023},
    {"city": "Kolkata", "latitude": 22.572646, "longitude": 88.363895},
    {"city": "kne", "latitude": None, "longitude": None},
]


def test_index_skips_unusable_records():
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    check("kne" not in index, "junk city with null coordinates should not enter the index")
    check(len(index.get("Purnea", [])) == 1, "null-coordinate record should be dropped")
    check(
        len(index.get("Kishanganj", [])) == 3,
        "all distinct coordinate clusters must be kept — collapsing them is what breaks "
        "resolution for cities with inconsistent data",
    )


def test_gps_in_kishanganj_resolves_to_kishanganj():
    """The case the whole nearest-cluster design exists for. A centroid of Kishanganj's
    three clusters lands ~376km away and would resolve a patient standing in Kishanganj to
    Purnea instead."""
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    city, km = city_index.nearest_city(index, 26.10, 87.93)
    check(city == "Kishanganj", f"GPS in Kishanganj resolved to {city!r} instead of 'Kishanganj'")
    check(km < 10, f"resolved distance {km:.1f}km is implausibly large for a same-town match")


def test_other_cities_resolve():
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    for lat, lng, expected in [
        (25.78, 87.47, "Purnea"),
        (22.57, 88.36, "Kolkata"),
        (28.61, 77.21, "Delhi NCR"),
        (12.97, 77.59, "Bengaluru"),
    ]:
        city, _ = city_index.nearest_city(index, lat, lng)
        check(city == expected, f"({lat}, {lng}) resolved to {city!r}, expected {expected!r}")


def test_typed_city_matching():
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    for typed, expected in [
        ("Kishanganj", "Kishanganj"),
        ("kishanganj", "Kishanganj"),      # API is case-insensitive, but only on exact names
        ("  kishanganj  ", "Kishanganj"),
        ("Kishanganj Bihar", "Kishanganj"),  # "<city> <state>" phrasing
        ("near purnea", "Purnea"),
        ("Delhi NCR", "Delhi NCR"),
        ("Delhi", "Delhi NCR"),            # 1HMS files it as "Delhi NCR"; API rejects "Delhi"
        ("Kishan", "Kishanganj"),          # partial — API rejects it, we resolve it
        ("Patna", None),                   # not served
        ("", None),
        ("xy", None),                      # under the 4-char floor
    ]:
        got = city_index.match_typed_city(index, typed)
        check(got == expected, f"match_typed_city({typed!r}) returned {got!r}, expected {expected!r}")


def test_empty_index_resolves_to_nothing():
    """If the index can't be built the caller must fall back to an unfiltered search
    rather than pass a bogus city and show an empty doctor list."""
    check(city_index.nearest_city({}, 26.1, 87.9) == (None, float("inf")), "empty index should resolve to None")
    check(city_index.match_typed_city({}, "Kishanganj") is None, "empty index should match nothing")


def test_cities_within_radius():
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    # Patient standing in Kishanganj. Purnea is ~58km away; Kolkata and the rest are far.
    within_10 = dict(city_index.cities_within(index, 26.10, 87.93, 10, 8))
    within_75 = dict(city_index.cities_within(index, 26.10, 87.93, 75, 8))
    check(set(within_10) == {"Kishanganj"}, f"10km band should hold only Kishanganj, got {set(within_10)}")
    check(
        set(within_75) == {"Kishanganj", "Purnea"},
        f"75km band should add Purnea, got {set(within_75)}",
    )
    check(
        list(within_75) == sorted(within_75, key=within_75.get),
        "cities must come back nearest-first",
    )


def test_kishanganj_qualifies_on_its_good_cluster():
    """Kishanganj carries a Delhi-coordinate cluster as well as its real one. The town must
    still qualify for the 10km band on its good cluster — excluding the whole town because
    some of its records are mislabelled would hide the genuinely local doctors."""
    index = city_index.build_from_doctors(DIRTY_DOCTORS)
    within_10 = dict(city_index.cities_within(index, 26.10, 87.93, 10, 8))
    check("Kishanganj" in within_10, "Kishanganj should qualify via its correct Bihar cluster")
    check(
        within_10.get("Kishanganj", 999) < 5,
        f"distance should come from the good cluster, got {within_10.get('Kishanganj')}km",
    )


def test_radius_bands_are_ordered_and_capped():
    radii = settings.doctor_search_radii_km
    check(radii == sorted(radii), f"radius bands must widen, got {radii}")
    check(len(radii) >= 1, "at least one radius band is required")
    check(
        all(r > 0 for r in radii),
        "radius bands must be positive — a zero band would match only exact coordinates",
    )


def test_mislabelled_doctor_falls_outside_every_band():
    """The Delhi-coordinate record labelled 'Kishanganj' must sit outside the widest band,
    so it's excluded by ordinary distance rather than by any special-case rule."""
    from app.geo import haversine_km

    distance = haversine_km(26.10, 87.93, 28.7, 77.3)
    widest = settings.doctor_search_radii_km[-1]
    check(
        distance > widest,
        f"mislabelled record is {distance:.0f}km away but the widest band is {widest}km — "
        "it would be shown to the patient as local",
    )


# --- appointment day / shift timing -------------------------------------------------------
# Shift times copied verbatim from a live availability response.
LIVE_SHIFTS = {
    "success": True,
    "isAvailable": True,
    "shifts": [
        {"name": "Morning", "startTime": "09:00:00", "endTime": "12:00:00"},
        {"name": "Afternoon", "startTime": "13:00:00", "endTime": "17:00:00"},
        {"name": "Evening", "startTime": "17:30:00", "endTime": "20:30:00"},
    ],
}


def _shifts_at(clock: str, on_date, target_date):
    """Runs _usable_shifts with the clinic clock pinned to `clock` on `on_date`."""
    import datetime as _dt

    real_now = conversation._clinic_now
    fixed = _dt.datetime.combine(on_date, _dt.time.fromisoformat(clock))
    conversation._clinic_now = lambda: fixed
    try:
        return conversation._usable_shifts(LIVE_SHIFTS, target_date)
    finally:
        conversation._clinic_now = real_now


def test_past_shifts_are_hidden_today():
    """The availability endpoint returns a standing schedule and has no idea what time it
    is — at 19:39 it still lists Morning (09:00-12:00). Offering that books a patient into a
    slot that finished hours ago."""
    import datetime as _dt

    today = _dt.date(2026, 7, 31)
    check(
        _shifts_at("08:00:00", today, today) == ["Morning", "Afternoon", "Evening"],
        "early morning should offer every shift",
    )
    check(
        _shifts_at("13:30:00", today, today) == ["Afternoon", "Evening"],
        "after midday, Morning must drop off",
    )
    check(
        _shifts_at("19:39:00", today, today) == ["Evening"],
        "at 19:39 only Evening (ends 20:30) is still reachable",
    )
    check(_shifts_at("21:00:00", today, today) == [], "after 20:30 nothing today is bookable")


def test_future_dates_keep_all_shifts():
    """Only today is filtered against the clock — tomorrow's morning is still valid at 11pm."""
    import datetime as _dt

    today = _dt.date(2026, 7, 31)
    tomorrow = today + _dt.timedelta(days=1)
    check(
        _shifts_at("23:00:00", today, tomorrow) == ["Morning", "Afternoon", "Evening"],
        "tomorrow's shifts must not be filtered by today's clock",
    )


def test_missing_shift_times_do_not_hide_the_shift():
    """If the API omits endTime, show the shift rather than silently dropping it — a shift
    shown in error is recoverable, one hidden in error is invisible."""
    import datetime as _dt

    real_now = conversation._clinic_now
    conversation._clinic_now = lambda: _dt.datetime(2026, 7, 31, 23, 0)
    try:
        shifts = conversation._usable_shifts(
            {"shifts": [{"name": "Morning"}, {"name": "Evening", "endTime": "not-a-time"}]},
            _dt.date(2026, 7, 31),
        )
    finally:
        conversation._clinic_now = real_now
    check(shifts == ["Morning", "Evening"], f"unparseable/missing endTime should keep the shift, got {shifts}")


def test_clinic_timezone_is_ahead_of_utc():
    """The container has no TZ set, so date.today() there is the UTC date. Between midnight
    and 05:30 IST that is still yesterday — a patient tapping 'Today' at 1am would be booked
    into a date that had already passed."""
    import datetime as _dt
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(settings.clinic_timezone)
    # 00:30 IST on 1 Aug is 19:00 UTC on 31 Jul — different calendar days.
    ist_early = _dt.datetime(2026, 8, 1, 0, 30, tzinfo=tz)
    check(
        ist_early.astimezone(_dt.timezone.utc).date() < ist_early.date(),
        "expected the UTC date to lag the clinic date in the early hours",
    )
    check(
        conversation._clinic_now().tzinfo is not None,
        "_clinic_now must be timezone-aware, or the comparison against shift times is meaningless",
    )


# --- patient details & shift validation ---------------------------------------------------


def test_details_parsing():
    check(conversation._parse_details("Aquib, 32", 2) == ["Aquib", "32"], "self details should parse")
    check(
        conversation._parse_details("Riya, 8, Daughter", 3) == ["Riya", "8", "Daughter"],
        "family details should parse",
    )
    check(conversation._parse_details("  Riya , 8 , Beti ", 3) == ["Riya", "8", "Beti"], "should trim")
    check(conversation._parse_details("32", 2) is None, "a lone age is not enough")
    check(conversation._parse_details("Aquib, 32", 3) is None, "missing relation should be rejected")
    check(conversation._parse_details("Aquib, , 32", 3) is None, "empty middle part should be rejected")
    check(
        conversation._parse_details("Riya, 8, Female, Rajesh", 4) == ["Riya", "8", "Female", "Rajesh"],
        "4-part details should parse",
    )
    check(conversation._parse_details("Riya, 8, Female", 4) is None, "missing guardian should be rejected")


def test_patient_line_format():
    context = {
        "patient_display_name": "Riya",
        "patient_age": "8",
        "patient_gender": "Female",
        "patient_guardian": "Rajesh",
    }
    line = conversation._patient_line(context, "en")
    check(line == "Riya, 8, Female (Guardian: Rajesh)", f"expected formatted patient line, got {line!r}")


def test_age_sanity_check():
    for good in ["32", "8", "8 yrs", "8 saal", "120", " 45 "]:
        check(conversation._looks_like_age(good), f"{good!r} should be accepted as an age")
    for bad in ["", "abc", "0", "250", "9876543210", "Daughter"]:
        check(not conversation._looks_like_age(bad), f"{bad!r} should be rejected as an age")


def test_shift_choice_is_limited_to_what_was_offered():
    """The clock filter hides shifts that have finished, but the patient can also type
    instead of tapping. Without validating against the offered list, typing 'morning' at
    7pm booked a slot that ended hours earlier — the exact thing the filter prevents."""
    offered = ["Evening"]
    lowered = [name.lower() for name in offered]
    check(
        conversation._match_choice("text", "morning", lowered) is None,
        "a shift that was not offered must be refused, however it is entered",
    )
    check(
        conversation._match_choice("text", "kuch bhi likh diya", lowered) is None,
        "free text must not become a shift label",
    )
    check(
        conversation._match_choice("text", "Evening", lowered) == "evening",
        "an offered shift should still be accepted when typed",
    )
    check(
        conversation._match_choice("button_reply", "evening", lowered) == "evening",
        "tapping the button must keep working",
    )


def test_time_of_day_normalization():
    """"kal subah" must not lose "subah" — normalize_time_of_day maps the shift qualifier
    onto the same canonical names (Morning/Afternoon/Evening) _get_offered_slots already
    uses, so it can be matched against a real offered slot without a second translation."""
    check(nlu_client.normalize_time_of_day("subah") == "Morning", "subah -> Morning")
    check(nlu_client.normalize_time_of_day("kal subah") == "Morning", "fused 'kal subah' still recovers Morning")
    check(nlu_client.normalize_time_of_day("evening") == "Evening", "evening -> Evening")
    check(nlu_client.normalize_time_of_day("dopahar") == "Afternoon", "dopahar -> Afternoon")
    check(nlu_client.normalize_time_of_day("raat") == "Evening", "raat -> Evening")
    check(nlu_client.normalize_time_of_day("purple") is None, "unrecognized text maps to nothing")
    check(nlu_client.normalize_time_of_day("") is None, "empty text maps to nothing")


def test_pick_matching_slot_auto_selects_unambiguous_shift():
    """The auto-select path in _send_slot_options: a patient who already said "kal subah"
    should skip straight past the button prompt when exactly one offered slot matches —
    but never guess when it's ambiguous."""
    today = date(2026, 8, 9)
    tomorrow = today + timedelta(days=1)
    slots = [
        {"date": today, "is_today": True, "shift_name": "Evening", "button_id": "slot_today_evening", "label": "Evening (Today)"},
        {"date": tomorrow, "is_today": False, "shift_name": "Morning", "button_id": "slot_tomorrow_morning", "label": "Morning (Tomorrow)"},
    ]

    check(conversation._pick_matching_slot(slots, None, None) is None, "no time_of_day hint -> no auto-match")

    matched = conversation._pick_matching_slot(slots, None, "Morning")
    check(matched is not None and matched["button_id"] == "slot_tomorrow_morning", "unique shift -> auto-matches")

    matched = conversation._pick_matching_slot(slots, tomorrow.isoformat(), "Morning")
    check(matched is not None and matched["button_id"] == "slot_tomorrow_morning", "shift + matching date -> auto-matches")

    check(conversation._pick_matching_slot(slots, today.isoformat(), "Morning") is None, "shift exists but not on that date -> no match")

    ambiguous_slots = slots + [
        {"date": today, "is_today": True, "shift_name": "Morning", "button_id": "slot_today_morning", "label": "Morning (Today)"},
    ]
    check(conversation._pick_matching_slot(ambiguous_slots, None, "Morning") is None, "shift offered on two dates -> ambiguous, no guess")


def test_finalize_slot_selection_always_stores_a_date_string():
    """context is serialised straight to JSON (db.save_conversation_state) — a real date
    object surviving into it would crash json.dumps on the very next save. The auto-match
    path hands _finalize_slot_selection a slot straight from _get_offered_slots (a real
    date object); the manual-tap path hands it one already deserialised from context_json
    (already a string). Both must come out as a string."""
    import asyncio
    today = date(2026, 8, 9)
    sent = {}

    class MockDB:
        async def save_conversation_state(self, phone, step, context):
            sent["context"] = context
        async def get_conversation_state(self, phone):
            return None
        async def clear_conversation_state(self, phone):
            pass

    async def mock_send_patient_details_flow(client, phone, context):
        sent["context"] = context

    original = conversation._send_patient_details_flow
    conversation._send_patient_details_flow = mock_send_patient_details_flow
    original_db = conversation.db
    conversation.db = MockDB()
    
    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_location = conversation.whatsapp_client.send_location_request
    original_send_list = conversation.whatsapp_client.send_list
    
    async def mock_nop(*args, **kwargs):
        pass
    conversation.whatsapp_client.send_text = mock_nop
    conversation.whatsapp_client.send_buttons = mock_nop
    conversation.whatsapp_client.send_location_request = mock_nop
    conversation.whatsapp_client.send_list = mock_nop
    
    mock_client = object()
    try:
        slot_with_real_date_object = {"date": today, "is_today": True, "shift_name": "Evening"}
        asyncio.run(conversation._finalize_slot_selection(mock_client, "123", {"lang": "en"}, slot_with_real_date_object))
        check(isinstance(sent["context"]["preferred_date"], str), "a real date object must be normalised to a string")
        check(sent["context"]["preferred_date"] == today.isoformat(), "normalised string matches the selected date")

        slot_with_string_date = {"date": today.isoformat(), "is_today": True, "shift_name": "Evening"}
        asyncio.run(conversation._finalize_slot_selection(mock_client, "123", {"lang": "en"}, slot_with_string_date))
        check(isinstance(sent["context"]["preferred_date"], str), "an already-string date is left as a string")
    finally:
        conversation._send_patient_details_flow = original
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_location_request = original_send_location
        conversation.whatsapp_client.send_list = original_send_list


def test_confirm_shows_clinic_and_distance():
    """The search reaches up to 75km, so the doctor may be in another town. That has to be
    visible before confirming, not only afterwards when the map pin arrives."""
    context = {
        "hospital_name": "Purnea General Hospital",
        "hospital_city": "Purnea",
        "hospital_lat": 25.7772,
        "hospital_lng": 87.4743,
        "patient_lat": 26.10,
        "patient_lng": 87.93,
    }
    line = conversation._clinic_line(context, "en")
    check("Purnea General Hospital" in line, f"hospital name missing from {line!r}")
    check("Purnea" in line, f"city missing from {line!r}")
    check("km" in line, f"distance missing from {line!r}")


def test_confirm_clinic_line_survives_missing_data():
    check(
        conversation._clinic_line({}, "en") == i18n.t("clinic_unknown", "en"),
        "an empty clinic should fall back to a label, not render as an empty line",
    )
    no_coords = conversation._clinic_line({"hospital_name": "X", "hospital_city": "Y"}, "en")
    check("km" not in no_coords, f"distance must be omitted when coordinates are unknown, got {no_coords!r}")


def test_every_string_has_all_three_languages():
    for key, variants in i18n._STRINGS.items():
        for lang in LANGS:
            check(variants.get(lang), f"string {key!r} is missing a {lang} translation")


def test_welcome_banner_languages():
    variants = i18n._STRINGS.get("welcome_banner")
    check(variants is not None, "welcome_banner key must exist")
    
    en_val = variants.get("en")
    check("Welcome! You can type" in en_val, "en welcome_banner must contain English text")
    check("स्वागत है!" not in en_val, "en welcome_banner must NOT contain Hindi text")

    hi_val = variants.get("hi")
    check("स्वागत है! आप किसी भी समय" in hi_val, "hi welcome_banner must contain Hindi text")
    check("Welcome!" not in hi_val, "hi welcome_banner must NOT contain English text")

    hg_val = variants.get("hg")
    check("Welcome! Aap kabhi bhi" in hg_val, "hg welcome_banner must contain Hinglish text")
    check("स्वागत है!" not in hg_val, "hg welcome_banner must NOT contain Hindi text")

    bn_val = variants.get("bn")
    check("স্বাগতম!" in bn_val, "bn welcome_banner must contain Bengali text")
    check("Welcome!" not in bn_val, "bn welcome_banner must NOT contain English text")


def test_language_detection():
    detect = conversation._detect_language

    # 1. Generic greetings (open option / None)
    check(detect("hi") == (None, False), "single word hi should be open option")
    check(detect("hello") == (None, False), "single word hello should be open option")
    check(detect("hey") == (None, False), "single word hey should be open option")
    check(detect("  hey  ") == (None, False), "whitespace-padded hey should be open option")
    check(detect("Hlo") == (None, False), "Capitalized hlo should be open option")

    # 2. English (en) with spelling variations / typos
    check(detect("hey i have to book an appointment") == ("en", False), "standard english appointment booking should be en")
    check(detect("need to bok an apointmint") == ("en", False), "english with typos bok/apointmint should be en")
    check(detect("dr appontment") == ("en", False), "english with typos dr/appontment should be en")
    check(detect("download prescribtion") == ("en", False), "prescription download english should be en")
    check(detect("get medicine list") == ("en", False), "medicine list english should be en")

    # 3. Hinglish (hg) with spelling variations / typos
    check(detect("hi mujhe appointment book krna hai") == ("hg", False), "standard hinglish appointment booking should be hg")
    check(detect("muje appontment buk krna h") == ("hg", False), "hinglish with typos muje/buk/h should be hg")
    check(detect("apointment book krna he") == ("hg", False), "hinglish with typos apointment/he should be hg")
    check(detect("mje dr dikhao") == ("hg", False), "hinglish with typo mje and dikhao should be hg")
    check(detect("parcha download krna h") == ("hg", False), "prescription download in hinglish should be hg")
    check(detect("preskripsion downlod krna hai") == ("hg", False), "prescription download with typos in hinglish should be hg")

    # 4. Hindi Devanagari (hi) with spelling variations / typos (High confidence)
    check(detect("मुझे अपॉइंटमेंट बुक करना है") == ("hi", True), "hindi devanagari should be hi")
    check(detect("अपोइंटमेंट बुक करना है") == ("hi", True), "hindi devanagari with typo अपोइंटमेंट should be hi")
    check(detect("डॉक्टर बुक करे") == ("hi", True), "hindi devanagari doctor should be hi")
    check(detect("दवा पर्ची डाउनलोड") == ("hi", True), "hindi devanagari prescription download should be hi")

    # 5. Bengali (bn) with spelling variations / typos (High confidence)
    check(detect("আমি একটা অ্যাপয়েন্টমেন্ট বুক করতে চাই") == ("bn", True), "bengali should be bn")
    check(detect("ডাক্তার বুকিং করতে চাই") == ("bn", True), "bengali doctor booking should be bn")
    check(detect("প্রেসক্রিপশন ডাউনলোড") == ("bn", True), "bengali prescription download should be bn")
    check(detect("প্রেসক্রিপসন ডাউনলোড করতে চাই") == ("bn", True), "bengali prescription typo should be bn")

    # 5b. Benglish / Romanized Bengali (bn) (Low confidence)
    check(detect("amar doctor lagbe") == ("bn", False), "benglish amar doctor lagbe should be bn")
    check(detect("amar daktarer appointment lagbe") == ("bn", False), "benglish daktarer appointment lagbe should be bn")
    check(detect("daktar dekhate chai") == ("bn", False), "benglish daktar dekhate chai should be bn")
    check(detect("oshudh prescription lagbe") == ("bn", False), "benglish oshudh prescription lagbe should be bn")

    # 6. Gibberish / Unknown / Mixed Tie-breaking / Standalone 'he' regression
    check(detect("xyz abc") == (None, False), "gibberish should trigger open option")
    check(detect("He needs an appointment") == ("en", False), "standalone 'he' should detect as English, not Hinglish")
    check(detect("doctor appointment lagbe") == ("bn", False), "tie-breaking between english loanwords and lagbe (benglish) should be bn")
    check(detect("appointment book karna hai") == ("hg", False), "tie-breaking with karna hai (hinglish) should be hg")


def test_slot_label_formatting():
    check(conversation._format_slot_label("Morning", True, "en") == "Morning (Today)", "morning today english")
    check(conversation._format_slot_label("Afternoon", False, "en") == "Noon (Tomorrow)", "afternoon tomorrow english should map to Noon")
    check(conversation._format_slot_label("Evening", False, "en") == "Evening (Tomorrow)", "evening tomorrow english")
    
    check(conversation._format_slot_label("Morning", True, "hg") == "Morning (Aaj)", "morning today hinglish")
    check(conversation._format_slot_label("Afternoon", False, "hg") == "Noon (Kal)", "afternoon tomorrow hinglish should map to Noon")
    check(conversation._format_slot_label("Evening", False, "hg") == "Evening (Kal)", "evening tomorrow hinglish")

    check(conversation._format_slot_label("Morning", True, "hi") == "सुबह (आज)", "morning today hindi")
    check(conversation._format_slot_label("Afternoon", False, "hi") == "दोपहर (कल)", "afternoon tomorrow hindi")
    check(conversation._format_slot_label("Evening", False, "hi") == "शाम (कल)", "evening tomorrow hindi")

    check(conversation._format_slot_label("Morning", True, "bn") == "সকাল (আজ)", "morning today bengali")
    check(conversation._format_slot_label("Afternoon", False, "bn") == "দুপুর (আগামীকাল)", "afternoon tomorrow bengali")
    check(conversation._format_slot_label("Evening", False, "bn") == "সন্ধ্যা (আগামীকাল)", "evening tomorrow bengali")


def test_doctor_search_matching_and_formatting():
    check(conversation._is_doctor_search_query("Dr Manoj"), "should match Dr Manoj")
    check(conversation._is_doctor_search_query("book appointment with dr. manoj krishnan"), "should match dr. prefix")
    check(conversation._is_doctor_search_query("doctor xyz"), "should match doctor word")
    check(conversation._is_doctor_search_query("hi, i have to book appointment with Dr, Radha"), "should match Dr, Radha comma case")
    check(not conversation._is_doctor_search_query("i want to book an appointment"), "should not match general booking")

    docs = [
        {"doctorId": "1", "fullName": "Dr. Manoj Krishnan", "specialtyCategory": "Cardiologist", "hospitalName": "Kishanganj Clinic"},
        {"doctorId": "2", "fullName": "Dr. Rajesh Shah", "specialtyCategory": "Dentist", "hospitalName": "Purnea Hospital"},
        {"doctorId": "3", "fullName": "Dr. Radha", "specialtyCategory": "Gynaecologist", "hospitalName": "Radha Hospital"}
    ]
    check(len(conversation._match_doctor_by_query("Dr. Manoj", docs)) == 1, "should match Manoj")
    check(conversation._match_doctor_by_query("Dr. Manoj", docs)[0]["doctorId"] == "1", "should match Manoj id")
    check(len(conversation._match_doctor_by_query("book appointment with Rajesh", docs)) == 1, "should match Rajesh")
    check(len(conversation._match_doctor_by_query("hi, i have to book appointment with Dr, Radha", docs)) == 1, "should match Dr, Radha")
    check(conversation._match_doctor_by_query("hi, i have to book appointment with Dr, Radha", docs)[0]["doctorId"] == "3", "should match Radha id")

    doc = {
        "fullName": "Dr. Manoj Krishnan",
        "primaryMedicalSpecialityPatientFacingName": "Cardiologist",
        "hospitalName": "Kishanganj Clinic",
        "rating": 4.5,
        "fee": 500.0,
        "experienceYears": 12,
        "latitude": 26.1,
        "longitude": 87.9
    }
    context = {"patient_lat": 26.1, "patient_lng": 87.9}
    desc = conversation._doctor_row_description(doc, context)
    check("Cardiologist" in desc, "specialty should be in description")
    check("Kishanganj Clinic" in desc, "clinic should be in description")
    check("⭐4.5" in desc, "rating should be in description")
    check("₹500" in desc, "fee should be in description")
    check("12yrs" in desc, "experience should be in description")
    check("0km" in desc, "distance should be in description")
    check(len(desc) <= 72, f"description length {len(desc)} should be <= 72")
    check(conversation._clean_specialty("Nephrologist / Kidney Specialist") == "Nephrologist", "clean Nephrologist")
    check(conversation._clean_specialty("QA Dev Seed - General Practice") == "General Practice", "clean General Practice")
    check(conversation._clean_hospital("Kishanganj General Hospital (QA Dev Seed)") == "Kishanganj General Hospital", "clean Kishanganj")

    # Test location-based filtering logic
    import asyncio
    class MockClient:
        pass
    mock_client = MockClient()

    original_get_all_doctors = city_index.get_all_doctors
    async def mock_get_all_doctors(*args, **kwargs):
        return [
            {"doctorId": "1", "fullName": "Dr. Radha Das", "city": "Kishanganj", "latitude": 26.1, "longitude": 87.9},
            {"doctorId": "2", "fullName": "Dr. Radha Kapoor", "city": "Mumbai", "latitude": 19.0, "longitude": 72.8}
        ]
    city_index.get_all_doctors = mock_get_all_doctors

    # _search_doctors_flow also calls city_index.get_index() (to extract a city name typed
    # inline in the query, e.g. "Radha in Kishanganj") -- an empty index is fine here since
    # both sub-tests below already provide city/GPS directly in context rather than relying
    # on that extraction, but the real (unmocked) call would otherwise hit the network and
    # get caught by _search_doctors_flow's own try/except, silently returning zero matches.
    original_get_index = city_index.get_index
    async def mock_get_index(*args, **kwargs):
        return {}
    city_index.get_index = mock_get_index

    rendered_doctors = []
    original_render_doctor_list = conversation._render_doctor_list
    async def mock_render_doctor_list(client, phone, context, doctors, current_step=None):
        rendered_doctors.extend(doctors)
    conversation._render_doctor_list = mock_render_doctor_list

    try:
        rendered_doctors.clear()
        context_typed = {"search_doctor_query": "Radha", "city": "Kishanganj", "lang": "en"}
        asyncio.run(conversation._search_doctors_flow(mock_client, "123", context_typed, "choosing_location"))
        check(len(rendered_doctors) == 1, "should filter to 1 local doctor for typed city")
        check(rendered_doctors[0]["doctorId"] == "1", "should be Dr. Radha Das in Kishanganj")

        rendered_doctors.clear()
        context_gps = {"search_doctor_query": "Radha", "patient_lat": 19.0, "patient_lng": 72.8, "lang": "en"}
        asyncio.run(conversation._search_doctors_flow(mock_client, "123", context_gps, "choosing_location"))
        check(len(rendered_doctors) == 1, "should filter to 1 local doctor for GPS location")
        check(rendered_doctors[0]["doctorId"] == "2", "should be Dr. Radha Kapoor in Mumbai")
    finally:
        city_index.get_all_doctors = original_get_all_doctors
        city_index.get_index = original_get_index
        conversation._render_doctor_list = original_render_doctor_list


def test_extract_location_from_query():
    from app.resolver import extract_location_from_query
    index = {"Kishanganj": [], "Purnea": [], "Delhi NCR": []}
    
    city, cleaned = extract_location_from_query("doctor Sharma in Kishanganj", index)
    check(city == "Kishanganj", "should extract Kishanganj")
    check(cleaned == "doctor sharma", "should clean query to doctor sharma")
    
    city, cleaned = extract_location_from_query("dentist near Purnea", index)
    check(city == "Purnea", "should extract Purnea")
    check(cleaned == "dentist", "should clean query to dentist")
    
    city, cleaned = extract_location_from_query("doctor Avinash in Delhi NCR", index)
    check(city == "Delhi NCR", "should extract Delhi NCR (longest match first)")
    check(cleaned == "doctor avinash", "should clean Delhi NCR")
    
    city, cleaned = extract_location_from_query("doctor Avinash without city", index)
    check(city is None, "should return None city if not found")
    check(cleaned == "doctor Avinash without city", "should return same query")


def test_safety_triage_interception():
    import asyncio
    class MockClient:
        pass
    mock_client = MockClient()
    
    sent_messages = []
    async def mock_send_text(client, phone, text):
        sent_messages.append((phone, text))
    
    original_send_text = conversation.whatsapp_client.send_text
    conversation.whatsapp_client.send_text = mock_send_text
    
    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            return {"current_step": "awaiting_symptom", "context": {"lang": "en", "city": "Kishanganj"}}
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)
            
    original_db = conversation.db
    conversation.db = MockDB()
    
    try:
        asyncio.run(conversation.handle_message(mock_client, "999", "User", "text", "Help, I have severe chest pain and breathlessness!"))
        check(len(sent_messages) == 1, "should send safety warning")
        check("EMERGENCY WARNING" in sent_messages[0][1], "should contain English warning text")
        
        sent_messages.clear()
        asyncio.run(conversation.handle_message(mock_client, "999", "User", "text", "mere papa behosh ho gaye hain aur saans lene me taklif hai"))
        check(len(sent_messages) == 1, "should send safety warning in Hinglish")
        check("Emergency Warning: Agar aapko" in sent_messages[0][1], "should contain Hinglish warning text")
    finally:
        conversation.whatsapp_client.send_text = original_send_text
        conversation.db = original_db


def test_unified_date_time_flow():
    import asyncio
    import json
    class MockClient:
        pass
    mock_client = MockClient()
    
    sent_flows = []
    async def mock_send_flow(client, to, body_text, flow_id, flow_cta, screen_id, flow_token, initial_data=None):
        sent_flows.append((to, initial_data))
        return True
        
    sent_buttons = []
    async def mock_send_buttons(client, phone, text, buttons):
        sent_buttons.append((phone, text, buttons))
        
    original_send_flow = conversation.whatsapp_client.send_flow
    conversation.whatsapp_client.send_flow = mock_send_flow
    
    original_send_buttons = conversation.whatsapp_client.send_buttons
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    
    original_availability = conversation.hms_client.get_doctor_availability
    async def mock_availability(doctor_id, date):
        return {
            "isAvailable": True,
            "shifts": [
                {"shiftName": "Afternoon", "startTime": "13:00", "endTime": "17:00"},
                {"shiftName": "Morning", "startTime": "09:00", "endTime": "12:00"}
            ]
        }
    conversation.hms_client.get_doctor_availability = mock_availability
    
    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, context = self.state[phone]
                return {"current_step": step, "context": context}
            booking = conversation.booking_slots.empty()
            conversation.booking_slots.fill(booking, "lang", "en")
            conversation.booking_slots.fill(booking, "location", "Kishanganj")
            conversation.booking_slots.fill(booking, "doctor", {"id": "d1", "fullName": "Dr. Sen"})
            return {
                "current_step": "choosing_doctor",
                "context": {
                    "lang": "en",
                    "city": "Kishanganj",
                    "doctor_id": "d1",
                    "booking": booking
                }
            }
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)
            
    original_db = conversation.db
    db_mock = MockDB()
    conversation.db = db_mock
    
    try:
        booking = db_mock.state.get("123", {}).get("context", {}).get("booking")
        if not booking:
            booking = conversation.booking_slots.empty()
            conversation.booking_slots.fill(booking, "lang", "en")
            conversation.booking_slots.fill(booking, "location", "Kishanganj")
            conversation.booking_slots.fill(booking, "doctor", {"id": "d1", "fullName": "Dr. Sen"})
            
        # Test 1: Run with time_of_day_hint = "Morning"
        asyncio.run(conversation._advance_booking_flow(mock_client, "123", {
            "lang": "en", "city": "Kishanganj", "doctor_id": "d1", "booking": booking,
            "current_step": "choosing_doctor", "time_of_day_hint": "Morning"
        }, booking))
        check(len(sent_flows) == 1, "should trigger Flow form send")
        init_data_morning = sent_flows[0][1]
        check(init_data_morning is not None and "slots" in init_data_morning, "initial_data must contain slots")
        check(init_data_morning["slots"][0]["id"].endswith("morning"), f"Morning slot must be first, got {init_data_morning['slots'][0]['id']}")

        # Test 2: Run with time_of_day_hint = "Afternoon"
        sent_flows.clear()
        asyncio.run(conversation._advance_booking_flow(mock_client, "123", {
            "lang": "en", "city": "Kishanganj", "doctor_id": "d1", "booking": booking,
            "current_step": "choosing_doctor", "time_of_day_hint": "Afternoon"
        }, booking))
        check(len(sent_flows) == 1, "should trigger Flow form send again")
        init_data_afternoon = sent_flows[0][1]
        check(init_data_afternoon["slots"][0]["id"].endswith("afternoon"), f"Afternoon slot must be first, got {init_data_afternoon['slots'][0]['id']}")

        # Simulate submission using first slot from Morning run
        selected_slot_id = init_data_morning["slots"][0]["id"]
        submit_data = {
            "name": "Riya",
            "age": "20",
            "gender": "Female",
            "guardian": "Self",
            "slot_id": selected_slot_id
        }
        
        asyncio.run(conversation.handle_message(
            mock_client, "123", "User", "nfm_reply", json.dumps(submit_data)
        ))
        
        final_state = db_mock.state["123"]
        final_ctx = final_state[1]
        final_booking = final_ctx["booking"]
        
        check(final_booking["date"]["status"] == "filled", "date slot must be filled after Flow submit")
        check(final_booking["shift"]["status"] == "filled", "shift slot must be filled after Flow submit")
        check(final_booking["patient"]["status"] == "filled", "patient slot must be filled after Flow submit")
        check(final_state[0] == "confirming", "must advance straight to confirming step")
        
    finally:
        conversation.whatsapp_client.send_flow = original_send_flow
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.hms_client.get_doctor_availability = original_availability
        conversation.db = original_db


def test_is_doctor_search_query_whitelist():
    check(conversation._is_doctor_search_query("Sir dard kr raha hai doctor btao") == False, "Sir dard sentence must be False")
    check(conversation._is_doctor_search_query("Dr. Sharma se milna hai") == True, "Dr. Sharma must be True")
    check(conversation._is_doctor_search_query("Dr Sharma se milna hai") == True, "Dr Sharma must be True")
    check(conversation._is_doctor_search_query("doctor Verma se milna hai") == True, "doctor Verma must be True")
    check(conversation._is_doctor_search_query("doctor btao") == False, "doctor btao must be False")


def test_first_message_nlu_symptom_routing():
    import asyncio
    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            val = self.state.get(phone)
            if not val:
                return None
            return {"current_step": val[0], "context": val[1]}
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)
        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)
        async def log_nlu_interaction(self, *a, **k):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    # Mock NLU client classification to return describe_symptom with symptom "pet me bahut dard ho raha hai"
    mock_nlu_val = {
        "intent": "describe_symptom",
        "confidence": "high",
        "entities": {"symptom": "pet me bahut dard ho raha hai"}
    }
    async def mock_classify(client, text):
        if "Kishanganj" in text:
            return {
                "intent": "provide_location",
                "confidence": "high",
                "entities": {"location": "Kishanganj"}
            }
        return mock_nlu_val
    original_classify = conversation.nlu_client.classify_message
    conversation.nlu_client.classify_message = mock_classify

    # Mock symptom routing and category listing
    original_route_symptom = conversation.symptom_client.route_symptom
    async def mock_route_symptom(text):
        return ["Gynaecologist"]
    conversation.symptom_client.route_symptom = mock_route_symptom

    original_list_specialties = conversation.hms_client.list_specialties
    async def mock_list_specialties():
        return [{"category": "Gynaecologist"}]
    conversation.hms_client.list_specialties = mock_list_specialties

    sent_texts = []
    sent_buttons = []
    sent_lists = []
    
    async def mock_send_text(client, phone, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, phone, text, buttons):
        sent_buttons.append((text, buttons))
    async def mock_send_list(client, phone, text, button_label, rows, section_title):
        sent_lists.append((text, rows))

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_list = conversation.whatsapp_client.send_list
    original_send_loc = conversation.whatsapp_client.send_location_request
    
    async def mock_send_location_request(client, phone, text):
        pass
        
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_list = mock_send_list
    conversation.whatsapp_client.send_location_request = mock_send_location_request

    try:
        class MockClientObj:
            pass
        mock_client_obj = MockClientObj()
        
        # Fresh user sends "pet me bahut dard ho raha hai"
        asyncio.run(conversation.handle_message(
            mock_client_obj, "user_fresh_nlu", "Patient", "text", "pet me bahut dard ho raha hai"
        ))

        # Assertions
        state = db_mock.state.get("user_fresh_nlu")
        check(state is not None, "fresh user session state must be created")
        check(state[0] == "confirming_language", f"should transition to confirming_language, got {state[0]!r}")
        check(state[1].get("pending_specialty") == "Gynaecologist", f"should save matched specialty, got {state[1].get('pending_specialty')!r}")
        check(state[1].get("guess_lang") == "hg", f"should guess language (Hinglish), got {state[1].get('guess_lang')!r}")
        check(len(sent_buttons) == 1, "should send language confirmation buttons")

        # Simulate confirming language (Yes)
        asyncio.run(conversation.handle_message(
            mock_client_obj, "user_fresh_nlu", "Patient", "button_reply", "lang_confirm_yes"
        ))

        state = db_mock.state.get("user_fresh_nlu")
        check(state[0] == "choosing_location", f"should transition to choosing_location, got {state[0]!r}")

        # Simulate sharing location by typing city
        asyncio.run(conversation.handle_message(
            mock_client_obj, "user_fresh_nlu", "Patient", "text", "Kishanganj"
        ))

        state = db_mock.state.get("user_fresh_nlu")
        check(state[0] == "choosing_sort", f"should transition to choosing_sort, got {state[0]!r}")
        check(state[1].get("specialty_category") == "Gynaecologist", f"specialty_category must be set to Gynaecologist, got {state[1].get('specialty_category')!r}")
        check(len(sent_lists) == 1, "should send specialty sort options list")

    finally:
        conversation.db = original_db
        conversation.nlu_client.classify_message = original_classify
        conversation.symptom_client.route_symptom = original_route_symptom
        conversation.hms_client.list_specialties = original_list_specialties
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_list = original_send_list
        conversation.whatsapp_client.send_location_request = original_send_loc

def test_high_confidence_language_bypass():
    import asyncio
    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            val = self.state.get(phone)
            if not val:
                return None
            return {"current_step": val[0], "context": val[1]}
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)
        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    # Mock NLU to return out_of_scope
    async def mock_classify(client, text):
        return {"intent": "out_of_scope", "confidence": "low", "entities": {}}
    original_classify = conversation.nlu_client.classify_message
    conversation.nlu_client.classify_message = mock_classify

    sent_texts = []
    sent_buttons = []
    
    async def mock_send_text(client, phone, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, phone, text, buttons):
        sent_buttons.append((text, buttons))
    async def mock_send_loc(client, phone, text):
        pass

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_loc = conversation.whatsapp_client.send_location_request

    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_location_request = mock_send_loc

    try:
        class MockClientObj:
            pass
        mock_client = MockClientObj()

        # Send Devanagari text (unambiguous high confidence)
        asyncio.run(conversation.handle_message(
            mock_client, "user_high_conf", "Patient", "text", "मुझे डॉक्टर चाहिए"
        ))

        # Check state: should skip language confirmation and request location immediately
        state = db_mock.state.get("user_high_conf")
        check(state is not None, "session state must be created")
        check(state[0] == "choosing_location", f"should transition straight to choosing_location, got {state[0]!r}")
        check(state[1].get("lang") == "hi", f"language must be set directly to hi, got {state[1].get('lang')!r}")
        check(len(sent_buttons) == 0, "should NOT send language confirmation buttons")
        check(any("स्वागत है" in t for t in sent_texts), "should send welcome banner text")

    finally:
        conversation.db = original_db
        conversation.nlu_client.classify_message = original_classify
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_location_request = original_send_loc



def test_cancel_quit_and_back_logic():
    class MockDB:
        def __init__(self):
            self.state = {}
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

    import asyncio
    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock
    try:
        asyncio.run(conversation._transition_to("123", "step1", {"lang": "en"}, "choosing_language"))
        step, ctx = db_mock.state["123"]
        check(step == "step1", "should transition to step1")
        check(len(ctx.get("_history", [])) == 1, "should have 1 history record")
        check(ctx["_history"][0]["current_step"] == "choosing_language", "history step should be choosing_language")

        asyncio.run(conversation._transition_to("123", "step2", ctx, "step1"))
        step, ctx = db_mock.state["123"]
        check(step == "step2", "should transition to step2")
        check(len(ctx.get("_history", [])) == 2, "should have 2 history records")
        check(ctx["_history"][1]["current_step"] == "step1", "second history step should be step1")
    finally:
        conversation.db = original_db


def test_three_search_modes_flow():
    import asyncio
    class MockClient:
        pass
    mock_client = MockClient()

    class MockDB:
        def __init__(self):
            self.state = {}
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_messages = []
    original_send_text = conversation.whatsapp_client.send_text
    async def mock_send_text(client, to, text):
        sent_messages.append(text)
    conversation.whatsapp_client.send_text = mock_send_text

    try:
        context = {"lang": "en"}
        asyncio.run(conversation._handle_choosing_search_mode(mock_client, "123", "button_reply", "name", context))
        step, ctx = db_mock.state["123"]
        check(step == "awaiting_doctor_name", "should transition to awaiting_doctor_name")
        check(len(sent_messages) == 1, "should send 1 text message")
        check("type the name of the doctor" in sent_messages[0], "should ask for doctor name")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text


def test_language_confirmation_flow():
    import asyncio
    class MockClient:
        pass
    mock_client = MockClient()

    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            return self.state.get(phone)
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = {"current_step": step, "context": context}
        async def clear_conversation_state(self, phone):
            self.state[phone] = None
        async def log_nlu_interaction(self, *args, **kwargs):
            pass
        async def update_last_nlu_log_correctness(self, *args, **kwargs):
            pass
        async def mark_session_nlu_correctness_on_booking(self, *args, **kwargs):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_texts = []
    sent_buttons = []
    sent_locations = []
    sent_lists = []

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_location = conversation.whatsapp_client.send_location_request
    original_send_list = conversation.whatsapp_client.send_list

    async def mock_send_text(client, to, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))
    async def mock_send_location(client, to, text):
        sent_locations.append(text)
    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append((text, button_label, rows, section_title))

    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_location_request = mock_send_location
    conversation.whatsapp_client.send_list = mock_send_list

    try:
        # 1. Trigger initial text input that gets auto-detected as Hindi (Devanagari - High confidence)
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "मुझे डॉक्टर चाहिए"))
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is not None, "conversation state should be created")
        check(state["current_step"] == "choosing_location", "should transition directly to choosing_location")
        check(state["context"].get("lang") == "hi", "should set language directly to Hindi")
        check(len(sent_locations) == 1, "should send location request directly")

        # 1b. Reset and trigger text input that gets auto-detected as Hinglish (Low confidence - should prompt for confirmation)
        asyncio.run(db_mock.clear_conversation_state("123"))
        sent_buttons.clear()
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "mujhe doctor chahiye"))
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state["current_step"] == "confirming_language", "should transition to confirming_language for low-confidence match")
        check(len(sent_buttons) == 1, "should send language confirmation buttons")

        # 2. Reset and test fallback when language is not auto-detected (e.g. hello greeting)
        asyncio.run(db_mock.clear_conversation_state("123"))
        sent_lists.clear()
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "hello"))
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is not None, "state should be created for generic greeting")
        check(state["current_step"] == "choosing_language", "should transition to choosing_language for open choice")
        check(len(sent_lists) == 1, "should send language selection list")
        check(len(sent_lists[0][2]) == 4, "list message must contain all 4 languages")
        
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_location_request = original_send_location
        conversation.whatsapp_client.send_list = original_send_list


def test_wit_nlu_integration():
    import asyncio
    mock_client = object()
    
    class MockDB:
        def __init__(self):
            self.state = {}
            self.nlu_logs = []
            self.correctness_updates = []
            self.booking_updates = []
        async def get_conversation_state(self, phone):
            return self.state.get(phone)
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = {"current_step": step, "context": context}
        async def clear_conversation_state(self, phone):
            self.state[phone] = None
        async def log_nlu_interaction(self, phone, session_id, utterance, nlu_brain, intent, confidence, doctor_name, specialty, symptom, formatted_date, routed_step=None, is_correct=None, user_feedback=None):
            self.nlu_logs.append((phone, session_id, utterance, nlu_brain, intent, confidence, doctor_name, specialty, symptom, formatted_date, routed_step, is_correct, user_feedback))
        async def update_last_nlu_log_correctness(self, phone, is_correct, feedback):
            self.correctness_updates.append((phone, is_correct, feedback))
        async def mark_session_nlu_correctness_on_booking(self, phone, booked_doctor_name):
            self.booking_updates.append((phone, booked_doctor_name))

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    original_availability = conversation.hms_client.get_doctor_availability
    async def mock_availability(doc_id, date_val):
        return {"shifts": [{"name": "Morning", "startTime": "09:00:00", "endTime": "12:00:00"}]}
    conversation.hms_client.get_doctor_availability = mock_availability

    sent_texts = []
    sent_buttons = []
    sent_lists = []

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_list = conversation.whatsapp_client.send_list

    async def mock_send_text(client, to, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))
    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append((text, button_label, rows, section_title))

    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_list = mock_send_list

    original_classify = conversation.nlu_client.classify_message
    
    # Mock NLU results
    mock_nlu_val = {"intent": "unknown", "confidence": "low", "entities": {}}
    async def mock_classify(client, text):
        return mock_nlu_val
        
    conversation.nlu_client.classify_message = mock_classify

    try:
        # 1. Test cancel intent
        mock_nlu_val.update({"intent": "cancel_appointment", "confidence": "high", "entities": {}})
        asyncio.run(db_mock.save_conversation_state("123", "choosing_location", {"lang": "en"}))
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "cancel please"))
        
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is None, "NLU cancel should clear conversation state")
        check(any("thank" in t.lower() or "cancel" in t.lower() for t in sent_texts), "should send cancellation confirmation")

        # 2. Test navigate back intent
        sent_texts.clear()
        mock_nlu_val.update({"intent": "navigate_back", "confidence": "high", "entities": {}})
        history_context = {"lang": "en", "_history": [{"current_step": "choosing_language", "context": {"lang": "en"}}]}
        asyncio.run(db_mock.save_conversation_state("123", "choosing_location", history_context))
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "go back"))
        
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is not None, "NLU back should preserve state")
        check(state["current_step"] == "choosing_language", "NLU back should transition to previous step in history")

        # 3. Test doctor name entity matching directly in choosing_doctor state (Avinash)
        sent_texts.clear()
        mock_nlu_val.update({
            "intent": "unknown",
            "confidence": "low",
            "entities": {
                "doctor_name": "Avinash"
            }
        })
        original_get_all_doctors = city_index.get_all_doctors
        async def mock_get_docs():
            return [
                {"doctorId": "5", "fullName": "Dr. Avinash", "city": "Kishanganj", "latitude": 26.1, "longitude": 87.9},
                {"doctorId": "6", "fullName": "Dr. Avinash Senior", "city": "Kishanganj", "latitude": 26.1, "longitude": 87.9}
            ]
        city_index.get_all_doctors = mock_get_docs
        original_get_index = city_index.get_index
        async def mock_get_index(*a, **kw):
            return {}
        city_index.get_index = mock_get_index

        asyncio.run(db_mock.save_conversation_state("123", "choosing_doctor", {"lang": "en", "city": "Kishanganj"}))
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "Sorry dr Avinash tha"))

        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is not None, "entity state should exist")
        check(state["current_step"] == "choosing_doctor", "should transition to choosing_doctor after listing matches")
        check(state["context"].get("search_doctor_query") == "Avinash", "should set search query to Avinash")

        city_index.get_all_doctors = original_get_all_doctors
        city_index.get_index = original_get_index

        # 4. Test change selection without doctor name
        sent_texts.clear()
        mock_nlu_val.update({
            "intent": "change_selection",
            "confidence": "high",
            "entities": {}
        })
        asyncio.run(db_mock.save_conversation_state("123", "choosing_doctor", {"lang": "en", "city": "Kishanganj"}))
        asyncio.run(conversation.handle_message(mock_client, "123", "User", "text", "Change the doctor"))
        
        state = asyncio.run(db_mock.get_conversation_state("123"))
        check(state is not None, "change selection state should exist")
        check(state["current_step"] == "awaiting_doctor_name", "should transition to awaiting_doctor_name step")
        check(any("looking for" in t.lower() or "dhoond" in t.lower() for t in sent_texts), "should ask user for doctor's name")
        
        check(len(db_mock.nlu_logs) > 0, "should log NLU interactions to the database")

    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_list = original_send_list
        conversation.nlu_client.classify_message = original_classify
        conversation.hms_client.get_doctor_availability = original_availability


def test_previously_dropped_intents_now_handled():
    """ask_pricing, describe_symptom, and provide_location used to be classified,
    routed, and then silently fall through every elif in handle_message — the patient's
    actual question got ignored and they'd get bounced to a fresh welcome/location prompt
    instead. Covers all three now having a real response."""
    import asyncio
    mock_client = object()

    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            return self.state.get(phone)
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = {"current_step": step, "context": context}
        async def clear_conversation_state(self, phone):
            self.state[phone] = None
        async def log_nlu_interaction(self, *args, **kwargs):
            pass
        async def update_last_nlu_log_correctness(self, *args, **kwargs):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_texts = []
    sent_lists = []
    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_list = conversation.whatsapp_client.send_list
    async def mock_send_text(client, to, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, to, text, buttons):
        pass
    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append((text, button_label, rows, section_title))
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_list = mock_send_list

    mock_nlu_val = {"intent": "unknown", "confidence": "high", "entities": {}}
    async def mock_classify(client, text):
        return mock_nlu_val
    original_classify = conversation.nlu_client.classify_message
    conversation.nlu_client.classify_message = mock_classify

    doctors = [
        {"doctorId": "1", "fullName": "Dr. Amit Sharma", "city": "Kishanganj", "fee": 500},
        {"doctorId": "2", "fullName": "Dr. Priya Sharma", "city": "Patna", "fee": 700},
        {"doctorId": "3", "fullName": "Dr. Manoj Kumar", "city": "Kishanganj", "fee": 400},
        {"doctorId": "4", "fullName": "Dr. Kavita Sharma", "city": "Kishanganj", "fee": 600},
    ]
    original_get_all_doctors = conversation.city_index.get_all_doctors
    async def mock_get_all_doctors(force_refresh=False):
        return doctors
    conversation.city_index.get_all_doctors = mock_get_all_doctors

    original_list_specialties = conversation.hms_client.list_specialties
    async def mock_list_specialties():
        return [{"category": "Gynaecologist"}, {"category": "Orthopaedic Surgeon (Bone)"}]
    conversation.hms_client.list_specialties = mock_list_specialties

    original_list_doctors = conversation.hms_client.list_doctors
    async def mock_list_doctors(specialty_category, page_size=10, city=None):
        return [d for d in doctors if specialty_category == "Gynaecologist"]
    conversation.hms_client.list_doctors = mock_list_doctors

    original_route_symptom = conversation.symptom_client.route_symptom
    async def mock_route_symptom(query):
        return ["Gynaecologist"]
    conversation.symptom_client.route_symptom = mock_route_symptom

    original_get_index = conversation.city_index.get_index
    async def mock_get_index(force_refresh=False):
        return {"Kishanganj": [[26.10, 87.95]], "Patna": [[25.61, 85.14]]}
    conversation.city_index.get_index = mock_get_index

    try:
        # 1. ask_pricing + a name matching exactly one doctor -> a fee, not a dropped message
        mock_nlu_val.update({"intent": "ask_pricing", "entities": {"doctor_name": "Manoj"}})
        asyncio.run(db_mock.save_conversation_state("p1", "choosing_location", {"lang": "en", "city": "Kishanganj"}))
        asyncio.run(conversation.handle_message(mock_client, "p1", "User", "text", "Dr Manoj ki fees kitni hai?"))
        check(sent_texts and "400" in sent_texts[-1] and "500" not in sent_texts[-1], f"unique doctor -> their own fee, got {sent_texts[-1] if sent_texts else None!r}")

        # 2. ask_pricing + a name matching multiple doctors (even after narrowing to the
        # patient's own city — two different Sharmas both practise in Kishanganj) -> a
        # list, not silence and not a guess
        sent_texts.clear()
        mock_nlu_val.update({"intent": "ask_pricing", "entities": {"doctor_name": "Sharma"}})
        asyncio.run(conversation.handle_message(mock_client, "p1", "User", "text", "Sharma ki fees?"))
        check(sent_texts and "500" in sent_texts[-1] and "600" in sent_texts[-1], f"ambiguous name -> lists every match's fee, got {sent_texts[-1] if sent_texts else None!r}")

        # 3. ask_pricing + a specialty -> a fee range (mock_list_doctors returns all 4
        # doctors for this category: fees 400/500/600/700 -> range is 400 to 700)
        sent_texts.clear()
        mock_nlu_val.update({"intent": "ask_pricing", "entities": {"specialty": "Gynaecologist"}})
        asyncio.run(conversation.handle_message(mock_client, "p1", "User", "text", "Gynaecologist consultation kitne ki hai?"))
        check(sent_texts and "400" in sent_texts[-1] and "700" in sent_texts[-1], f"specialty pricing -> a fee range, got {sent_texts[-1] if sent_texts else None!r}")

        # 4. describe_symptom -> proceeds to the sort/doctor-list flow, same as
        # book_appointment already does for a symptom, instead of being dropped
        sent_lists.clear()
        mock_nlu_val.update({"intent": "describe_symptom", "entities": {"symptom": "pet me dard"}})
        asyncio.run(conversation.handle_message(mock_client, "p1", "User", "text", "pet me bahut dard ho raha hai"))
        state = asyncio.run(db_mock.get_conversation_state("p1"))
        check(state["current_step"] == "choosing_sort", f"describe_symptom should reach choosing_sort, got {state['current_step']!r}")

        # 5. provide_location -> updates city and moves the conversation forward, instead
        # of the message being silently ignored
        asyncio.run(db_mock.save_conversation_state("p2", "choosing_search_mode", {"lang": "en"}))
        mock_nlu_val.update({"intent": "provide_location", "entities": {"location": "Kishanganj"}})
        asyncio.run(conversation.handle_message(mock_client, "p2", "User", "text", "main Kishanganj mein hoon"))
        state = asyncio.run(db_mock.get_conversation_state("p2"))
        check(state["context"].get("city") == "Kishanganj", f"provide_location should resolve and set city, got {state['context'].get('city')!r}")
        check(state["current_step"] == "choosing_search_mode", "should proceed to the search-mode prompt after location is known")

    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_list = original_send_list
        conversation.nlu_client.classify_message = original_classify
        conversation.city_index.get_all_doctors = original_get_all_doctors
        conversation.hms_client.list_specialties = original_list_specialties
        conversation.hms_client.list_doctors = original_list_doctors
        conversation.symptom_client.route_symptom = original_route_symptom
        conversation.city_index.get_index = original_get_index


def test_data_bearing_steps_are_never_model_phrased():
    """The safety boundary for LLM phrasing: any step whose message carries real booking
    data (the confirmation summary with doctor/fee/date, the doctor list, slot labels)
    must have no STEP_GOALS entry, so generate_step_prompt structurally cannot write it.
    A hallucinated fee or appointment time reaching a patient is the failure this prevents."""
    from app import nlu_client as nc
    for data_bearing_step in ("confirming", "choosing_doctor", "choosing_slot", "choosing_specialty", "choosing_sort"):
        check(data_bearing_step not in nc.STEP_GOALS, f"{data_bearing_step} must not be model-phrasable — it carries booking data")

    # And the phrasing prompt itself must forbid inventing that data, belt-and-braces.
    lowered = nc.STEP_PROMPT_SYSTEM.lower()
    check("never invent" in lowered, "phrasing prompt should explicitly forbid inventing details")


def test_phrase_falls_back_to_template_when_model_unavailable():
    """Phrasing must never be load-bearing: if the model is unavailable, slow, or errors,
    the patient gets exactly the message the bot has always sent. This is also why the
    existing template-asserting tests still pass — in tests the key is "test", so
    _query_llm_text returns None and every _phrase call lands on its template."""
    import asyncio
    from app import nlu_client as nc

    context = {"lang": "en", "city": "Kishanganj"}
    expected = i18n.t("symptom_ask", "en")

    original = nc.generate_step_prompt
    try:
        async def model_unavailable(client, step, lang, known=None):
            return None
        nc.generate_step_prompt = model_unavailable
        got = asyncio.run(conversation._phrase(object(), "awaiting_symptom", context, "symptom_ask"))
        check(got == expected, f"unavailable model should fall back to the template, got {got!r}")

        async def model_raises(client, step, lang, known=None):
            raise RuntimeError("simulated model outage")
        nc.generate_step_prompt = model_raises
        got = asyncio.run(conversation._phrase(object(), "awaiting_symptom", context, "symptom_ask"))
        check(got == expected, f"a raising model must not break the reply, got {got!r}")

        async def model_works(client, step, lang, known=None):
            return "Aapko kya ho raha hai?"
        nc.generate_step_prompt = model_works
        got = asyncio.run(conversation._phrase(object(), "awaiting_symptom", context, "symptom_ask"))
        check(got == "Aapko kya ho raha hai?", f"a working model's wording should be used, got {got!r}")
    finally:
        nc.generate_step_prompt = original


def test_failed_hot_swap_clears_stale_doctor_and_reprompts():
    """A patient at choosing_slot (an existing doctor already selected) names a DIFFERENT
    doctor who resolves to zero matches. Before this fix: search_doctor_query and the old
    doctor's id/name/fee/hospital fields stayed stale in context forever, and the patient
    got a dead-end "not found" message with no prompt for what to do next — caught during
    shadow-mode testing (_shadow_clipboard's search_doctor_query handling), fixed to mirror
    _handle_awaiting_doctor_name's own "not found" branch, which already cleaned up and
    re-prompted."""
    import asyncio
    mock_client = object()

    class MockDB:
        def __init__(self):
            self.state = {}
        async def get_conversation_state(self, phone):
            return self.state.get(phone)
        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = {"current_step": step, "context": context}
        async def clear_conversation_state(self, phone):
            self.state[phone] = None
        async def log_nlu_interaction(self, *args, **kwargs):
            pass
        async def update_last_nlu_log_correctness(self, *args, **kwargs):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_texts = []
    sent_buttons = []
    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    async def mock_send_text(client, to, text):
        sent_texts.append(text)
    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons

    # datetime included so intent_router's REQUIRED_ENTITIES check for book_appointment
    # (doctor_name/specialty AND datetime) is satisfied and reaches proceed_to_business_logic
    # — otherwise it stops at ask_followup before handle_message's hot-swap check ever runs.
    mock_nlu_val = {"intent": "book_appointment", "confidence": "high", "entities": {"doctor_name": "Nobody Matching", "datetime": "2026-08-11"}}
    async def mock_classify(client, text):
        return mock_nlu_val
    original_classify = conversation.nlu_client.classify_message
    conversation.nlu_client.classify_message = mock_classify

    # No doctor in the pool matches "Nobody Matching" -> resolves to zero.
    original_get_all_doctors = conversation.city_index.get_all_doctors
    async def mock_get_all_doctors(force_refresh=False):
        return [{"doctorId": "old_id", "fullName": "Dr. Old Doctor", "city": "Kishanganj"}]
    conversation.city_index.get_all_doctors = mock_get_all_doctors
    original_get_index = conversation.city_index.get_index
    async def mock_get_index(*a, **kw):
        return {}
    conversation.city_index.get_index = mock_get_index

    try:
        asyncio.run(db_mock.save_conversation_state("hs1", "choosing_slot", {
            "lang": "en", "city": "Kishanganj",
            "doctor_id": "old_id", "doctor_name": "Dr. Old Doctor", "doctor_fee": 300,
            "hospital_name": "Old Hospital", "hospital_address": "Somewhere",
            "hospital_city": "Kishanganj", "hospital_lat": 26.1, "hospital_lng": 87.9,
        }))
        asyncio.run(conversation.handle_message(mock_client, "hs1", "User", "text", "nahi Nobody Matching se dikhao"))

        check(any("not" in t.lower() or "nahi" in t.lower() for t in sent_texts), "should tell the patient the new name wasn't found")
        check(len(sent_buttons) == 1, "should re-prompt with search-mode options, not leave a dead end")

        state = asyncio.run(db_mock.get_conversation_state("hs1"))
        check(state["current_step"] == "choosing_search_mode", f"should land on choosing_search_mode, got {state['current_step']!r}")
        ctx = state["context"]
        for stale_key in ("doctor_id", "doctor_name", "doctor_fee", "hospital_name", "hospital_address", "hospital_city", "hospital_lat", "hospital_lng", "search_doctor_query"):
            check(stale_key not in ctx, f"{stale_key} should be cleared after a failed hot-swap, still present: {ctx.get(stale_key)!r}")

        # Same miss, reached via change_selection instead of the hot-swap branch — this
        # path had the identical bug and now shares _handle_doctor_search_miss.
        sent_texts.clear()
        sent_buttons.clear()
        mock_nlu_val.update({"intent": "change_selection", "entities": {"new_doctor_name": "Nobody Matching"}})
        asyncio.run(db_mock.save_conversation_state("hs2", "choosing_doctor", {
            "lang": "en", "city": "Kishanganj",
            "doctor_id": "old_id", "doctor_name": "Dr. Old Doctor", "doctor_fee": 300,
        }))
        asyncio.run(conversation.handle_message(mock_client, "hs2", "User", "text", "Old nahi, Nobody Matching dikhao"))
        state = asyncio.run(db_mock.get_conversation_state("hs2"))
        check(state["current_step"] == "choosing_search_mode", f"change_selection miss should also re-prompt, got {state['current_step']!r}")
        check("doctor_id" not in state["context"], "change_selection miss should also clear the stale doctor")

        # And via a plain book_appointment naming an unfindable doctor from an earlier step.
        sent_texts.clear()
        sent_buttons.clear()
        mock_nlu_val.update({"intent": "book_appointment", "entities": {"doctor_name": "Nobody Matching", "datetime": "2026-08-11"}})
        asyncio.run(db_mock.save_conversation_state("hs3", "choosing_search_mode", {"lang": "en", "city": "Kishanganj"}))
        asyncio.run(conversation.handle_message(mock_client, "hs3", "User", "text", "Nobody Matching se kal appointment"))
        state = asyncio.run(db_mock.get_conversation_state("hs3"))
        check(state["current_step"] == "choosing_search_mode", f"book_appointment miss should re-prompt, got {state['current_step']!r}")
        check("search_doctor_query" not in state["context"], "book_appointment miss should clear the abandoned query")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.nlu_client.classify_message = original_classify
        conversation.city_index.get_all_doctors = original_get_all_doctors
        conversation.city_index.get_index = original_get_index


def test_single_match_message_includes_full_details():
    """Task 1: the single-doctor-match message used to say only "Found matching doctor:
    X (Y)." — the specialty/hospital/distance/rating/fee were already in context but never
    reused for this message. Now reuses _doctor_row_description() directly."""
    import asyncio

    class MockDB:
        async def save_conversation_state(self, phone, step, context):
            pass

    original_db = conversation.db
    conversation.db = MockDB()
    sent = []

    async def mock_send_text(client, to, text):
        sent.append(text)

    original_send_text = conversation.whatsapp_client.send_text
    conversation.whatsapp_client.send_text = mock_send_text

    async def mock_advance(client, phone, context, booking):
        pass

    original_advance = conversation._advance_booking_flow
    conversation._advance_booking_flow = mock_advance

    doctor = {
        "doctorId": "d1", "fullName": "Dr. Avinash Kumar",
        "primaryMedicalSpecialityPatientFacingName": "Orthopaedic Surgeon",
        "hospitalName": "Purnea General Hospital", "city": "Purnea",
        "latitude": 25.78, "longitude": 87.47, "rating": 4.6, "discountedFee": 500,
    }
    try:
        asyncio.run(conversation._render_doctor_list(
            object(), "999", {"lang": "hg", "patient_lat": 25.80, "patient_lng": 87.48}, [doctor]
        ))
        check(len(sent) == 1, "single match should send exactly one message")
        msg = sent[0] if sent else ""
        check("Avinash" in msg, f"message should name the doctor, got {msg!r}")
        check("Orthopaedic" in msg, f"message should include the specialty, got {msg!r}")
        check("Purnea General Hospital" in msg, f"message should include the hospital, got {msg!r}")
        check("500" in msg, f"message should include the fee, got {msg!r}")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation._advance_booking_flow = original_advance


def test_doctor_too_many_matches_asks_location_instead_of_truncating():
    """Task 3: _render_doctor_list used to silently slice to [:10] with no signal that more
    matches existed. Now: >10 matches with no location known yet asks for location instead
    of showing a silently-truncated list. <=10 must still render normally (regression)."""
    import asyncio

    saved = {}

    class MockDB:
        async def save_conversation_state(self, phone, step, context):
            saved["step"] = step

    original_db = conversation.db
    conversation.db = MockDB()

    sent_loc_reqs, sent_lists = [], []

    async def mock_send_loc_req(client, to, text):
        sent_loc_reqs.append(text)

    async def mock_send_list(client, to, text, btn, rows, section="Options"):
        sent_lists.append(rows)

    original_send_loc = conversation.whatsapp_client.send_location_request
    original_send_list = conversation.whatsapp_client.send_list
    conversation.whatsapp_client.send_location_request = mock_send_loc_req
    conversation.whatsapp_client.send_list = mock_send_list

    try:
        docs_15 = [{"doctorId": f"d{i}", "fullName": f"Dr. Sharma {i}", "hospitalName": "H", "city": "C"} for i in range(15)]
        context = {"lang": "hg", "search_doctor_query": "Sharma"}
        asyncio.run(conversation._render_doctor_list(object(), "999", context, docs_15, "awaiting_doctor_name"))
        check(len(sent_loc_reqs) == 1, "15 matches with no location should ask for location once")
        check("15" in sent_loc_reqs[0], f"message should state the count, got {sent_loc_reqs[0]!r}")
        check(len(sent_lists) == 0, "must not show a truncated list when asking for location")
        check(saved.get("step") == "choosing_location", "should transition to choosing_location")

        sent_loc_reqs.clear(); sent_lists.clear(); saved.clear()
        docs_4 = [{"doctorId": f"d{i}", "fullName": f"Dr. X {i}", "hospitalName": "H", "city": "C"} for i in range(4)]
        asyncio.run(conversation._render_doctor_list(object(), "999", {"lang": "hg", "search_doctor_query": "X"}, docs_4, "awaiting_doctor_name"))
        check(len(sent_loc_reqs) == 0, "4 matches should not trigger a location ask (regression)")
        check(len(sent_lists) == 1 and len(sent_lists[0]) == 4, "4 matches should still show a normal list")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_location_request = original_send_loc
        conversation.whatsapp_client.send_list = original_send_list


def test_welcome_message_lists_actions():
    """Task 0: welcome_multilang used to only say "select a language" — now names what the
    bot can do, in the same message (no new message added)."""
    msg = i18n.t("welcome_multilang", None)
    check("Doctor search" in msg, f"welcome message should list doctor search, got {msg!r}")
    check("Symptom check" in msg, f"welcome message should list symptom check, got {msg!r}")
    check("Book appointment" in msg, f"welcome message should list booking, got {msg!r}")


def test_booking_success_invites_new_search():
    """Task 14: booked_queue_note used to end the conversation with no invitation to start
    again, unlike the cancelled message which already did. Now symmetric."""
    for lang in ("en", "hi", "hg", "bn"):
        msg = i18n.t("booked_queue_note", lang)
        check(len(msg.split("\n\n")) >= 2, f"booked_queue_note[{lang}] should have a closing invitation on its own line")


def test_symptom_and_specialty_open_with_one_combined_location_message():
    """Task 4 and 5: the sym_name/spec_name branches used to fall through to a generic,
    symptom/specialty-unaware location_prompt when location wasn't known yet. Now each sends
    exactly one personalised message (concern/enthusiasm + the specialty + location ask)."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

        async def log_nlu_interaction(self, **kw):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    original_router_db = conversation.intent_router.db
    conversation.db = db_mock
    conversation.intent_router.db = db_mock

    sent_texts, sent_loc_reqs = [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_loc_req(client, to, text):
        sent_loc_reqs.append(text)

    original_send_text = conversation.whatsapp_client.send_text
    original_send_loc = conversation.whatsapp_client.send_location_request
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_location_request = mock_send_loc_req

    async def mock_list_specialties():
        return [{"category": "General Physician"}, {"category": "Cardiologist (Heart)"}]

    async def mock_route_symptom(q):
        return ["General Physician"]

    original_list_specialties = conversation.hms_client.list_specialties
    original_route_symptom = conversation.symptom_client.route_symptom
    conversation.hms_client.list_specialties = mock_list_specialties
    conversation.symptom_client.route_symptom = mock_route_symptom

    original_classify = conversation.nlu_client.classify_message
    mock_client = object()

    try:
        mock_nlu_symptom = {"intent": "describe_symptom", "confidence": "high", "entities": {"symptom": "bahut tez bukhar hai"}}

        async def mock_classify_symptom(client, text):
            return mock_nlu_symptom

        conversation.nlu_client.classify_message = mock_classify_symptom
        asyncio.run(db_mock.save_conversation_state("cs1", None, {"lang": "hg"}))
        asyncio.run(conversation.handle_message(mock_client, "cs1", "User", "text", "bahut tez bukhar hai"))
        check(len(sent_texts) == 0, "symptom path should not send a plain text message")
        check(len(sent_loc_reqs) == 1, "symptom path should send exactly one location-request message")
        if sent_loc_reqs:
            check("General Physician" in sent_loc_reqs[0], f"should name the resolved specialty, got {sent_loc_reqs[0]!r}")

        sent_texts.clear(); sent_loc_reqs.clear()
        mock_nlu_specialty = {"intent": "check_availability", "confidence": "high", "entities": {"specialty": "cardiologist"}}

        async def mock_classify_specialty(client, text):
            return mock_nlu_specialty

        conversation.nlu_client.classify_message = mock_classify_specialty
        asyncio.run(db_mock.save_conversation_state("cs2", None, {"lang": "hg"}))
        asyncio.run(conversation.handle_message(mock_client, "cs2", "User", "text", "mujhe cardiologist ko dikhana hai"))
        check(len(sent_texts) == 0, "specialty path should not send a plain text message")
        check(len(sent_loc_reqs) == 1, "specialty path should send exactly one location-request message")
        if sent_loc_reqs:
            check("Cardiologist" in sent_loc_reqs[0], f"should name the resolved specialty, got {sent_loc_reqs[0]!r}")
    finally:
        conversation.db = original_db
        conversation.intent_router.db = original_router_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_location_request = original_send_loc
        conversation.hms_client.list_specialties = original_list_specialties
        conversation.symptom_client.route_symptom = original_route_symptom
        conversation.nlu_client.classify_message = original_classify


def test_radius_auto_widens_without_confirm_tap():
    """Task 6: once the widest configured band (50km) comes up empty, _send_doctor_list used
    to ask permission via buttons before searching further. Now it tells the patient and
    searches unrestricted immediately — no confirm tap, and no city filter."""
    import asyncio

    class MockDB:
        async def clear_conversation_state(self, phone):
            pass

    original_db = conversation.db
    conversation.db = MockDB()

    sent_texts, sent_buttons = [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons

    original_fetch_near = conversation._fetch_doctors_near
    original_safe_index = conversation._safe_city_index
    original_render = conversation._render_doctor_list
    original_list_doctors = conversation.hms_client.list_doctors

    async def mock_fetch_near_empty(specialty, context, radius, index, cache):
        return []

    async def mock_safe_index():
        return {"SomeCity": [[26.1, 87.9]]}

    rendered = {}

    async def mock_render(client, phone, context, doctors, current_step=None):
        rendered["doctors"] = doctors

    wide_results = [{"doctorId": "far1", "fullName": "Dr. Far"}]

    async def mock_list_doctors(specialty, page_size=50, city=None):
        check(city is None, "auto-widened search must not pass a city filter")
        return wide_results

    conversation._fetch_doctors_near = mock_fetch_near_empty
    conversation._safe_city_index = mock_safe_index
    conversation._render_doctor_list = mock_render
    conversation.hms_client.list_doctors = mock_list_doctors

    try:
        context = {"lang": "hg", "specialty_category": "Cardiologist (Heart)", "patient_lat": 26.10, "patient_lng": 87.95}
        asyncio.run(conversation._send_doctor_list(object(), "999", context))
        check(len(sent_buttons) == 0, "auto-widen must not show a search-wider confirm button")
        check(len(sent_texts) == 1, "auto-widen should send exactly one informational message")
        if sent_texts:
            check("50" in sent_texts[0], f"message should state the max radius, got {sent_texts[0]!r}")
        check(rendered.get("doctors") == wide_results, "should render the unrestricted search results")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation._fetch_doctors_near = original_fetch_near
        conversation._safe_city_index = original_safe_index
        conversation._render_doctor_list = original_render
        conversation.hms_client.list_doctors = original_list_doctors


def test_first_message_doctor_name_resolves_without_waiting_for_location():
    """Reported live bug: a fresh user's first message naming a doctor ("Dr Avinash k sath
    appointment book krna hai") showed the personalized single-match reply nowhere — after
    confirming the (low-confidence, romanized) detected language, the bot asked for location
    with no mention of the doctor at all.

    Root cause: _advance_booking_flow only looked at search_doctor_query once next_action()
    said "doctor" — which, per booking_slots.SLOT_ORDER (lang, location, doctor, ...), only
    happens after location is already filled. A name search that resolves without needing
    location (0/1/few matches) never got the chance to run first.

    Fix: search_doctor_query is checked before next_action() at all, and once a doctor
    resolves without location being known, location is marked satisfied directly (not via
    booking_slots.fill(), which would cascade-invalidate the doctor just resolved) so the
    flow doesn't then ask for location purely because of slot position."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

        async def log_nlu_interaction(self, **kw):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    original_router_db = conversation.intent_router.db
    conversation.db = db_mock
    conversation.intent_router.db = db_mock

    sent_texts, sent_buttons, sent_flows = [], [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))

    async def mock_send_flow(client, to, body_text, flow_id, flow_cta, screen_id, flow_token, initial_data=None):
        sent_flows.append(body_text)
        return True

    async def mock_send_location_request(client, to, text):
        raise AssertionError(f"location must not be requested for a single resolved match, got: {text!r}")

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_flow = conversation.whatsapp_client.send_flow
    original_send_loc_req = conversation.whatsapp_client.send_location_request
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_flow = mock_send_flow
    conversation.whatsapp_client.send_location_request = mock_send_location_request

    mock_nlu = {"intent": "book_appointment", "confidence": "high", "entities": {"doctor_name": "Avinash"}}

    async def mock_classify(client, text):
        return mock_nlu

    original_classify = conversation.nlu_client.classify_message
    conversation.nlu_client.classify_message = mock_classify

    async def mock_get_all_doctors(force_refresh=False):
        return [{
            "doctorId": "d1", "fullName": "Dr. Avinash Kumar", "hospitalName": "Purnea General Hospital",
            "city": "Kishanganj", "primaryMedicalSpecialityPatientFacingName": "General Physician",
            "rating": 4.5, "discountedFee": 400,
        }]

    original_get_all_doctors = conversation.city_index.get_all_doctors
    conversation.city_index.get_all_doctors = mock_get_all_doctors

    async def mock_get_index(*a, **kw):
        return {}
    original_get_index = conversation.city_index.get_index
    conversation.city_index.get_index = mock_get_index

    async def mock_get_offered_slots(doctor_id, lang):
        from datetime import date
        return [{"date": date(2026, 8, 12), "is_today": True, "shift_name": "Morning", "button_id": "slot_today_morning", "label": "Morning (Today)"}]

    original_get_offered_slots = conversation._get_offered_slots
    conversation._get_offered_slots = mock_get_offered_slots

    mock_client = object()
    try:
        # Turn 1: fresh user, first message, romanized text -> low-confidence detection,
        # confirmation shown (this part is correct and expected).
        asyncio.run(conversation.handle_message(mock_client, "fb1", "User", "text", "Dr Avinash k sath appointment book krna hai"))
        check(len(sent_buttons) == 1, "first message with low-confidence detection should show the confirm buttons")

        # Turn 2: patient taps Proceed.
        asyncio.run(conversation.handle_message(mock_client, "fb1", "User", "button_reply", "lang_confirm_yes"))
        step, ctx = db_mock.state.get("fb1", (None, {}))

        check(ctx.get("doctor_id") == "d1", f"doctor should resolve to the single match, got doctor_id={ctx.get('doctor_id')!r}")
        check(
            any("Avinash" in t for t in sent_texts),
            f"a message naming the resolved doctor should have been sent, got {sent_texts!r}",
        )
        check(len(sent_flows) == 1, "should proceed straight to the patient details Flow")
        check(step == "awaiting_patient_details", f"should land on awaiting_patient_details, got {step!r}")
    finally:
        conversation.db = original_db
        conversation.intent_router.db = original_router_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_flow = original_send_flow
        conversation.whatsapp_client.send_location_request = original_send_loc_req
        conversation.nlu_client.classify_message = original_classify
        conversation.city_index.get_all_doctors = original_get_all_doctors
        conversation.city_index.get_index = original_get_index
        conversation._get_offered_slots = original_get_offered_slots


def test_sarvam_language_confidence_upgrade_skips_confirm_step():
    """User-requested improvement: local keyword-based language detection (_detect_language)
    caps romanized/Hinglish guesses at low-confidence by design (word-overlap alone is a weak
    signal), which always shows the confirm-language buttons. But nlu_client.classify_message
    already reads the full message for intent/entity extraction on every text message (and now
    on the first message too) via the same Sarvam call -- if it also reports a confident
    language opinion, _confirm_or_start_language should use that to skip the redundant confirm
    step entirely, matching the earlier "agar bot khud hi language identify kr leta hai toh fr
    usee language choose krne ka option na ho" request. Script-based detection (Devanagari/
    Bengali) is untouched by this -- it was already high-confidence and free.

    This uses the exact same typo'd message as the "Dr Avinash" bug report ("Miujhe" instead of
    "Mujhe"), which previously always showed the confirm buttons -- this test locks in that it
    now skips them when Sarvam is confident, while a companion low-confidence case still shows
    them."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

        async def log_nlu_interaction(self, **kw):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    original_router_db = conversation.intent_router.db
    conversation.db = db_mock
    conversation.intent_router.db = db_mock

    sent_texts, sent_buttons, sent_flows = [], [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))

    async def mock_send_flow(client, to, body_text, flow_id, flow_cta, screen_id, flow_token, initial_data=None):
        sent_flows.append(body_text)
        return True

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_flow = conversation.whatsapp_client.send_flow
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_flow = mock_send_flow

    async def mock_get_all_doctors(force_refresh=False):
        return [{
            "doctorId": "d1", "fullName": "Dr. Avinash Kumar", "hospitalName": "Purnea General Hospital",
            "city": "Kishanganj", "primaryMedicalSpecialityPatientFacingName": "General Physician",
            "rating": 4.5, "discountedFee": 400,
        }]

    original_get_all_doctors = conversation.city_index.get_all_doctors
    conversation.city_index.get_all_doctors = mock_get_all_doctors

    async def mock_get_index(*a, **kw):
        return {}
    original_get_index = conversation.city_index.get_index
    conversation.city_index.get_index = mock_get_index

    async def mock_get_offered_slots(doctor_id, lang):
        return [{"date": date(2026, 8, 12), "is_today": True, "shift_name": "Morning", "button_id": "slot_today_morning", "label": "Morning (Today)"}]

    original_get_offered_slots = conversation._get_offered_slots
    conversation._get_offered_slots = mock_get_offered_slots

    mock_client = object()
    message_text = "Miujhe Dr Avinash k sath appointment book krni hai"

    original_classify = conversation.nlu_client.classify_message
    try:
        # Case A: Sarvam confidently reports the language -- confirm buttons must be skipped.
        async def mock_classify_high_conf(client, text):
            return {
                "intent": "book_appointment", "confidence": "high",
                "entities": {"doctor_name": "Avinash"},
                "detected_language": "hg", "language_confidence": "high",
            }
        conversation.nlu_client.classify_message = mock_classify_high_conf

        asyncio.run(conversation.handle_message(mock_client, "fb-sarvam-upgrade", "User", "text", message_text))

        check(len(sent_buttons) == 0, f"Sarvam high-confidence language should skip the confirm buttons, got {sent_buttons!r}")
        check(any("Avinash" in t for t in sent_texts), "should proceed straight to the resolved doctor's message")
        check(len(sent_flows) == 1, "should proceed straight to the patient details Flow")

        # Case B: Sarvam offers no opinion (or low confidence) -- confirm buttons must still show,
        # exactly as before this change (no regression on the ambiguous case).
        sent_texts.clear()
        sent_buttons.clear()
        sent_flows.clear()

        async def mock_classify_no_opinion(client, text):
            return {
                "intent": "book_appointment", "confidence": "high",
                "entities": {"doctor_name": "Avinash"},
                "detected_language": None, "language_confidence": None,
            }
        conversation.nlu_client.classify_message = mock_classify_no_opinion

        asyncio.run(conversation.handle_message(mock_client, "fb-sarvam-no-upgrade", "User", "text", message_text))

        check(len(sent_buttons) == 1, f"without a confident Sarvam opinion, the confirm buttons should still show, got {sent_buttons!r}")
    finally:
        conversation.db = original_db
        conversation.intent_router.db = original_router_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_flow = original_send_flow
        conversation.nlu_client.classify_message = original_classify
        conversation.city_index.get_all_doctors = original_get_all_doctors
        conversation.city_index.get_index = original_get_index
        conversation._get_offered_slots = original_get_offered_slots


def test_first_message_symptom_resolves_to_combined_message_not_generic_location_prompt():
    """Live-reported gap, same shape as the earlier "Dr Avinash" bug but for symptom/specialty
    instead of doctor-name: a fresh user's very first message describing a symptom ("bahut tez
    bukhar hai") got matched to a specialty correctly (the NLP route_symptom call did happen),
    but once it was that pending specialty's turn in _advance_booking_flow's slot walk, the
    code only knew the slot name was "location" -- so it fell through to the GENERIC location
    prompt via _step_for_action/_trigger_step_prompt, silently dropping the specialty/concern
    context, instead of the personalised Task 4 combined message
    (symptom_concern_and_location_ask) the has-lang-already version of this same flow already
    sent correctly (see test_symptom_and_specialty_open_with_one_combined_location_message,
    which seeds lang up front and so never exercises this path).

    Fix: _advance_booking_flow now checks for a pending specialty/symptom before deferring to
    next_action()'s generic slot order, same as it already does for a pending doctor-name
    search."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

        async def log_nlu_interaction(self, **kw):
            pass

    db_mock = MockDB()
    original_db = conversation.db
    original_router_db = conversation.intent_router.db
    conversation.db = db_mock
    conversation.intent_router.db = db_mock

    sent_texts, sent_buttons, sent_loc_reqs = [], [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_buttons(client, to, text, buttons):
        sent_buttons.append((text, buttons))

    async def mock_send_loc_req(client, to, text):
        sent_loc_reqs.append(text)

    original_send_text = conversation.whatsapp_client.send_text
    original_send_buttons = conversation.whatsapp_client.send_buttons
    original_send_loc = conversation.whatsapp_client.send_location_request
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_buttons = mock_send_buttons
    conversation.whatsapp_client.send_location_request = mock_send_loc_req

    async def mock_list_specialties():
        return [{"category": "General Physician"}, {"category": "Cardiologist (Heart)"}]

    async def mock_route_symptom(q):
        return ["General Physician"]

    original_list_specialties = conversation.hms_client.list_specialties
    original_route_symptom = conversation.symptom_client.route_symptom
    conversation.hms_client.list_specialties = mock_list_specialties
    conversation.symptom_client.route_symptom = mock_route_symptom

    original_classify = conversation.nlu_client.classify_message
    mock_client = object()

    try:
        # Sarvam confidently reports both the intent/symptom AND the language on this single
        # first message, so this test isolates the specialty-first-message bug rather than
        # also re-exercising the separate low-confidence confirm-buttons dance.
        async def mock_classify_symptom(client, text):
            return {
                "intent": "describe_symptom", "confidence": "high",
                "entities": {"symptom": "bahut tez bukhar hai"},
                "detected_language": "hg", "language_confidence": "high",
            }
        conversation.nlu_client.classify_message = mock_classify_symptom

        asyncio.run(conversation.handle_message(mock_client, "fb-symptom-first", "User", "text", "bahut tez bukhar hai"))

        check(len(sent_buttons) == 0, "high-confidence language should skip the confirm buttons")
        # One send_text is expected here -- the "welcome_banner" greeting _confirm_or_start_language
        # sends once a high-confidence language resolves. The bug this test guards against is a
        # SECOND text message: the generic, specialty-unaware location_prompt template.
        generic_location_prompt = i18n.t("location_prompt", "hg")
        check(
            all(t != generic_location_prompt for t in sent_texts),
            f"must not fall back to the generic plain-text location prompt, got {sent_texts!r}",
        )
        check(len(sent_loc_reqs) == 1, f"should send exactly one combined location-request message, got {sent_loc_reqs!r}")
        if sent_loc_reqs:
            check("General Physician" in sent_loc_reqs[0], f"should name the resolved specialty, got {sent_loc_reqs[0]!r}")
            check("fikar" in sent_loc_reqs[0] or "concerning" in sent_loc_reqs[0].lower() or "চিন্তা" in sent_loc_reqs[0],
                  f"should use the symptom-concern framing (not the plain specialty-enthusiasm one), got {sent_loc_reqs[0]!r}")

        step, ctx = db_mock.state.get("fb-symptom-first", (None, {}))
        check(step == "choosing_location", f"should be waiting on location next, got {step!r}")
        check(ctx.get("pending_specialty") == "General Physician", "pending_specialty must survive for after location resolves")
    finally:
        conversation.db = original_db
        conversation.intent_router.db = original_router_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_buttons = original_send_buttons
        conversation.whatsapp_client.send_location_request = original_send_loc
        conversation.hms_client.list_specialties = original_list_specialties
        conversation.symptom_client.route_symptom = original_route_symptom
        conversation.nlu_client.classify_message = original_classify


def test_awaiting_symptom_step_announces_specialty_before_sort():
    """Live-reported gap: the "Describe symptoms" button flow (choosing_search_mode ->
    awaiting_symptom, i.e. _handle_awaiting_symptom) matched the typed symptom to a specialty
    correctly, but jumped straight to the generic sort-prompt list ("Doctor list kis basis par
    dikhayein?") with zero mention of what specialty it matched to or any concern
    acknowledgment -- unlike the NLU-shortcut symptom/specialty paths (Task 4/5), which always
    named the specialty. This step is reached only after choosing_location has already passed,
    so there's no location to ask for here -- the fix is to fold a concern/enthusiasm sentence
    naming the specialty into the SAME sort-prompt list message via _send_sort_prompt's new
    concern_prefix, not to send it as a second message."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_lists = []

    async def mock_send_list(client, to, text, button_label, rows, section_title="Options"):
        sent_lists.append(text)

    original_send_list = conversation.whatsapp_client.send_list

    async def mock_list_specialties():
        return [{"category": "General Physician"}, {"category": "Cardiologist (Heart)"}]

    async def mock_route_symptom(q):
        return ["General Physician"]

    original_list_specialties = conversation.hms_client.list_specialties
    original_route_symptom = conversation.symptom_client.route_symptom
    conversation.whatsapp_client.send_list = mock_send_list
    conversation.hms_client.list_specialties = mock_list_specialties
    conversation.symptom_client.route_symptom = mock_route_symptom

    mock_client = object()
    try:
        # Location already known (as it would be by the time this step is reached in the real
        # flow -- choosing_search_mode only follows choosing_location).
        context = {"lang": "hg", "patient_lat": 26.11, "patient_lng": 87.55}
        asyncio.run(conversation._handle_awaiting_symptom(mock_client, "aw1", "text", "bahut tez bukhar hai", context))

        check(len(sent_lists) == 1, f"should send exactly one combined message, got {len(sent_lists)}")
        if sent_lists:
            check("General Physician" in sent_lists[0], f"should name the matched specialty, got {sent_lists[0]!r}")
            check("fikar" in sent_lists[0], f"should use the symptom-concern framing, got {sent_lists[0]!r}")
            check(i18n.t("sort_prompt", "hg") in sent_lists[0], "should still include the sort-prompt question in the same message")
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_list = original_send_list
        conversation.hms_client.list_specialties = original_list_specialties
        conversation.symptom_client.route_symptom = original_route_symptom


def test_guardian_is_optional_in_patient_details():
    """User-reported mismatch: the WhatsApp Flow form already marks Guardian as an optional
    field, but _handle_awaiting_patient_details rejected a submission with no guardian as
    invalid on both the Flow (nfm_reply) and free-text paths -- code was stricter than the
    form it's validating. Guardian was never actually required downstream (_patient_line
    already does `if guardian: ...`, hms_client never receives it, db.py's
    create_pending_appointment already types it as str | None), so the fix is purely relaxing
    this one validation check plus the free-text parser accepting a 3rd-field-less line."""
    import asyncio
    import json

    sent_texts = []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_advance_booking_flow(client, phone, context, booking):
        pass  # isolate the parsing/validation being tested from the rest of the booking flow

    async def mock_send_patient_details_flow(client, phone, context):
        pass  # isolate from the re-prompt Flow/buttons rendering, not what's under test here

    original_send_text = conversation.whatsapp_client.send_text
    original_advance = conversation._advance_booking_flow
    original_send_flow_prompt = conversation._send_patient_details_flow
    conversation.whatsapp_client.send_text = mock_send_text
    conversation._advance_booking_flow = mock_advance_booking_flow
    conversation._send_patient_details_flow = mock_send_patient_details_flow

    mock_client = object()
    try:
        # Free-text path: "Name, Age, Gender" with no 4th (guardian) part.
        context = {"lang": "en"}
        asyncio.run(conversation._handle_awaiting_patient_details(mock_client, "p1", "text", "Riya, 8, Female", context))
        check(not sent_texts, f"3-field details (no guardian) should be accepted, got {sent_texts!r}")
        check(context.get("patient_display_name") == "Riya", "name should still be captured")
        check(context.get("patient_guardian") == "", f"guardian should be empty, not missing, got {context.get('patient_guardian')!r}")

        # Flow (nfm_reply) path: guardian submitted as an empty string, as the Flow would send
        # when its optional field is left blank.
        sent_texts.clear()
        context2 = {"lang": "en"}
        flow_payload = json.dumps({"name": "Aman", "age": "40", "gender": "Male", "guardian": ""})
        asyncio.run(conversation._handle_awaiting_patient_details(mock_client, "p2", "nfm_reply", flow_payload, context2))
        check(not sent_texts, f"Flow submission with blank guardian should be accepted, got {sent_texts!r}")
        check(context2.get("patient_display_name") == "Aman", "name should still be captured from the Flow")

        # Guardian, when actually given, must still be captured correctly (not silently
        # dropped by this change).
        sent_texts.clear()
        context3 = {"lang": "en"}
        asyncio.run(conversation._handle_awaiting_patient_details(mock_client, "p3", "text", "Riya, 8, Female, Rajesh", context3))
        check(context3.get("patient_guardian") == "Rajesh", f"guardian should still be captured when given, got {context3.get('patient_guardian')!r}")

        # Name/age/gender remain required -- this isn't a blanket relaxation.
        sent_texts.clear()
        context4 = {"lang": "en"}
        asyncio.run(conversation._handle_awaiting_patient_details(mock_client, "p4", "text", "Riya, 8", context4))
        check(len(sent_texts) == 1, f"missing gender should still be rejected, got {sent_texts!r}")
    finally:
        conversation.whatsapp_client.send_text = original_send_text
        conversation._advance_booking_flow = original_advance
        conversation._send_patient_details_flow = original_send_flow_prompt


def test_unresolved_location_reprompts_instead_of_silently_advancing():
    """Live-reported bug: a generic booking request with no doctor/symptom/specialty named
    (e.g. "Mujhe kal subah appointment chahiye") correctly asks for location. But when the
    typed city fails to resolve (e.g. "kishaganj", missing the 'n' in "Kishanganj" -- city_index
    has no fuzzy/typo tolerance, see match_typed_city), the conversation silently jumped straight
    to "Aap symptom ke hisaab se search karna chahte hain ya doctor ke naam se?" (choosing_search_mode)
    with NO "couldn't find that city" message at all -- the patient had no idea their location
    wasn't understood. Later, once a symptom/specialty search actually needed a location, the bot
    asked for it again, which read as a confusing duplicate ask for something already answered.

    Root cause: booking_slots.next_action() correctly returns ("retry", "location") when the
    location slot is marked notfound, but _step_for_action had no case for action=="retry" with
    slot_name=="location" -- it fell through every other elif and hit the function's default
    return of "choosing_search_mode". _trigger_step_prompt's own "choosing_location" branch
    already knows how to send the "couldn't find that city" message for a notfound status (see
    app/conversation.py's step == "choosing_location" case) -- it just was never reached.

    Fix: _step_for_action now routes ("retry", "location") back to "choosing_location" so that
    existing notfound-messaging actually fires before re-asking, instead of masquerading as if
    the location had been accepted."""
    import asyncio

    class MockDB:
        def __init__(self):
            self.state = {}

        async def get_conversation_state(self, phone):
            if phone in self.state:
                step, ctx = self.state[phone]
                return {"current_step": step, "context": ctx}
            return None

        async def save_conversation_state(self, phone, step, context):
            self.state[phone] = (step, context)

        async def clear_conversation_state(self, phone):
            self.state.pop(phone, None)

    db_mock = MockDB()
    original_db = conversation.db
    conversation.db = db_mock

    sent_texts, sent_loc_reqs = [], []

    async def mock_send_text(client, to, text):
        sent_texts.append(text)

    async def mock_send_loc_req(client, to, text):
        sent_loc_reqs.append(text)

    original_send_text = conversation.whatsapp_client.send_text
    original_send_loc = conversation.whatsapp_client.send_location_request
    conversation.whatsapp_client.send_text = mock_send_text
    conversation.whatsapp_client.send_location_request = mock_send_loc_req

    mock_client = object()
    try:
        # Fresh clipboard, lang already known, sitting on choosing_location -- exactly the
        # state a generic "Mujhe kal subah appointment chahiye" request leaves the patient in.
        booking = conversation.booking_slots.empty()
        conversation.booking_slots.fill(booking, "lang", "hg", source="user")
        context = {"lang": "hg", "booking": booking, "current_step": "choosing_location"}

        asyncio.run(conversation._handle_choosing_location(mock_client, "loc1", "text", "kishaganj", context))

        check(
            any("kishaganj" in t and ("nahi mili" in t or "couldn't find" in t.lower()) for t in sent_texts),
            f"should tell the patient their city wasn't recognised, got sent_texts={sent_texts!r}",
        )
        check(len(sent_loc_reqs) == 1, f"should re-ask for location (not silently move on), got {sent_loc_reqs!r}")

        step, ctx = db_mock.state.get("loc1", (None, {}))
        check(step == "choosing_location", f"must stay on choosing_location for a retry, got {step!r}")
        check(
            ctx.get("booking", {}).get("location", {}).get("status") == "notfound",
            "clipboard should record the failed match as notfound, not silently filled or blank",
        )
    finally:
        conversation.db = original_db
        conversation.whatsapp_client.send_text = original_send_text
        conversation.whatsapp_client.send_location_request = original_send_loc


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        test()
        print(f"  ran {test.__name__}")
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print(f"PASSED — {len(tests)} checks, no truncation or coverage gaps")
