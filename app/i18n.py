"""
app/i18n.py
Three-language string table for the conversation flow: English (en), Hindi (hi, Devanagari),
Hinglish (hg, romanised — matching the tone already used in this project's other bot copy,
e.g. "Aapki report ready hote hi yahin bhej di jayegi.").

Design note: this is a plain dict, not a full i18n framework (gettext, babel, etc.) —
deliberately KISS, matching this project's stated preference elsewhere ("KISS first, add
complexity only when there's a concrete need"). ~40 keys is small enough that a dict is the
simplest thing that works; revisit only if the string count grows much larger or a translator
workflow (as opposed to hand-editing this file) becomes necessary.

`lang` is always one of "en" / "hi" / "hg". Any code path that doesn't yet know the patient's
choice (i.e. before LANG_PROMPT is answered) must not call t() — LANG_PROMPT itself is the one
message that has no language variant, since it's what solicits the language in the first place.
"""

DEFAULT_LANG = "en"

LANGUAGE_LABELS = {"en": "English", "hi": "हिंदी", "hg": "Hinglish"}

# ---------------------------------------------------------------------------------------
# Specialty grouping — why this exists:
#
# GET /public/specialties returns 30 categories. A WhatsApp interactive list holds at most
# 10 rows TOTAL (Meta's limit, across all sections). Sending 30 doesn't error — WhatsApp
# silently keeps the first 10 and drops the rest, which alphabetically meant Gynaecologist,
# Paediatrician, Orthopaedic Surgeon and 17 others were unreachable when browsing.
#
# So: browse is two short lists instead of one long truncated one. Groups are organised by
# what a patient FEELS ("bones & joints"), not by medical taxonomy, and each row carries an
# examples line — someone with knee pain shouldn't need to know the word "Orthopaedic".
#
# Two invariants, both asserted in the group-coverage check:
#   - len(SPECIALTY_GROUPS) + 1 (for Other) must stay <= 10, so the group list itself fits.
#   - no single group may exceed 10 categories, so the second list fits too.
# "categories" entries are matched against the LIVE category strings from the API, so a
# typo here means that specialty quietly lands in Other rather than disappearing.
# ---------------------------------------------------------------------------------------

