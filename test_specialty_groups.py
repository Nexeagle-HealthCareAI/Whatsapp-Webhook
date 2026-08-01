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

os.environ.setdefault("WHATSAPP_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "test")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "test")
os.environ.setdefault("WHATSAPP_APP_SECRET", "test")
os.environ.setdefault("SQLSERVER_CONN_STRING", "test")
os.environ.setdefault("INTERNAL_EVENTS_TOKEN", "test")

from app import city_index, conversation, i18n  # noqa: E402
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


def test_language_detection():
    detect = conversation._detect_language

    # 1. Generic greetings (open option / None)
    check(detect("hi") is None, "single word hi should be open option")
    check(detect("hello") is None, "single word hello should be open option")
    check(detect("hey") is None, "single word hey should be open option")
    check(detect("  hey  ") is None, "whitespace-padded hey should be open option")
    check(detect("Hlo") is None, "Capitalized hlo should be open option")

    # 2. English (en) with spelling variations / typos
    check(detect("hey i have to book an appointment") == "en", "standard english appointment booking should be en")
    check(detect("need to bok an apointmint") == "en", "english with typos bok/apointmint should be en")
    check(detect("dr appontment") == "en", "english with typos dr/appontment should be en")
    check(detect("download prescribtion") == "en", "prescription download english should be en")
    check(detect("get medicine list") == "en", "medicine list english should be en")

    # 3. Hinglish (hg) with spelling variations / typos
    check(detect("hi mujhe appointment book krna hai") == "hg", "standard hinglish appointment booking should be hg")
    check(detect("muje appontment buk krna h") == "hg", "hinglish with typos muje/buk/h should be hg")
    check(detect("apointment book krna he") == "hg", "hinglish with typos apointment/he should be hg")
    check(detect("mje dr dikhao") == "hg", "hinglish with typo mje and dikhao should be hg")
    check(detect("parcha download krna h") == "hg", "prescription download in hinglish should be hg")
    check(detect("preskripsion downlod krna hai") == "hg", "prescription download with typos in hinglish should be hg")

    # 4. Hindi Devanagari (hi) with spelling variations / typos
    check(detect("मुझे अपॉइंटमेंट बुक करना है") == "hi", "hindi devanagari should be hi")
    check(detect("अपोइंटमेंट बुक करना है") == "hi", "hindi devanagari with typo अपोइंटमेंट should be hi")
    check(detect("डॉक्टर बुक करे") == "hi", "hindi devanagari doctor should be hi")
    check(detect("दवा पर्ची डाउनलोड") == "hi", "hindi devanagari prescription download should be hi")

    # 5. Bengali (bn) with spelling variations / typos
    check(detect("আমি একটা অ্যাপয়েন্টমেন্ট বুক করতে চাই") == "bn", "bengali should be bn")
    check(detect("ডাক্তার বুকিং করতে চাই") == "bn", "bengali doctor booking should be bn")
    check(detect("প্রেসক্রিপশন ডাউনলোড") == "bn", "bengali prescription download should be bn")
    check(detect("প্রেসক্রিপসন ডাউনলোড করতে চাই") == "bn", "bengali prescription typo should be bn")

    # 6. Gibberish / Unknown
    check(detect("xyz abc") is None, "gibberish should trigger open option")


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
