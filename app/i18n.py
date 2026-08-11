"""
app/i18n.py
Four-language string table for the conversation flow: English (en), Hindi (hi, Devanagari),
Hinglish (hg, romanised — matching the tone already used in this project's other bot copy),
and Bengali (bn).

Design note: this is a plain dict, not a full i18n framework (gettext, babel, etc.) —
deliberately KISS, matching this project's stated preference elsewhere ("KISS first, add
complexity only when there's a concrete need"). ~40 keys is small enough that a dict is the
simplest thing that works.
"""

DEFAULT_LANG = "en"

LANGUAGE_LABELS = {"en": "English", "hi": "हिंदी", "hg": "Hinglish", "bn": "বাংলা"}

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
        "title": {
            "en": "🩺 Everyday health",
            "hi": "🩺 रोज़मर्रा की सेहत",
            "hg": "🩺 Rozmarra ki sehat",
            "bn": "🩺 দৈনন্দিন স্বাস্থ্য",
        },
        "desc": {
            "en": "Fever, cough, BP, sugar, check-up",
            "hi": "बुखार, खांसी, बीपी, शुगर, जांच",
            "hg": "Bukhar, khaansi, BP, sugar, check-up",
            "bn": "জ্বর, কাশি, বিপি, সুগার, চেক-আপ",
        },
    },
    {
        "id": "grp_women_children",
        "categories": ["Gynaecologist", "Paediatrician"],
        "title": {
            "en": "👶 Women & children",
            "hi": "👶 महिला और बच्चे",
            "hg": "👶 Mahila aur bachche",
            "bn": "👶 নারী ও শিশু",
        },
        "desc": {
            "en": "Pregnancy, periods, child health",
            "hi": "गर्भावस्था, पीरियड्स, बच्चों की सेहत",
            "hg": "Pregnancy, periods, bachchon ki sehat",
            "bn": "গর্ভাবস্থা, পিরিয়ড, শিশু স্বাস্থ্য",
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
        "title": {
            "en": "🦴 Bones & joints",
            "hi": "🦴 हड्डी और जोड़",
            "hg": "🦴 Haddi aur jod",
            "bn": "🦴 হাড় এবং জয়েন্ট",
        },
        "desc": {
            "en": "Back pain, knee pain, fracture, sprain",
            "hi": "कमर दर्द, घुटने का दर्द, फ्रैक्चर, मोच",
            "hg": "Kamar dard, ghutne ka dard, fracture",
            "bn": "পিঠের ব্যথা, হাঁটুর ব্যথা, ফ্র্যাকচার",
        },
    },
    {
        "id": "grp_eyes_ent_skin",
        "categories": ["Ophthalmologist (Eye)", "ENT Specialist", "Dermatologist (Skin)"],
        "title": {
            "en": "👁️ Eye, ENT & skin",
            "hi": "👁️ आंख, कान, त्वचा",
            "hg": "👁️ Aankh, ENT, skin",
            "bn": "👁️ চোখ, কান ও ত্বক",
        },
        "desc": {
            "en": "Eyesight, ear pain, throat, rashes",
            "hi": "नज़र, कान दर्द, गला, चर्म रोग",
            "hg": "Nazar, kaan dard, gala, skin problem",
            "bn": "দৃষ্টিশক্তি, কানের ব্যথা, গলা, ফুসকুড়ি",
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
        "title": {
            "en": "💧 Stomach & kidney",
            "hi": "💧 पेट और किडनी",
            "hg": "💧 Pet aur kidney",
            "bn": "💧 পাকস্থলী ও কিডনি",
        },
        "desc": {
            "en": "Acidity, stomach pain, urine, stones",
            "hi": "एसिडिटी, पेट दर्द, पेशाब, पथरी",
            "hg": "Acidity, pet dard, peshab, pathri",
            "bn": "অ্যাসিডিটি, পেটে ব্যথা, প্রস্রাব, পাথর",
        },
    },
    {
        "id": "grp_heart_chest",
        "categories": ["Cardiologist (Heart)", "Pulmonologist (Chest/Lungs)", "Cardiothoracic Surgeon"],
        "title": {
            "en": "❤️ Heart & chest",
            "hi": "❤️ दिल और छाती",
            "hg": "❤️ Dil aur chhaati",
            "bn": "❤️ হার্ট এবং বুক",
        },
        "desc": {
            "en": "Chest pain, breathing trouble, asthma",
            "hi": "सीने में दर्द, सांस की तकलीफ, अस्थमा",
            "hg": "Seene mein dard, saans ki takleef, asthma",
            "bn": "বুকে ব্যথা, শ্বাসকষ্ট, হাঁপানি",
        },
    },
    {
        "id": "grp_brain_mind",
        "categories": ["Neurologist", "Neurosurgeon", "Psychiatrist"],
        "title": {
            "en": "🧠 Brain & mind",
            "hi": "🧠 दिमाग और मन",
            "hg": "🧠 Dimaag aur mann",
            "bn": "🧠 মস্তিষ্ক ও মন",
        },
        "desc": {
            "en": "Headache, fits, memory, stress, sleep",
            "hi": "सिरदर्द, दौरे, याददाश्त, तनाव, नींद",
            "hg": "Sirdard, daure, yaaddasht, stress, neend",
            "bn": "মাথাব্যথা, খিঁচুনি, স্মৃতিশক্তি, মানসিক চাপ",
        },
    },
    {
        "id": "grp_surgery",
        "categories": ["General Surgeon", "Plastic Surgeon", "Vascular Surgeon", "Oncologist (Cancer)"],
        "title": {
            "en": "🏥 Surgery & cancer",
            "hi": "🏥 सर्जरी और कैंसर",
            "hg": "🏥 Surgery aur cancer",
            "bn": "🏥 সার্জারি ও ক্যান্সার",
        },
        "desc": {
            "en": "Operations, cancer treatment, veins",
            "hi": "ऑपरेशन, कैंसर का इलाज, नसें",
            "hg": "Operation, cancer ka ilaaj, nasein",
            "bn": "অপারেশন, ক্যান্সারের চিকিৎসা, শিরা",
        },
    },
    {
        "id": "grp_tests",
        "categories": ["Radiologist", "Pathologist", "Anaesthesiologist"],
        "title": {
            "en": "🔬 Tests & scans",
            "hi": "🔬 जांच और स्कैन",
            "hg": "🔬 Jaanch aur scan",
            "bn": "🔬 পরীক্ষা এবং স্ক্যান",
        },
        "desc": {
            "en": "X-ray, sonography, lab reports",
            "hi": "एक्स-रे, सोनोग्राफी, लैब रिपोर्ट",
            "hg": "X-ray, sonography, lab report",
            "bn": "এক্স-রে, সোনোগ্রাফি, ল্যাব রিপোর্ট",
        },
    },
]

# Catch-all for any live category not listed above — e.g. if 1HMS adds a new specialty
# tomorrow, it shows up here instead of silently vanishing (which is exactly the failure
# mode this whole grouping exists to fix). Only offered when it actually has something in it.
OTHER_GROUP = {
    "id": "grp_other",
    "title": {
        "en": "➕ Other specialities",
        "hi": "➕ अन्य विशेषज्ञ",
        "hg": "➕ Aur specialities",
        "bn": "➕ অন্যান্য বিশেষজ্ঞতা",
    },
    "desc": {
        "en": "Anything not listed above",
        "hi": "जो ऊपर नहीं दिखा",
        "hg": "Jo upar nahi dikha",
        "bn": "উপরে তালিকাভুক্ত নয় এমন কিছু",
    },
}


def group_label(group: dict, lang: str | None) -> tuple[str, str]:
    """(title, description) for a group row, in the patient's language."""
    lang = lang or DEFAULT_LANG
    title = group["title"].get(lang) or group["title"][DEFAULT_LANG]
    desc = group["desc"].get(lang) or group["desc"][DEFAULT_LANG]
    return title, desc


# The very first message of any conversation — necessarily shown in all languages at once,
# since we don't know the patient's preference yet.
LANG_PROMPT = (
    "👋 Welcome! I can help you book a doctor's appointment.\n"
    "भाषा चुनें / Please choose a language / Language choose kar lijiye / ভাষা চয়ন করুন:"
)

_STRINGS: dict[str, dict[str, str]] = {
    "greeting": {
        "en": "Great, I'll continue in English.",
        "hi": "ठीक है, अब मैं हिंदी में बात करूँगा।",
        "hg": "Theek hai, ab Hinglish mein baat karte hain.",
        "bn": "ঠিক আছে, আমি বাংলায় কথা বলব।",
    },
    "confirm_lang_prompt": {
        "en": "We identified your language as English. Click 'Proceed' to share location, or 'Change Language' below.",
        "hi": "हमने आपकी भाषा की पहचान हिंदी के रूप में की है। लोकेशन भेजने के लिए 'आगे बढ़ें' पर क्लिक करें, या नीचे 'भाषा बदलें' चुनें।",
        "hg": "Humne aapki language Hinglish identify ki hai. Location share karne ke liye 'Proceed' click karein, ya 'Language badlein' select karein.",
        "bn": "আমরা আপনার ভাষা বাংলা হিসেবে সনাক্ত করেছি। লোকেশন শেয়ার করতে 'এগিয়ে যান' ক্লিক করুন, অথবা নিচে 'ভাষা পরিবর্তন করুন' নির্বাচন করুন।",
    },
    "confirm_yes": {
        "en": "Proceed",
        "hi": "आगे बढ़ें",
        "hg": "Proceed",
        "bn": "এগিয়ে যান",
    },
    "confirm_change": {
        "en": "Change Language",
        "hi": "भाषा बदलें",
        "hg": "Language badlein",
        "bn": "ভাষা পরিবর্তন করুন",
    },
    "welcome_banner": {
        "en": "Welcome! You can type 'cancel' or 'quit' to end this chat anytime, and 'back' to go back 1 step.",
        "hi": "स्वागत है! आप किसी भी समय बातचीत खत्म करने के लिए 'cancel' या 'quit' टाइप कर सकते हैं, और 1 कदम पीछे जाने के लिए 'back' टाइप करें।",
        "hg": "Welcome! Aap kabhi bhi chat khatam karne ke liye 'cancel' ya 'quit' type kar sakte hain, aur 1 step peeche jaane ke liye 'back' type karein.",
        "bn": "স্বাগতম! আপনি যেকোনো সময় চ্যাট শেষ করতে 'cancel' বা 'quit' টাইপ করতে পারেন, এবং ১ ধাপ পিছিয়ে যেতে 'back' টাইপ করুন।",
    },
    "welcome_multilang": {
        "en": (
            "Welcome! Select language below to start.\n"
            "स्वागत है! आगे बढ़ने के लिए नीचे भाषा चुनें।\n\n"
            "🩺 Doctor search · 🤒 Symptom check · 📅 Book appointment"
        ),
        "hi": (
            "Welcome! Select language below to start.\n"
            "स्वागत है! आगे बढ़ने के लिए नीचे भाषा चुनें।\n\n"
            "🩺 Doctor search · 🤒 Symptom check · 📅 Book appointment"
        ),
        "hg": (
            "Welcome! Select language below to start.\n"
            "स्वागत है! आगे बढ़ने के लिए नीचे भाषा चुनें।\n\n"
            "🩺 Doctor search · 🤒 Symptom check · 📅 Book appointment"
        ),
        "bn": (
            "Welcome! Select language below to start.\n"
            "स्वागत है! आगे बढ़ने के लिए नीचे भाषा चुनें।\n\n"
            "🩺 Doctor search · 🤒 Symptom check · 📅 Book appointment"
        ),
    },
    "back_no_history": {
        "en": "Cannot go back further. Starting over...",
        "hi": "अब और पीछे नहीं जा सकते। फिर से शुरू कर रहे हैं...",
        "hg": "Ab aur peeche nahi ja sakte. Phir se shuru kar rahe hain...",
        "bn": "আর পিছনে যাওয়া যাবে না। আবার শুরু করা হচ্ছে...",
    },
    "you": {"en": "You", "hi": "आप", "hg": "Aap", "bn": "আপনি"},
    "clinic_unknown": {"en": "Clinic", "hi": "क्लिनिक", "hg": "Clinic", "bn": "ক্লিনিক"},
    "patient_details_prompt_flow": {
        "en": "Please fill in the patient's details (Name, Age, Gender, Guardian optional) by tapping below.",
        "hi": "नीचे टैप करके मरीज़ का विवरण (नाम, उम्र, लिंग, अभिभावक वैकल्पिक) भरें।",
        "hg": "Neeche tap karke patient ki details (Name, Age, Gender, Guardian optional) fill kar dijiye.",
        "bn": "নীচে ট্যাপ করে রোগীর বিবরণ (নাম, বয়স, লিঙ্গ, অভিভাবক ঐচ্ছিক) পূরণ করুন।",
    },
    "patient_details_prompt_text": {
        "en": "Please send the patient's details in this format: Name, Age, Gender, Guardian (optional) (e.g. 'Riya, 8, Female, Rajesh' or just 'Riya, 8, Female').",
        "hi": "कृपया मरीज़ का विवरण इस प्रारूप में भेजें: नाम, उम्र, लिंग, अभिभावक (वैकल्पिक) (जैसे 'रिया, 8, महिला, राजेश' या सिर्फ 'रिया, 8, महिला')।",
        "hg": "Please patient ke details is format mein bhejein: Name, Age, Gender, Guardian (optional) (jaise 'Riya, 8, Female, Rajesh' ya sirf 'Riya, 8, Female').",
        "bn": "অনুগ্রহ করে রোগীর বিবরণ এই বিন্যাসে পাঠান: নাম, বয়স, লিঙ্গ, অভিভাবক (ঐচ্ছিক) (যেমন 'Riya, 8, Female, Rajesh' অথবা শুধু 'Riya, 8, Female')।",
    },
    "patient_details_flow_cta": {
        "en": "Fill Form",
        "hi": "विवरण भरें",
        "hg": "Form bharein",
        "bn": "ফর্ম পূরণ করুন",
    },
    "patient_details_invalid": {
        "en": "Please fill Name, Age and Gender correctly (Guardian is optional) — e.g. 'Riya, 8, Female, Rajesh' or just 'Riya, 8, Female'.",
        "hi": "कृपया नाम, उम्र और लिंग सही से भरें (अभिभावक वैकल्पिक है) — जैसे 'रिया, 8, महिला, राजेश' या सिर्फ 'रिया, 8, महिला'।",
        "hg": "Please Name, Age aur Gender sahi se fill karein (Guardian optional hai) — jaise 'Riya, 8, Female, Rajesh' ya sirf 'Riya, 8, Female'.",
        "bn": "অনুগ্রহ করে নাম, বয়স এবং লিঙ্গ সঠিকভাবে পূরণ করুন (অভিভাবক ঐচ্ছিক) — যেমন 'Riya, 8, Female, Rajesh' অথবা শুধু 'Riya, 8, Female'।",
    },
    "update_details_btn": {
        "en": "Update details",
        "hi": "विवरण बदलें",
        "hg": "Details badlein",
        "bn": "বিবরণ পরিবর্তন করুন",
    },
    "age_invalid": {
        "en": "That age doesn't look right — please send a number of years, e.g. 32.",
        "hi": "यह उम्र सही नहीं लग रही — कृपया वर्षों में संख्या भेजें, जैसे 32।",
        "hg": "Ye age sahi nahi lag rahi — saalon mein number bhejein, jaise 32.",
        "bn": "এই বয়সটি সঠিক মনে হচ্ছে না — অনুগ্রহ করে বছরের সংখ্যা পাঠান, যেমন 32।",
    },
    "location_prompt": {
        "en": "To show doctors near you, please share your location — tap below, it fills in from your phone's GPS automatically.",
        "hi": "आपके पास के डॉक्टर दिखाने के लिए, कृपया अपनी लोकेशन शेयर करें — नीचे टैप करें, यह आपके फोन के GPS से अपने आप भर जाएगी।",
        "hg": "Aapke paas ke doctors dikhane ke liye, apni location share kar dijiye — neeche tap karein, phone ke GPS se automatic fill ho jayegi.",
        "bn": "আপনার কাছাকাছি ডাক্তারদের দেখাতে, অনুগ্রহ করে আপনার অবস্থান শেয়ার করুন — নিচে ট্যাপ করুন, এটি আপনার ফোনের জিপিএস থেকে স্বয়ংক্রিয়ভাবে পূরণ হবে।",
    },
    "location_manual_hint": {
        "en": "Or just type your city/area name instead.",
        "hi": "या फिर अपने शहर/इलाके का नाम टाइप कर दें।",
        "hg": "Ya phir apne city/area ka naam type kar dijiye.",
        "bn": "অথবা এর পরিবর্তে শুধু আপনার শহর/এলাকার নাম টাইপ করুন।",
    },
    "location_not_found": {
        "en": "We couldn't find a city matching '{query}'. Please share location or type a valid city name.",
        "hi": "हमें '{query}' नाम से कोई शहर नहीं मिला। कृपया लोकेशन शेयर करें या सही शहर का नाम टाइप करें।",
        "hg": "Hamein '{query}' naam se koi city nahi mili. Kripya location share karein ya sahi city ka naam type karein.",
        "bn": "আমরা '{query}' নামের কোনো शहर খুঁজে পাইনি। অনুগ্রহ করে অবস্থান শেয়ার করুন বা সঠিক শহরের নাম টাইপ করুন।",
    },
    "search_mode_prompt": {
        "en": "How would you like to find a doctor?",
        "hi": "आप डॉक्टर कैसे खोजना चाहेंगे?",
        "hg": "Doctor kaise dhundna chahenge?",
        "bn": "আপনি কিভাবে ডাক্তার খুঁজতে চান?",
    },
    "search_mode_symptom": {
        "en": "Describe symptoms",
        "hi": "लक्षण बताएं",
        "hg": "Symptoms bataayein",
        "bn": "লক্ষণ বলুন",
    },
    "search_mode_browse": {
        "en": "Browse specialties",
        "hi": "विशेषज्ञता देखें",
        "hg": "Specialty dekhein",
        "bn": "বিশেষজ্ঞতা খুঁজুন",
    },
    "search_mode_name": {
        "en": "Search by doctor name",
        "hi": "डॉक्टर के नाम से खोजें",
        "hg": "Doctor ke naam se search karein",
        "bn": "ডাক্তারের নাম দিয়ে খুঁজুন",
    },
    "doctor_name_ask": {
        "en": "Please type the name of the doctor you are looking for:",
        "hi": "कृपया उस डॉक्टर का नाम टाइप करें जिन्हें आप ढूंढ रहे हैं:",
        "hg": "Please us doctor ka naam type karein jinhe aap dhoond rahe hain:",
        "bn": "অনুগ্রহ করে আপনি যে ডাক্তারের সন্ধান করছেন তার নাম টাইপ করুন:",
    },
    "doctor_name_text_required": {
        "en": "Please type the doctor's name to search.",
        "hi": "खोजने के लिए कृपया डॉक्टर का नाम टाइप करें।",
        "hg": "Search karne ke liye please doctor ka naam type karein.",
        "bn": "অনুগ্রহ করে খোঁজার জন্য ডাক্তারের নাম টাইপ করুন।",
    },
    "symptom_ask": {
        "en": "Sure — describe what's bothering you (e.g. 'chest pain and shortness of breath').",
        "hi": "ठीक है — बताएं क्या तकलीफ़ है (जैसे 'सीने में दर्द और सांस फूलना')।",
        "hg": "Theek hai — bataiye kya problem hai (jaise 'chest pain aur saans phoolna').",
        "bn": "অবশ্যই — আপনার সমস্যাটি বর্ণনা করুন (যেমন 'বুকে ব্যথা এবং শ্বাসকষ্ট')।",
    },
    "symptom_text_required": {
        "en": "Please describe your symptoms as text.",
        "hi": "कृपया अपने लक्षण टेक्स्ट में लिखें।",
        "hg": "Apne symptoms text mein likh dijiye.",
        "bn": "অনুগ্রহ করে আপনার লক্ষণগুলি পাঠ্য হিসাবে বর্ণনা করুন।",
    },
    "symptom_no_match": {
        "en": "I couldn't confidently match that to a specialty — here's the full list instead:",
        "hi": "मैं इसे किसी विशेषज्ञता से पक्के तौर पर नहीं जोड़ पाया — इसके बजाय पूरी सूची यहां है:",
        "hg": "Main isko kisi specialty se confidently match nahi kar paaya — poori list yeh rahi:",
        "bn": "আমি নিশ্চিতভাবে এটিকে কোনো বিশেষজ্ঞতার সাথে মেলাতে পারিনি — পরিবর্তে সম্পূর্ণ তালিকাটি এখানে দেওয়া হলো:",
    },
    "symptom_matched": {
        "en": "That sounds like a job for a {category}.",
        "hi": "यह {category} के काम जैसा लगता है।",
        "hg": "Yeh {category} ka kaam lagta hai.",
        "bn": "এটি একজন {category} এর কাজ বলে মনে হচ্ছে।",
    },
    "no_specialties": {
        "en": "Sorry, no doctors are available for booking right now. Please try later.",
        "hi": "क्षमा करें, अभी बुकिंग के लिए कोई डॉक्टर उपलब्ध नहीं है। कृपया बाद में कोशिश करें।",
        "hg": "Sorry, abhi booking ke liye koi doctor available nahi hai. Baad mein try karein.",
        "bn": "দুঃখিত, এই মুহূর্তে বুকিংয়ের জন্য কোনো ডাক্তার উপলব্ধ নেই। অনুগ্রহ করে পরে চেষ্টা করুন।",
    },
    "specialty_group_prompt": {
        "en": "No long forms here 🙂 Just tell me roughly what it's about — I'll find the right doctor.",
        "hi": "यहां कोई लंबा फॉर्म नहीं है 🙂 बस मोटे तौर पर बता दीजिए किस बारे में है — सही डॉक्टर मैं ढूंढ दूंगा।",
        "hg": "Yahan koi lamba form nahi hai 🙂 Bas mote taur par bata dijiye kis baare mein hai — sahi doctor main dhoond dunga.",
        "bn": "এখানে কোনো দীর্ঘ ফর্ম নেই 🙂 শুধু আমাকে সংক্ষেপে বলুন এটা কী সম্পর্কে — আমি সঠিক ডাক্তার খুঁজে দেব।",
    },
    "specialty_group_button": {
        "en": "Pick an area",
        "hi": "क्षेत्र चुनें",
        "hg": "Area choose karein",
        "bn": "একটি এলাকা চয়ন করুন",
    },
    "specialty_group_section": {"en": "Areas", "hi": "क्षेत्र", "hg": "Areas", "bn": "এলাকা"},
    "specialty_group_choose_hint": {
        "en": "Please pick one of the areas above — or just describe how you're feeling and I'll work it out.",
        "hi": "कृपया ऊपर दिए क्षेत्रों में से एक चुनें — या बस बता दें कि क्या तकलीफ़ है, मैं समझ लूंगा।",
        "hg": "Upar diye areas mein se ek choose karein — ya bas bata dijiye kya takleef hai, main samajh lunga.",
        "bn": "অনুগ্রহ করে উপরের এলাকাগুলির মধ্যে একটি বেছে নিন — অথবা আপনি কেমন অনুভব করছেন তা বর্ণনা করুন এবং আমি এটি সমাধান করব।",
    },
    "search_mode_choose_hint": {
        "en": "Please choose Symptom search or Browse specialties above.",
        "hi": "कृपया ऊपर बीमारी खोजें या विशेषता चुनें।",
        "hg": "Please upar diye gaye options mein se Symptom search ya Browse specialties choose karein.",
        "bn": "অনুগ্রহ করে উপরে রোগের উপসর্গ অনুযায়ী খুঁজুন বা বিশেষত্ব অনুযায়ী খুঁজুন বেছে নিন।",
    },
    "search_doctor_not_found": {
        "en": "We couldn't find a doctor matching '{query}'. Let's find one by symptom or specialty instead:",
        "hi": "हमें '{query}' नाम से कोई डॉक्टर नहीं मिला। आइए इसके बजाय बीमारी या विशेषता के अनुसार ढूंढते हैं:",
        "hg": "Hamein '{query}' naam se koi doctor nahi mila. Aaiye iske badle symptom ya specialty se search karte hain:",
        "bn": "আমরা '{query}' নামের কোনো ডাক্তার খুঁজে পাইনি। আসুন রোগ বা বিশেষত্ব অনুযায়ী খুঁজি:",
    },
    "pricing_doctor_fee": {
        "en": "{doctor}'s consultation fee is ₹{fee}.",
        "hi": "{doctor} की परामर्श फीस ₹{fee} है।",
        "hg": "{doctor} ki consultation fee ₹{fee} hai.",
        "bn": "{doctor}-এর পরামর্শ ফি ₹{fee}।",
    },
    "pricing_multiple_doctors": {
        "en": "A few doctors match '{query}':\n{list}",
        "hi": "'{query}' से मिलते-जुलते कुछ डॉक्टर:\n{list}",
        "hg": "'{query}' se milte julte kuch doctors:\n{list}",
        "bn": "'{query}' এর সাথে মিলে যাওয়া কয়েকজন ডাক্তার:\n{list}",
    },
    "pricing_specialty_range": {
        "en": "{specialty} consultation fees range from ₹{min_fee} to ₹{max_fee}, depending on the doctor.",
        "hi": "{specialty} परामर्श की फीस डॉक्टर के अनुसार ₹{min_fee} से ₹{max_fee} तक है।",
        "hg": "{specialty} consultation ki fee doctor ke hisaab se ₹{min_fee} se ₹{max_fee} tak hai.",
        "bn": "{specialty} পরামর্শের ফি ডাক্তার অনুযায়ী ₹{min_fee} থেকে ₹{max_fee} পর্যন্ত।",
    },
    "pricing_not_available": {
        "en": "Sorry, fee information isn't available for that right now.",
        "hi": "क्षमा करें, अभी इसकी फीस की जानकारी उपलब्ध नहीं है।",
        "hg": "Sorry, abhi iski fee ki jaankari available nahi hai.",
        "bn": "দুঃখিত, এই মুহূর্তে ফি সংক্রান্ত তথ্য পাওয়া যাচ্ছে না।",
    },
    "pricing_ask_which": {
        "en": "Which doctor or specialty would you like pricing for?",
        "hi": "आप किस डॉक्टर या विशेषता की फीस जानना चाहते हैं?",
        "hg": "Aap kis doctor ya specialty ki fee jaanna chahte hain?",
        "bn": "আপনি কোন ডাক্তার বা বিশেষত্বের ফি জানতে চান?",
    },
    "instructions": {
        "en": "Type 'cancel' or 'quit' to end this chat anytime (just send a new message to start again), and 'back' to go back 1 step.",
        "hi": "आप किसी भी समय बातचीत खत्म करने के लिए 'cancel' या 'quit' टाइप कर सकते हैं (फिर से शुरू करने के लिए नया संदेश भेजें), और 1 कदम पीछे जाने के लिए 'back' टाइप करें।",
        "hg": "Aap kabhi bhi chat khatam karne ke liye 'cancel' ya 'quit' type kar sakte hain (phir se shuru karne ke liye naya message bhejein), aur 1 step peeche jaane ke liye 'back' type karein.",
        "bn": "আপনি যেকোনো সময় চ্যাট শেষ করতে 'cancel' বা 'quit' টাইপ করতে পারেন (আবার শুরু করতে নতুন বার্তা পাঠান), এবং ১ ধাপ পিছিয়ে যেতে 'back' টাইপ করুন।",
    },
    "specialty_list_prompt": {
        "en": "Good — which of these fits best?",
        "hi": "ठीक है — इनमें से कौन सा सबसे सही रहेगा?",
        "hg": "Theek hai — inmein se kaun sa sabse sahi rahega?",
        "bn": "ভালো — এর মধ্যে কোনটি সবচেয়ে উপযুক্ত?",
    },
    "specialty_list_button": {
        "en": "Choose specialty",
        "hi": "विशेषज्ञता चुनें",
        "hg": "Specialty chunein",
        "bn": "বিশেষজ্ঞতা বেছে নিন",
    },
    "specialty_choose_hint": {
        "en": "Please choose a specialty from the list above.",
        "hi": "कृपया ऊपर सूची में से एक विशेषज्ञता चुनें।",
        "hg": "Upar list mein se specialty choose kar lijiye.",
        "bn": "অনুগ্রহ করে উপরের তালিকা থেকে একটি विशेषज्ञता বেছে নিন।",
    },
    "sort_prompt": {
        "en": "How should I sort the doctor list?",
        "hi": "डॉक्टरों की सूची किस आधार पर दिखाऊं?",
        "hg": "Doctor list kis basis par dikhayein?",
        "bn": "আমি কিভাবে ডাক্তারদের তালিকা সাজাব?",
    },
    "sort_button": {
        "en": "Choose sort order",
        "hi": "क्रम चुनें",
        "hg": "Sort karein",
        "bn": "সাজানোর ক্রম",
    },
    "sort_rating": {
        "en": "Top rated",
        "hi": "सर्वश्रेष्ठ रेटिंग",
        "hg": "Top rated",
        "bn": "সর্বোচ্চ রেটিং",
    },
    "sort_nearest": {
        "en": "Nearest first",
        "hi": "सबसे नज़दीक",
        "hg": "Sabse nazdeek",
        "bn": "সবচেয়ে কাছাকাছি আগে",
    },
    "sort_experience": {
        "en": "Most experienced",
        "hi": "सबसे अनुभवी",
        "hg": "Sabse experienced",
        "bn": "সবচেয়ে অভিজ্ঞ",
    },
    "sort_fee": {
        "en": "Lowest fee",
        "hi": "सबसे कम फीस",
        "hg": "Sabse kam fees",
        "bn": "সর্বনিম্ন ফি",
    },
    "sort_choose_hint": {
        "en": "Please choose one of the sort options above.",
        "hi": "कृपया ऊपर दिए क्रम विकल्पों में से एक चुनें।",
        "hg": "Upar diye sort options mein se ek choose kar lijiye.",
        "bn": "অনুগ্রহ করে উপরের সাজানোর বিকল্পগুলির মধ্যে একটি বেছে নিন।",
    },
    "no_doctors": {
        "en": "Sorry, no doctors are currently available in that specialty. Please type 'hi' to start over.",
        "hi": "क्षमा करें, अभी इस विशेषज्ञता में कोई डॉक्टर उपलब्ध नहीं है। फिर से शुरू करने के लिए 'hi' टाइप करें।",
        "hg": "Sorry, is specialty mein abhi koi doctor available nahi hai. Phir se shuru karne ke liye 'hi' type karein.",
        "bn": "দুঃখিত, এই মুহূর্তে সেই বিশেষজ্ঞতার কোনো ডাক্তার উপলব্ধ নেই। আবার শুরু করতে 'hi' টাইপ করুন।",
    },
    "doctors_widened": {
        "en": "No doctors of this type in {city} right now — showing nearby options instead.",
        "hi": "{city} में अभी इस तरह के डॉक्टर नहीं हैं — आस-पास के विकल्प दिखा रहे हैं।",
        "hg": "{city} mein abhi is type ke doctor nahi hain — aas-paas ke options dikha rahe hain.",
        "bn": "এই মুহূর্তে {city}-তে এই ধরণের কোনো ডাক্তার নেই — পরিবর্তে কাছাকাছি বিকল্পগুলি দেখানো হচ্ছে।",
    },
    "doctors_widened_radius": {
        "en": "Nobody very close by, so here's everyone within about {radius} km.",
        "hi": "बिल्कुल पास कोई नहीं मिला, तो लगभग {radius} किमी के अंदर के सभी डॉक्टर दिखा रहे हैं।",
        "hg": "Bilkul paas koi nahi mila, to lagbhag {radius} km ke andar ke sabhi doctor dikha rahe hain.",
        "bn": "খুব কাছাকাছি কেউ নেই, তাই প্রায় {radius} কিমি এর মধ্যে যারা আছেন তাদের দেখানো হচ্ছে।",
    },
    "no_doctors_in_radius": {
        "en": "I couldn't find this type of doctor within {radius} km of you. Shall I look further away?",
        "hi": "आप आपसे {radius} किमी के अंदर इस तरह के डॉक्टर नहीं मिले। क्या और दूर तक देखूं?",
        "hg": "Aapse {radius} km ke andar is type ke doctor nahi mile. Aur door tak dekhun?",
        "bn": "আমি আপনার {radius} কিমি এর মধ্যে এই ধরণের ডাক্তার খুঁজে পাইনি। আমি কি আরও দূরে খুঁজব?",
    },
    "search_wider_yes": {
        "en": "Yes, look further",
        "hi": "हां, और दूर देखें",
        "hg": "Haan, door dekhein",
        "bn": "হ্যাঁ, দূরে দেখুন",
    },
    "doctor_list_prompt": {
        "en": "Here are the doctors available:",
        "hi": "उपलब्ध डॉक्टरों की सूची यह है:",
        "hg": "Available doctors ki list yeh hai:",
        "bn": "এখানে উপলব্ধ ডাক্তারদের তালিকা রয়েছে:",
    },
    "doctor_list_button": {
        "en": "Choose doctor",
        "hi": "डॉक्टर चुनें",
        "hg": "Doctor choose karein",
        "bn": "ডাক্তার বেছে নিন",
    },
    "doctor_choose_hint": {
        "en": "Please choose a doctor from the list above.",
        "hi": "कृपया ऊपर सूची में से एक doctor चुनें।",
        "hg": "Upar list mein se doctor choose kar lijiye.",
        "bn": "অনুগ্রহ করে উপরের তালিকা থেকে একজন ডাক্তার বেছে নিন।",
    },
    "date_prompt": {
        "en": "When would you like to visit?",
        "hi": "आप कब आना चाहेंगे?",
        "hg": "Kab aana chahenge?",
        "bn": "আপনি কখন দেখা করতে চান?",
    },
    "date_today": {"en": "Today", "hi": "आज", "hg": "Aaj", "bn": "আজ"},
    "date_tomorrow": {"en": "Tomorrow", "hi": "कल", "hg": "Kal", "bn": "আগামীকাল"},
    "shift_morning": {
        "en": "Morning",
        "hi": "सुबह",
        "hg": "Morning",
        "bn": "সকাল",
    },
    "shift_noon": {
        "en": "Noon",
        "hi": "दोपहर",
        "hg": "Noon",
        "bn": "দুপুর",
    },
    "shift_evening": {
        "en": "Evening",
        "hi": "शाम",
        "hg": "Evening",
        "bn": "সন্ধ্যা",
    },
    "date_choose_hint": {
        "en": "Please choose Today or Tomorrow above.",
        "hi": "कृपया ऊपर आज या कल चुनें।",
        "hg": "Upar Aaj ya Kal choose kar lijiye.",
        "bn": "অনুগ্রহ করে উপরে আজ বা আগামীকাল বেছে নিন।",
    },
    "not_available": {
        "en": "That doctor isn't available then. Try another day, or pick a different doctor?",
        "hi": "उस दिन यह डॉक्टर उपलब्ध नहीं है। दूसरा दिन देखें, या दूसरा डॉक्टर चुनें?",
        "hg": "Us din yeh doctor available nahi hai. Doosra din dekhein, ya doosra doctor chunein?",
        "bn": "সেই ডাক্তার তখন উপলব্ধ নেই। অন্য দিন চেষ্টা করবেন, নাকি অন্য ডাক্তার বেছে নেবেন?",
    },
    "today_shifts_over": {
        "en": "Today's timings are already over. Try tomorrow, or pick a different doctor?",
        "hi": "आज का समय निकल चुका है। कल देखें, या दूसरा डॉक्टर चुनें?",
        "hg": "Aaj ka time nikal chuka hai. Kal dekhein, ya doosra doctor chunein?",
        "bn": "আজকের সময় ইতিমধ্যেই শেষ। আগামীকাল চেষ্টা করবেন, নাকি অন্য ডাক্তার বেছে নেবেন?",
    },
    "change_doctor_btn": {
        "en": "Different doctor",
        "hi": "दूसरा डॉक्टर",
        "hg": "Doosra doctor",
        "bn": "অন্য ডাক্তার",
    },
    "shift_prompt": {
        "en": "Which time of day works best?",
        "hi": "दिन के किस समय आना ठीक रहेगा?",
        "hg": "Din ke kis time aana theek rahega?",
        "bn": "দিনের কোন সময়টি সবচেয়ে ভালো হয়?",
    },
    "shift_choose_hint": {
        "en": "Please pick one of these: {options}",
        "hi": "इनमें से एक चुनें: {options}",
        "hg": "Inmein se ek chunein: {options}",
        "bn": "অনুগ্রহ করে এর মধ্যে একটি বেছে নিন: {options}",
    },
    "confirm_prompt": {
        "en": "Please check and confirm:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
        "hi": "देख लीजिए और कन्फ़र्म करें:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
        "hg": "Dekh lijiye aur confirm karein:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
        "bn": "অনুগ্রহ করে পরীক্ষা করে নিশ্চিত করুন:\n\n👤 {patient}\n🩺 {doctor}\n🏥 {where}\n📅 {when}\n💰 ₹{fee}",
    },
    "confirm_btn": {"en": "Confirm", "hi": "कन्फ़र्म करें", "hg": "Confirm", "bn": "নিশ্চিত করুন"},
    "cancel_btn": {"en": "Cancel", "hi": "रद्द करें", "hg": "Cancel", "bn": "বাতিল করুন"},
    "confirm_choose_hint": {
        "en": "Please tap Confirm or Cancel above.",
        "hi": "कृपया ऊपर Confirm या Cancel पर टैप करें।",
        "hg": "Upar Confirm ya Cancel par tap karein.",
        "bn": "অনুগ্রহ করে উপরে নিশ্চিত করুন বা বাতিল করুন-এ ট্যাপ করুন।",
    },
    "cancelled": {
        "en": "Thank you. Send any message to start over, or just ask if you need help.",
        "hi": "धन्यवाद। फिर से शुरू करने के लिए कोई भी संदेश भेजें, या सहायता के लिए पूछें।",
        "hg": "Thank you. Phir se shuru karne ke liye koi bhi message bhejein, ya help ke liye pooch sakte hain.",
        "bn": "ধন্যবাদ। আবার শুরু করতে যেকোনো বার্তা পাঠান, অথবা সাহায্যের জন্য জিজ্ঞাসা করুন।",
    },
    "already_pending": {
        "en": "You already have a pending request for that day — our team will reach out shortly.",
        "hi": "उस दिन के लिए आपका एक अनुरोध पहले से लंबित है — हमारी टीम जल्द ही संपर्क करेगी।",
        "hg": "Us din ke liye aapka ek request pehle se pending hai — hamari team jald hi contact karegi.",
        "bn": "সেই দিনের জন্য আপনার ইতিমধ্যে একটি অনুরোধ মুলতুবি রয়েছে — আমাদের দল শীঘ্রই যোগাযোগ করবে।",
    },
    "booked_success": {
        "en": "Appointment request for {patient_name} has been submitted! Our front desk will confirm the exact time shortly.",
        "hi": "{patient_name} के लिए अपॉइंटमेंट अनुरोध भेज दिया गया है! हमारी रिसेप्शन जल्द ही सही समय कन्फ़र्म करेगी।",
        "hg": "{patient_name} ke liye appointment request submit ho gayi hai! Front desk jald hi exact time confirm karegi.",
        "bn": "{patient_name}-এর জন্য অ্যাপয়েন্টমেন্টের অনুরোধ জমা দেওয়া হয়েছে! আমাদের ফ্রন্ট ডেস্ক শীঘ্রই সঠিক সময় নিশ্চিত করবে।",
    },
    "no_doctors_in_radius_widening": {
        "en": "No {specialty} found within {radius}km — checking a wider area...",
        "hi": "{radius}km के अंदर कोई {specialty} नहीं मिला — थोड़े बड़े क्षेत्र में देखते हैं...",
        "hg": "{radius}km ke andar koi {specialty} nahi mila — thoda bada area check karte hain...",
        "bn": "{radius}km এর মধ্যে কোনো {specialty} পাওয়া যায়নি — একটু বড় এলাকায় খুঁজছি...",
    },
    "symptom_concern_and_location_ask": {
        "en": "That sounds concerning — best to get it checked soon. This looks like a case for a {specialty}.\n\nShare your location so I can find doctors near you:",
        "hi": "ये सुनकर थोड़ी चिंता हुई — जल्दी दिखवाना सही रहेगा। ये {specialty} के पास जाने वाला मामला लगता है।\n\nअपने पास के डॉक्टर ढूंढने के लिए कृपया अपनी लोकेशन शेयर करें:",
        "hg": "Sunke thodi fikar hui — jaldi dikhwana sahi rahega. Ye {specialty} ke paas jaane wala case lagta hai.\n\nApke najdeek doctor dhoondhne ke liye apni location share kar dijiye:",
        "bn": "শুনে একটু চিন্তা হলো — শীঘ্রই দেখানো ভালো হবে। এটা {specialty}-এর কাছে যাওয়ার মতো বিষয় মনে হচ্ছে।\n\nআপনার কাছাকাছি ডাক্তার খুঁজতে অনুগ্রহ করে আপনার লোকেশন শেয়ার করুন:",
    },
    "specialty_enthusiasm_and_location_ask": {
        "en": "Sure! Let's find you a good {specialty}. Share your location so I can show doctors near you:",
        "hi": "ज़रूर! आपके लिए एक अच्छे {specialty} ढूंढते हैं। अपने पास के डॉक्टर दिखाने के लिए कृपया अपनी लोकेशन शेयर करें:",
        "hg": "Zaroor! Aapke liye achha {specialty} dhoondte hain. Najdeeki doctors dikhane ke liye apni location share kar dijiye:",
        "bn": "অবশ্যই! আপনার জন্য একজন ভালো {specialty} খুঁজি। কাছাকাছি ডাক্তার দেখাতে অনুগ্রহ করে আপনার লোকেশন শেয়ার করুন:",
    },
    # Same concern/enthusiasm framing as the two entries above, minus the location-ask
    # sentence -- for use when location is ALREADY known (e.g. _handle_awaiting_symptom,
    # reached only after the choosing_location step has already passed) and the very next
    # thing shown is the sort-prompt, not a location request. Prepended to sort_prompt's own
    # text by _send_sort_prompt to keep it one message, not two.
    "symptom_concern_only": {
        "en": "That sounds concerning — best to get it checked soon. This looks like a case for a {specialty}.",
        "hi": "ये सुनकर थोड़ी चिंता हुई — जल्दी दिखवाना सही रहेगा। ये {specialty} के पास जाने वाला मामला लगता है।",
        "hg": "Sunke thodi fikar hui — jaldi dikhwana sahi rahega. Ye {specialty} ke paas jaane wala case lagta hai.",
        "bn": "শুনে একটু চিন্তা হলো — শীঘ্রই দেখানো ভালো হবে। এটা {specialty}-এর কাছে যাওয়ার মতো বিষয় মনে হচ্ছে।",
    },
    "specialty_enthusiasm_only": {
        "en": "Sure! Let's find you a good {specialty}.",
        "hi": "ज़रूर! आपके लिए एक अच्छे {specialty} ढूंढते हैं।",
        "hg": "Zaroor! Aapke liye achha {specialty} dhoondte hain.",
        "bn": "অবশ্যই! আপনার জন্য একজন ভালো {specialty} খুঁজি।",
    },
    "doctor_too_many_ask_location": {
        "en": "We have {count}+ doctors matching '{query}' — share your location so I can find the right one quickly:",
        "hi": "'{query}' नाम से हमारे पास {count}+ डॉक्टर हैं — सही वाले तक जल्दी पहुंचने के लिए कृपया अपनी लोकेशन शेयर करें:",
        "hg": "'{query}' naam se hamare paas {count}+ doctors hain — sahi wale tak jaldi pahunchne ke liye apni location share kar dijiye:",
        "bn": "'{query}' নামে আমাদের কাছে {count}+ ডাক্তার আছেন — সঠিকজনকে দ্রুত খুঁজে পেতে অনুগ্রহ করে আপনার লোকেশন শেয়ার করুন:",
    },
    "doctor_match_found_detailed": {
        "en": "Found {doctor} for you — {details}.",
        "hi": "{doctor} मिल गए — {details}।",
        "hg": "{doctor} mil gaye — {details}.",
        "bn": "{doctor}-কে পাওয়া গেছে — {details}।",
    },
    "booked_queue_note": {
        "en": "We'll send you live queue/token updates on WhatsApp on the day of the visit.\n\nNeed anything else? Just send a message — doctor search, symptom check, or a new booking.",
        "hi": "विज़िट के दिन हम आपको WhatsApp पर लाइव क्यू/टोकन अपडेट भेजेंगे।\n\nकुछ और चाहिए? बस मैसेज भेजें — डॉक्टर सर्च, सिम्पटम चेक, या नई बुकिंग।",
        "hg": "Visit ke din hum aapko WhatsApp par live queue/token updates bhejenge.\n\nKuch aur chahiye? Bas message bhejein — doctor search, symptom check, ya nayi booking.",
        "bn": "আমরা পরিদর্শনের দিন আপনাকে হোয়াটসঅ্যাপে লাইভ কিউ/টোকন আপডেট পাঠাব।\n\nআর কিছু দরকার? শুধু একটি বার্তা পাঠান — ডাক্তার খোঁজা, উপসর্গ পরীক্ষা, বা নতুন বুকিং।",
    },
    "booked_map_caption": {
        "en": "{hospital_name} — tap to see the location.",
        "hi": "{hospital_name} — लोकेशन देखने के लिए टैप करें।",
        "hg": "{hospital_name} — location dekhne ke liye tap karein.",
        "bn": "{hospital_name} — অবস্থান দেখতে ট্যাপ করুন।",
    },
    "error_hms": {
        "en": "Sorry, something went wrong on our end. Please try again in a moment.",
        "hi": "क्षमा करें, हमारी तरफ़ से कुछ गड़बड़ हो गई। कृपया थोड़ी देर में फिर कोशिश करें।",
        "hg": "Sorry, hamari taraf se kuch gadbad ho gayi. Thodi der mein phir try karein.",
        "bn": "দুঃখিত, আমাদের তরফ থেকে কিছু ভুল হয়েছে। অনুগ্রহ করে কিছুক্ষণ পর আবার চেষ্টা করুন।",
    },
    "error_hms_unreachable": {
        "en": "Our booking system is temporarily unavailable. Please try again shortly.",
        "hi": "हमारा बुकिंग सिस्टम अभी अस्थायी रूप से उपलब्ध नहीं है। कृपया थोड़ी देर बाद कोशिश करें।",
        "hg": "Hamara booking system abhi temporarily available nahi hai. Thodi der baad try karein.",
        "bn": "আমাদের বুকিং সিস্টেম সাময়িকভাবে অনুপলব্ধ। অনুগ্রহ করে শীঘ্রই আবার চেষ্টা করুন।",
    },
    "followup_reminder": {
        "en": "Hi {patient_name}, just checking in after your visit to {doctor_name} — how are you feeling? Reply if you need a follow-up booked.",
        "hi": "नमस्ते {patient_name}, {doctor_name} से मिलने के बाद बस हाल-चाल पूछ रहे हैं — अब कैसा महसूस कर रहे हैं? फॉलो-अप बुक करना हो तो जवाब दें।",
        "hg": "Hi {patient_name}, {doctor_name} se milne ke baad bas haal-chaal pooch rahe hain — kaisa feel kar rahe hain? Follow-up book karna ho to reply karein.",
        "bn": "হাই {patient_name}, {doctor_name}-এর কাছে আপনার পরিদর্শনের পর খোঁজ নিচ্ছি — আপনি কেমন অনুভব করছেন? ফলো-আপ বুক করতে হলে উত্তর দিন।",
    },
    "checkin_invalid_code": {
        "en": "This check-in code isn't valid. Please ask reception for help.",
        "hi": "यह चेक-इन कोड मान्य नहीं है। कृपया रिसेप्शन से मदद लें।",
        "hg": "Yeh check-in code valid nahi hai. Reception se madad le lijiye.",
        "bn": "এই চেক-ইন কোডটি বৈধ নয়। অনুগ্রহ করে রিসেপশনের সাহায্য নিন।",
    },
    "checkin_location_prompt": {
        "en": "You're checking in at {hospital_name}. Please share your location so we can confirm you're on-site — tap below.",
        "hi": "आप {hospital_name} पर चेक-इन कर रहे हैं। कृपया अपनी लोकेशन शेयर करें ताकि हम पुष्टि कर सकें कि आप वहाँ मौजूद हैं — नीचे टैप करें।",
        "hg": "Aap {hospital_name} par check-in kar rahe hain. Kripya apni location share karein taaki hum confirm kar sakein aap wahaan maujood hain — neeche tap karein.",
        "bn": "আপনি {hospital_name}-এ চেক-ইন করছেন। অনুগ্রহ করে আপনার অবস্থান শেয়ার করুন যাতে আমরা নিশ্চিত করতে পারি আপনি সেখানে আছেন — নিচে ট্যাপ করুন।",
    },
    "checkin_too_far": {
        "en": "You don't seem to be at the hospital yet. Please make sure you're on-site and share your location again.",
        "hi": "लगता है आप अभी अस्पताल पर नहीं हैं। कृपया सुनिश्चित करें कि आप वहाँ हैं और दोबारा लोकेशन शेयर करें।",
        "hg": "Lagta hai aap abhi hospital par nahi hain. Kripya confirm karein aap wahaan hain aur dobara location share karein.",
        "bn": "মনে হচ্ছে আপনি এখনও হাসপাতালে নেই। অনুগ্রহ করে নিশ্চিত করুন আপনি সেখানে আছেন এবং আবার আপনার অবস্থান শেয়ার করুন।",
    },
    "checkin_no_appointment": {
        "en": "We couldn't find an appointment for today under this number. Please check in at reception.",
        "hi": "इस नंबर पर आज के लिए कोई अपॉइंटमेंट नहीं मिली। कृपया रिसेप्शन पर चेक-इन करें।",
        "hg": "Is number par aaj ke liye koi appointment nahi mili. Kripya reception par check-in karein.",
        "bn": "এই নম্বরে আজকের জন্য কোনো অ্যাপয়েন্টমেন্ট পাওয়া যায়নি। অনুগ্রহ করে রিসেপশনে চেক-ইন করুন।",
    },
    "checkin_choose_appointment": {
        "en": "We found more than one appointment for you today. Please choose one:",
        "hi": "आज आपके लिए एक से ज़्यादा अपॉइंटमेंट मिली हैं। कृपया एक चुनें:",
        "hg": "Aaj aapke liye ek se zyada appointments mili hain. Kripya ek choose karein:",
        "bn": "আজ আপনার জন্য একাধিক অ্যাপয়েন্টমেন্ট পাওয়া গেছে। অনুগ্রহ করে একটি বেছে নিন:",
    },
    "checkin_choose_button": {
        "en": "Choose",
        "hi": "चुनें",
        "hg": "Choose karein",
        "bn": "বেছে নিন",
    },
    "checkin_success": {
        "en": "You're checked in! Your token number is #{token_no}. We'll message you here as the queue moves.",
        "hi": "आप चेक-इन हो गए हैं! आपका टोकन नंबर #{token_no} है। क्यू आगे बढ़ने पर हम आपको यहाँ मैसेज करेंगे।",
        "hg": "Aap check-in ho gaye hain! Aapka token number #{token_no} hai. Queue aage badhne par hum aapko yahaan message karenge.",
        "bn": "আপনি চেক-ইন হয়ে গেছেন! আপনার টোকেন নম্বর #{token_no}। কিউ এগোলে আমরা আপনাকে এখানে মেসেজ করব।",
    },
    "checkin_failed": {
        "en": "We couldn't check you in right now. Please check in at reception.",
        "hi": "अभी हम आपको चेक-इन नहीं कर पाए। कृपया रिसेप्शन पर चेक-इन करें।",
        "hg": "Abhi hum aapko check-in nahi kar paaye. Kripya reception par check-in karein.",
        "bn": "এখন আমরা আপনাকে চেক-ইন করতে পারিনি। অনুগ্রহ করে রিসেপশনে চেক-ইন করুন।",
    },
    "discharge_not_available": {
        "en": "No discharge summary is available yet. Please check with the hospital.",
        "hi": "अभी कोई डिस्चार्ज समरी उपलब्ध नहीं है। कृपया अस्पताल से संपर्क करें।",
        "hg": "Abhi koi discharge summary available nahi hai. Kripya hospital se check karein.",
        "bn": "এখনও কোনো ডিসচার্জ সামারি পাওয়া যায়নি। অনুগ্রহ করে হাসপাতালের সাথে যোগাযোগ করুন।",
    },
    "prescription_not_available": {
        "en": "No prescription is available yet. Please check with the doctor or hospital.",
        "hi": "अभी कोई प्रिस्क्रिप्शन उपलब्ध नहीं है। कृपया डॉक्टर या अस्पताल से संपर्क करें।",
        "hg": "Abhi koi prescription available nahi hai. Kripya doctor ya hospital se check karein.",
        "bn": "এখনও কোনো প্রেসক্রিপশন পাওয়া যায়নি। অনুগ্রহ করে ডাক্তার বা হাসপাতালের সাথে যোগাযোগ করুন।",
    },
    "discharge_delivered": {
        "en": "Here's your discharge summary. 📄",
        "hi": "यह रही आपकी डिस्चार्ज समरी। 📄",
        "hg": "Yeh rahi aapki discharge summary. 📄",
        "bn": "এই যে আপনার ডিসচার্জ সামারি। 📄",
    },
    "prescription_delivered": {
        "en": "Here's your prescription. 📄",
        "hi": "यह रहा आपका प्रिस्क्रिप्शन। 📄",
        "hg": "Yeh raha aapka prescription. 📄",
        "bn": "এই যে আপনার প্রেসক্রিপশন। 📄",
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