SPECIALTY_GROUPS = [
    {
        "id": "grp_general",
        "categories": [
            "General Physician",
            "Endocrinologist (Hormones/Diabetes)",
            "Geriatrician",
            "Emergency Medicine Specialist",
        ],
        "title": {"en": "🩺 Everyday health", "hi": "🩺 रोज़मर्रा की सेहत", "hg": "🩺 Rozmarra ki sehat"},
        "desc": {
            "en": "Fever, cough, BP, sugar, check-up",
            "hi": "बुखार, खांसी, बीपी, शुगर, जांच",
            "hg": "Bukhar, khaansi, BP, sugar, check-up",
        },
    },
    {
        "id": "grp_women_children",
        "categories": ["Gynaecologist", "Paediatrician"],
        "title": {"en": "👶 Women & children", "hi": "👶 महिला और बच्चे", "hg": "👶 Mahila aur bachche"},
        "desc": {
            "en": "Pregnancy, periods, child health",
            "hi": "गर्भावस्था, पीरियड्स, बच्चों की सेहत",
            "hg": "Pregnancy, periods, bachchon ki sehat",
        },
    },
    {
        "id": "grp_bones",
        "categories": [
            "Orthopaedic Surgeon (Bone)",
            "Rheumatologist",
            "Physiotherapist / Rehab",
            "Sports Medicine Specialist",
        ],
        "title": {"en": "🦴 Bones & joints", "hi": "🦴 हड्डी और जोड़", "hg": "🦴 Haddi aur jod"},
        "desc": {
            "en": "Back pain, knee pain, fracture, sprain",
            "hi": "कमर दर्द, घुटने का दर्द, फ्रैक्चर, मोच",
            "hg": "Kamar dard, ghutne ka dard, fracture",
        },
    },
    {
        "id": "grp_eyes_ent_skin",
        "categories": ["Ophthalmologist (Eye)", "ENT Specialist", "Dermatologist (Skin)"],
        "title": {"en": "👁️ Eye, ENT & skin", "hi": "👁️ आंख, कान, त्वचा", "hg": "👁️ Aankh, ENT, skin"},
        "desc": {
            "en": "Eyesight, ear pain, throat, rashes",
            "hi": "नज़र, कान दर्द, गला, चर्म रोग",
            "hg": "Nazar, kaan dard, gala, skin problem",
        },
    },
    {
        "id": "grp_stomach_kidney",
        "categories": [
            "Gastroenterologist",
            "GI/Surgical Gastroenterologist",
            "Nephrologist (Kidney)",
            "Urologist",
        ],
        "title": {"en": "💧 Stomach & kidney", "hi": "💧 पेट और किडनी", "hg": "💧 Pet aur kidney"},
        "desc": {
            "en": "Acidity, stomach pain, urine, stones",
            "hi": "एसिडिटी, पेट दर्द, पेशाब, पथरी",
            "hg": "Acidity, pet dard, peshab, pathri",
        },
    },
    {
        "id": "grp_heart_chest",
        "categories": ["Cardiologist (Heart)", "Pulmonologist (Chest/Lungs)", "Cardiothoracic Surgeon"],
        "title": {"en": "❤️ Heart & chest", "hi": "❤️ दिल और छाती", "hg": "❤️ Dil aur chhaati"},
        "desc": {
            "en": "Chest pain, breathing trouble, asthma",
            "hi": "सीने में दर्द, सांस की तकलीफ, अस्थमा",
            "hg": "Seene mein dard, saans ki takleef, asthma",
        },
    },
    {
        "id": "grp_brain_mind",
        "categories": ["Neurologist", "Neurosurgeon", "Psychiatrist"],
        "title": {"en": "🧠 Brain & mind", "hi": "🧠 दिमाग और मन", "hg": "🧠 Dimaag aur mann"},
        "desc": {
            "en": "Headache, fits, memory, stress, sleep",
            "hi": "सिरदर्द, दौरे, याददाश्त, तनाव, नींद",
            "hg": "Sirdard, daure, yaaddasht, stress, neend",
        },
    },
    {
        "id": "grp_surgery",
        "categories": ["General Surgeon", "Plastic Surgeon", "Vascular Surgeon", "Oncologist (Cancer)"],
        "title": {"en": "🏥 Surgery & cancer", "hi": "🏥 सर्जरी और कैंसर", "hg": "🏥 Surgery aur cancer"},
        "desc": {
            "en": "Operations, cancer treatment, veins",
            "hi": "ऑपरेशन, कैंसर का इलाज, नसें",
            "hg": "Operation, cancer ka ilaaj, nasein",
        },
    },
    {
        "id": "grp_tests",
        "categories": ["Radiologist", "Pathologist", "Anaesthesiologist"],
        "title": {"en": "🔬 Tests & scans", "hi": "🔬 जांच और स्कैन", "hg": "🔬 Jaanch aur scan"},
        "desc": {
            "en": "X-ray, sonography, lab reports",
            "hi": "एक्स-रे, सोनोग्राफी, लैब रिपोर्ट",
            "hg": "X-ray, sonography, lab report",
        },
    },
]

# Catch-all for any live category not listed above — e.g. if 1HMS adds a new specialty
# tomorrow, it shows up here instead of silently vanishing (which is exactly the failure
# mode this whole grouping exists to fix). Only offered when it actually has something in it.
OTHER_GROUP = {
    "id": "grp_other",
    "title": {"en": "➕ Other specialities", "hi": "➕ अन्य विशेषज्ञ", "hg": "➕ Aur specialities"},
    "desc": {
        "en": "Anything not listed above",
        "hi": "जो ऊपर नहीं दिखा",
        "hg": "Jo upar nahi dikha",
    },
}


def group_label(group: dict, lang: str | None) -> tuple[str, str]:
    """(title, description) for a group row, in the patient's language."""
    lang = lang or DEFAULT_LANG
    title = group["title"].get(lang) or group["title"][DEFAULT_LANG]
    desc = group["desc"].get(lang) or group["desc"][DEFAULT_LANG]
    return title, desc

# The very first message of any conversation — necessarily shown in all three at once,
# since we don't know the patient's preference yet.
LANG_PROMPT = (
    "👋 Welcome! I can help you book a doctor's appointment.\n"
    "भाषा चुनें / Please choose a language / Language choose kar lijiye:"
)

_STRINGS: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "Great, I'll continue in English. Who is this appointment for?",
        "hi": "ठीक है, अब मैं हिंदी में बात करूँगा। यह अपॉइंटमेंट किसके लिए है?",
        "hg": "Theek hai, ab Hinglish mein baat karte hain. Yeh appointment kiske liye hai?",
    },
    "person_prompt": {
        "en": "Who are you booking for?",
        "hi": "आप किसके लिए बुकिंग कर रहे हैं?",
        "hg": "Kiske liye book kar rahe hain?",
    },
    # Button titles are capped at 20 chars by WhatsApp (_MAX_BUTTON_TITLE) — these are kept
    # short deliberately so they never render half-cut ("Family member ke liy").
    "person_self": {"en": "For myself", "hi": "खुद के लिए", "hg": "Khud ke liye"},
    "person_family": {"en": "For family", "hi": "परिवार के लिए", "hg": "Family ke liye"},
    "person_choose_hint": {
        "en": "Please tap one of the options above.",
        "hi": "कृपया ऊपर दिए गए विकल्पों में से एक चुनें।",
        "hg": "Please upar diye options mein se ek choose kar lijiye.",
    },
    "you": {"en": "You", "hi": "आप", "hg": "Aap"},
    "clinic_unknown": {"en": "Clinic", "hi": "क्लिनिक", "hg": "Clinic"},
    "self_details_prompt": {
        "en": "Please send your name and age — e.g. 'Aquib, 32'.",
        "hi": "कृपया अपना नाम और उम्र भेजें — जैसे 'अकीब, 32'।",
        "hg": "Apna naam aur age bhejein — jaise 'Aquib, 32'.",
    },
    "self_details_invalid": {
        "en": "Please send it as: Name, Age (e.g. 'Aquib, 32').",
        "hi": "कृपया इस तरह भेजें: नाम, उम्र (जैसे 'अकीब, 32')।",
        "hg": "Is format mein bhejein: Naam, Age (jaise 'Aquib, 32').",
    },
    "age_invalid": {
        "en": "That age doesn't look right — please send a number of years, e.g. 32.",
        "hi": "यह उम्र सही नहीं लग रही — कृपया वर्षों में संख्या भेजें, जैसे 32।",
        "hg": "Ye age sahi nahi lag rahi — saalon mein number bhejein, jaise 32.",
    },
    "family_details_prompt": {
        "en": "Please send the patient's name, age, and relation to you — e.g. 'Riya, 8, Daughter'.",
        "hi": "कृपया मरीज़ का नाम, उम्र और आपसे रिश्ता भेजें — जैसे 'रिया, 8, बेटी'।",
        "hg": "Patient ka naam, age, aur aapse relation bhejein — jaise 'Riya, 8, Daughter'.",
    },
    "family_details_invalid": {
        "en": "Please send it as: Name, Age, Relation (e.g. 'Riya, 8, Daughter').",
        "hi": "कृपया इस तरह भेजें: नाम, उम्र, रिश्ता (जैसे 'रिया, 8, बेटी')।",
        "hg": "Is format mein bhejein: Naam, Age, Relation (jaise 'Riya, 8, Daughter').",
    },
    "location_prompt": {
        "en": "To show doctors near you, please share your location — tap below, it fills in from your phone's GPS automatically.",
        "hi": "आपके पास के डॉक्टर दिखाने के लिए, कृपया अपनी लोकेशन शेयर करें — नीचे टैप करें, यह आपके फोन के GPS से अपने आप भर जाएगी।",
        "hg": "Aapke paas ke doctors dikhane ke liye, apni location share kar dijiye — neeche tap karein, phone ke GPS se automatic fill ho jayegi.",
    },
    "location_manual_hint": {
        "en": "Or just type your city/area name instead.",
        "hi": "या फिर अपने शहर/इलाके का नाम टाइप कर दें।",
        "hg": "Ya phir apne city/area ka naam type kar dijiye.",
    },
    "search_mode_prompt": {
        "en": "How would you like to find a doctor?",
        "hi": "आप डॉक्टर कैसे खोजना चाहेंगे?",
        "hg": "Doctor kaise dhundna chahenge?",
    },
    "search_mode_symptom": {"en": "Describe symptoms", "hi": "लक्षण बताएं", "hg": "Symptoms bataayein"},
    "search_mode_browse": {"en": "Browse specialties", "hi": "विशेषज्ञता देखें", "hg": "Specialty dekhein"},
    "symptom_ask": {
        "en": "Sure — describe what's bothering you (e.g. 'chest pain and shortness of breath').",
        "hi": "ठीक है — बताएं क्या तकलीफ़ है (जैसे 'सीने में दर्द और सांस फूलना')।",
        "hg": "Theek hai — bataiye kya problem hai (jaise 'chest pain aur saans phoolna').",
    },
    "symptom_text_required": {
        "en": "Please describe your symptoms as text.",
        "hi": "कृपया अपने लक्षण टेक्स्ट में लिखें।",
        "hg": "Apne symptoms text mein likh dijiye.",
    },
    "symptom_no_match": {
        "en": "I couldn't confidently match that to a specialty — here's the full list instead:",
        "hi": "मैं इसे किसी विशेषज्ञता से पक्के तौर पर नहीं जोड़ पाया — इसके बजाय पूरी सूची यहां है:",
        "hg": "Main isko kisi specialty se confidently match nahi kar paaya — poori list yeh rahi:",
    },
    "symptom_matched": {
        "en": "That sounds like a job for a {category}.",
        "hi": "यह {category} के काम जैसा लगता है।",
        "hg": "Yeh {category} ka kaam lagta hai.",
    },
    "no_specialties": {
        "en": "Sorry, no doctors are available for booking right now. Please try later.",
        "hi": "क्षमा करें, अभी बुकिंग के लिए कोई डॉक्टर उपलब्ध नहीं है। कृपया बाद में कोशिश करें।",
        "hg": "Sorry, abhi booking ke liye koi doctor available nahi hai. Baad mein try karein.",
    },
    # Deliberately not "Which specialty are you looking for?" — most patients don't think
    # in specialty names, and being asked to is what makes a chat feel like a form. Ask for
    # the rough area instead, in plain words, and say out loud that it's not a form.
    "specialty_group_prompt": {
        "en": "No long forms here 🙂 Just tell me roughly what it's about — I'll find the right doctor.",
        "hi": "यहां कोई लंबा फॉर्म नहीं है 🙂 बस मोटे तौर पर बता दीजिए किस बारे में है — सही डॉक्टर मैं ढूंढ दूंगा।",
        "hg": "Yahan koi lamba form nahi hai 🙂 Bas mote taur par bata dijiye kis baare mein hai — sahi doctor main dhoond dunga.",
    },
    "specialty_group_button": {"en": "Pick an area", "hi": "क्षेत्र चुनें", "hg": "Area choose karein"},
    "specialty_group_section": {"en": "Areas", "hi": "क्षेत्र", "hg": "Areas"},
    "specialty_group_choose_hint": {
        "en": "Please pick one of the areas above — or just describe how you're feeling and I'll work it out.",
        "hi": "कृपया ऊपर दिए क्षेत्रों में से एक चुनें — या बस बता दें कि क्या तकलीफ़ है, मैं समझ लूंगा।",
        "hg": "Upar diye areas mein se ek choose karein — ya bas bata dijiye kya takleef hai, main samajh lunga.",
    },
    "specialty_list_prompt": {
        "en": "Good — which of these fits best?",
        "hi": "ठीक है — इनमें से कौन सा सबसे सही रहेगा?",
        "hg": "Theek hai — inmein se kaun sa sabse sahi rahega?",
    },
    "specialty_list_button": {"en": "Choose specialty", "hi": "विशेषज्ञता चुनें", "hg": "Specialty chunein"},
    "specialty_choose_hint": {
        "en": "Please choose a specialty from the list above.",
        "hi": "कृपया ऊपर सूची में से एक विशेषज्ञता चुनें।",
        "hg": "Upar list mein se specialty choose kar lijiye.",
    },
    "sort_prompt": {
        "en": "How should I sort the doctor list?",
        "hi": "डॉक्टरों की सूची किस आधार पर दिखाऊं?",
        "hg": "Doctor list kis basis par dikhayein?",
    },
    "sort_button": {"en": "Choose sort order", "hi": "क्रम चुनें", "hg": "Sort karein"},
    "sort_rating": {"en": "Top rated", "hi": "सर्वश्रेष्ठ रेटिंग", "hg": "Top rated"},
    "sort_nearest": {"en": "Nearest first", "hi": "सबसे नज़दीक", "hg": "Sabse nazdeek"},
    "sort_experience": {"en": "Most experienced", "hi": "सबसे अनुभवी", "hg": "Sabse experienced"},
    "sort_fee": {"en": "Lowest fee", "hi": "सबसे कम फीस", "hg": "Sabse kam fees"},
    "sort_choose_hint": {
        "en": "Please choose one of the sort options above.",
        "hi": "कृपया ऊपर दिए क्रम विकल्पों में से एक चुनें।",
        "hg": "Upar diye sort options mein se ek choose kar lijiye.",
    },
    "no_doctors": {
        "en": "Sorry, no doctors are currently available in that specialty. Please type 'hi' to start over.",
        "hi": "क्षमा करें, अभी इस विशेषज्ञता में कोई डॉक्टर उपलब्ध नहीं है। फिर से शुरू करने के लिए 'hi' टाइप करें।",
        "hg": "Sorry, is specialty mein abhi koi doctor available nahi hai. Phir se shuru karne ke liye 'hi' type karein.",
    },
    "doctors_widened": {
        "en": "No doctors of this type in {city} right now — showing nearby options instead.",
        "hi": "{city} में अभी इस तरह के डॉक्टर नहीं हैं — आस-पास के विकल्प दिखा रहे हैं।",
        "hg": "{city} mein abhi is type ke doctor nahi hain — aas-paas ke options dikha rahe hain.",
    },
    "doctors_widened_radius": {
        "en": "Nobody very close by, so here's everyone within about {radius} km.",
        "hi": "बिल्कुल पास कोई नहीं मिला, तो लगभग {radius} किमी के अंदर के सभी डॉक्टर दिखा रहे हैं।",
        "hg": "Bilkul paas koi nahi mila, to lagbhag {radius} km ke andar ke sabhi doctor dikha rahe hain.",
    },
    "no_doctors_in_radius": {
        "en": "I couldn't find this type of doctor within {radius} km of you. Shall I look further away?",
        "hi": "आपसे {radius} किमी के अंदर इस तरह के डॉक्टर नहीं मिले। क्या और दूर तक देखूं?",
        "hg": "Aapse {radius} km ke andar is type ke doctor nahi mile. Aur door tak dekhun?",
    },
    # Button title — must stay inside WhatsApp's 20-char cap (test_specialty_groups.py checks this).
    "search_wider_yes": {"en": "Yes, look further", "hi": "हां, और दूर देखें", "hg": "Haan, door dekhein"},
    "doctor_list_prompt": {
        "en": "Here are the doctors available:",
        "hi": "उपलब्ध डॉक्टरों की सूची यह है:",
        "hg": "Available doctors ki list yeh hai:",
    },
    "doctor_list_button": {"en": "Choose doctor", "hi": "डॉक्टर चुनें", "hg": "Doctor choose karein"},
    "doctor_choose_hint": {
        "en": "Please choose a doctor from the list above.",
        "hi": "कृपया ऊपर सूची में से एक doctor चुनें।",
        "hg": "Upar list mein se doctor choose kar lijiye.",
    },
    "date_prompt": {
        "en": "When would you like to visit?",
        "hi": "आप कब आना चाहेंगे?",
        "hg": "Kab aana chahenge?",
    },
    "date_today": {"en": "Today", "hi": "आज", "hg": "Aaj"},
    "date_tomorrow": {"en": "Tomorrow", "hi": "कल", "hg": "Kal"},
    "date_choose_hint": {
        "en": "Please choose Today or Tomorrow above.",
        "hi": "कृपया ऊपर आज या कल चुनें।",
        "hg": "Upar Aaj ya Kal choose kar lijiye.",
    },
    # These two keep the patient's session — they're offered alongside buttons for the other
    # day and for picking a different doctor, so nothing they've already answered is lost.
    "not_available": {
        "en": "That doctor isn't available then. Try another day, or pick a different doctor?",
        "hi": "उस दिन यह डॉक्टर उपलब्ध नहीं है। दूसरा दिन देखें, या दूसरा डॉक्टर चुनें?",
        "hg": "Us din yeh doctor available nahi hai. Doosra din dekhein, ya doosra doctor chunein?",
    },
    "today_shifts_over": {
        "en": "Today's timings are already over. Try tomorrow, or pick a different doctor?",
        "hi": "आज का समय निकल चुका है। कल देखें, या दूसरा डॉक्टर चुनें?",
        "hg": "Aaj ka time nikal chuka hai. Kal dekhein, ya doosra doctor chunein?",
    },
    "change_doctor_btn": {"en": "Different doctor", "hi": "दूसरा डॉक्टर", "hg": "Doosra doctor"},
    "shift_prompt": {
        "en": "Which time of day works best?",
        "hi": "दिन के किस समय आना ठीक रहेगा?",
        "hg": "Din ke kis time aana theek rahega?",
    },
    # Lists what's actually still open, so a patient who types instead of tapping is told
    # which words will work rather than just being refused.
    "shift_choose_hint": {
        "en": "Please pick one of these: {options}",
        "hi": "इनमें से एक चुनें: {options}",
        "hg": "Inmein se ek chunein: {options}",
    },
    # Itemised rather than one run-on sentence, and the clinic line is the reason why: the
    # search reaches up to 75km, so the doctor may be in a different town from the patient.
    # That has to be visible BEFORE confirming, not after, when the map pin arrives.
    "confirm_prompt": {
        "en": "Please check and confirm:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
        "hi": "देख लीजिए और कन्फ़र्म करें:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
        "hg": "Dekh lijiye aur confirm karein:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
    },
    "confirm_btn": {"en": "Confirm", "hi": "कन्फ़र्म करें", "hg": "Confirm"},
    "cancel_btn": {"en": "Cancel", "hi": "रद्द करें", "hg": "Cancel"},
    "confirm_choose_hint": {
        "en": "Please tap Confirm or Cancel above.",
        "hi": "कृपया ऊपर Confirm या Cancel पर टैप करें।",
        "hg": "Upar Confirm ya Cancel par tap karein.",
    },
    "cancelled": {
        "en": "No problem — booking cancelled.",
        "hi": "कोई बात नहीं — बुकिंग रद्द कर दी गई है।",
        "hg": "Koi baat nahi — booking cancel kar di gayi hai.",
    },
    "already_pending": {
        "en": "You already have a pending request for that day — our team will reach out shortly.",
        "hi": "उस दिन के लिए आपका एक अनुरोध पहले से लंबित है — हमारी टीम जल्द ही संपर्क करेगी।",
        "hg": "Us din ke liye aapka ek request pehle se pending hai — hamari team jald hi contact karegi.",
    },
    "booked_success": {
        "en": "Appointment request for {patient_name} has been submitted! Our front desk will confirm the exact time shortly.",
        "hi": "{patient_name} के लिए अपॉइंटमेंट अनुरोध भेज दिया गया है! हमारी रिसेप्शन जल्द ही सही समय कन्फ़र्म करेगी।",
        "hg": "{patient_name} ke liye appointment request submit ho gayi hai! Front desk jald hi exact time confirm karegi.",
    },
    "booked_queue_note": {
        "en": "We'll send you live queue/token updates on WhatsApp on the day of the visit.",
        "hi": "विज़िट के दिन हम आपको WhatsApp पर लाइव क्यू/टोकन अपडेट भेजेंगे।",
        "hg": "Visit ke din hum aapko WhatsApp par live queue/token updates bhejenge.",
    },
    "booked_map_caption": {
        "en": "{hospital_name} — tap to see the location.",
        "hi": "{hospital_name} — लोकेशन देखने के लिए टैप करें।",
        "hg": "{hospital_name} — location dekhne ke liye tap karein.",
    },
    "error_hms": {
        "en": "Sorry, something went wrong on our end. Please try again in a moment.",
        "hi": "क्षमा करें, हमारी तरफ़ से कुछ गड़बड़ हो गई। कृपया थोड़ी देर में फिर कोशिश करें।",
        "hg": "Sorry, hamari taraf se kuch gadbad ho gayi. Thodi der mein phir try karein.",
    },
    "error_hms_unreachable": {
        "en": "Our booking system is temporarily unavailable. Please try again shortly.",
        "hi": "हमारा बुकिंग सिस्टम अभी अस्थायी रूप से उपलब्ध नहीं है। कृपया थोड़ी देर बाद कोशिश करें।",
        "hg": "Hamara booking system abhi temporarily available nahi hai. Thodi der baad try karein.",
    },
    "followup_reminder": {
        "en": "Hi {patient_name}, just checking in after your visit to {doctor_name} — how are you feeling? Reply if you need a follow-up booked.",
        "hi": "नमस्ते {patient_name}, {doctor_name} से मिलने के बाद बस हाल-चाल पूछ रहे हैं — अब कैसा महसूस कर रहे हैं? फॉलो-अप बुक करना हो तो जवाब दें।",
        "hg": "Hi {patient_name}, {doctor_name} se milne ke baad bas haal-chaal pooch rahe hain — kaisa feel kar rahe hain? Follow-up book karna ho to reply karein.",
    },
}


def t(key: str, lang: str | None, **kwargs) -> str:
    """Look up a string by key + language, formatting in any provided kwargs.
    Falls back to English if lang is unset/unknown, and to the key itself (so a missing
    translation fails loud in testing rather than silently sending blank text to a patient)."""
    entry = _STRINGS.get(key)
    if entry is None:
        return key
    text = entry.get(lang or DEFAULT_LANG) or entry.get(DEFAULT_LANG) or key
    return text.format(**kwargs) if kwargs else text
