# Architecture notes — "single authority" design aur `conversation.py` split

Ye document explain karta hai ki `app/flow_policy.py` kyun banaya gaya, kaunsa asli bug
isne fix kiya, aur poori conversation-handling architecture ke liye iska kya matlab hai.
Agla developer (ya future-me) jab bhi koi naya "sub-state" ya "mini follow-up flow" add
karne ki soche, usse ye samajhna chahiye ki wo kis contract ko honor karna hai.

## 1. Jo bug mila (screenshot se traced)

Patient ka already ek active appointment tha (2026-08-12). Unhone naya appointment book
karne ki koshish ki, toh bot ne poocha:

> "You already have an appointment on 2026-08-12. Would you like to book a new one, or
> reschedule it?"

Iske baad patient ne sirf **"hi"** bheja — bot ne **wahi exact same sawaal phir se**
bheja. "cancel" try kiya — **wahi sawaal phir se**. Patient literally is loop se nikal
hi nahi sakta tha, jab tak Redis ka 15-minute session TTL khud expire na ho jaaye.

## 2. Root cause — do alag "state machines" jo ek doosre se anjaan hain

Is codebase mein conversation ka state **do jagah** track hota hai:

1. **SQL `conversation_state` + `app/booking_slots.py`'s clipboard** — ye poore session
   mein humne jo kaam kiya (Dr Avinash bug, location-retry, symptom-concern message
   waghera), sab isi system ke through the. Iska apna docstring hi kehta hai: *"Ek hi
   function (next_action) decide karta hai ab kya karna hai"* — matlab ye SOLE authority
   banne ke liye design hua tha.

2. **Redis `nlu:session:<wa_id>` (`app/intent_router.py`)** — ye multi-turn NLU
   slot-filling ke liye hai (jaise "gyno" phir "kal" — do messages mila ke ek booking
   request banana). Isko bhi **poore turn ko override karne ki power** hai:
   `route_intent()` jab `action="ask_followup"` return karta hai, `handle_message()`
   turant `return` ho jaata hai ([app/conversation.py](../app/conversation.py), line ~502) —
   iske baad wale saare handlers (cancel/back/greeting sameth) kabhi chalte hi nahi.

`intent_router.py`'s apna hi comment (line ~141) is problem ko already documented karta
hai:

> "This Redis session and the SQL conversation_state are two independent stores of
> 'what's going on' — nothing keeps them in sync."

`awaiting_clarification` branch (jab patient se "naya book karna hai ya reschedule"
poocha jaata hai) ke paas sirf do chhoti keyword-lists thi:

```python
is_reschedule = any(k in raw_normalized for k in ["reschedule", "change", "shift", "badal", "parso", "kal", "time"])
is_new_booking = any(k in raw_normalized for k in ["new", "naya", "dusra", "another", "fresh", "book"])
```

"hi" in dono mein se kisi mein nahi aata → `else: keep asking` branch fire hota tha →
**wahi sawaal phir se, forever.**

Ye koi ek-off typo/bug nahi tha — ye ek **failure class** hai: jab bhi koi naya
sub-state (Redis session, `pending_specialty` flag, `search_doctor_query` flag) System A
(clipboard) ke upar layer kiya jaata hai, usse System A ka "cancel/back/greeting hamesha
kaam karega" wala global contract automatically nahi milta, jab tak koi explicitly wire
na kare.

## 3. Fix: `flow_policy.py` — single arbitration point

`app/flow_policy.py` naya module hai jiska sirf ek kaam hai: **"is CURRENT message ko,
jo bhi local/sub-state chal raha ho, usse override kar dena chahiye ya nahi?"**

```python
GLOBAL_INTENTS = {"cancel_appointment", "navigate_back", "greeting"}

def is_global_override(intent: str | None, confidence: str | None) -> bool:
    ...
```

Ye check `handle_message()` mein `intent_router.route_intent()` ko call karne se
**pehle** hota hai:

```python
if flow_policy.is_global_override(raw_nlu_result.get("intent"), raw_nlu_result.get("confidence")):
    await intent_router.clear_session(phone)

routed = await intent_router.route_intent(phone, raw_nlu_result, input_value, lang, current_step)
```

Jab "hi"/"cancel"/"back" aata hai, Redis session pehle hi clear ho jaata hai — isliye
`route_intent()` ko koi stale/awaiting_clarification state milta hi nahi, aur wo
naturally apne normal path se guzarta hai (jahan cancel/back/greeting already handle
hote hain, humesha se).

**Kya nahi badla**: koi teesri keyword-list nahi banayi. Wahi purana problem sirf ek aur
jagah repeat hota — is fix ka poora point hi ye hai ki *koi bhi* future sub-state agar
`flow_policy.is_global_override()` ko check kare (ya usse guzarne wale kisi common path
se ho ke jaaye), toh wo automatically safe rahega, koi naya keyword-list maintain nahi
karni padegi.

## 4. SOLID mapping (jo tumne poocha tha)

| Principle | Yahan kaise |
|---|---|
| **S**ingle Responsibility | `flow_policy.py` ka sirf ek kaam hai: "kya ye message global override hai" — na routing, na NLU, na Redis I/O |
| **O**pen/Closed | Naya global intent add karna sirf `GLOBAL_INTENTS` set mein ek entry hai — `intent_router.py` ya `conversation.py` ka koi existing if/elif edit nahi karna padta |
| **L**iskov Substitution | `is_global_override(intent, confidence) -> bool` — koi bhi caller isi signature se predictably use kar sakta hai, chahe future mein koi aur module bhi isse consult kare |
| **I**nterface Segregation | Bahut narrow surface — sirf ek function, koi extra cheez expose nahi karta jo caller ko chahiye hi nahi |
| **D**ependency Inversion | `intent_router.py` ab conceptually "flow_policy ke rules ka pabandh" hai (via `clear_session` jo `conversation.py` calls karta hai), na ki `conversation.py` ko `intent_router`'s internal keyword-lists ke baare mein pata hona chahiye |

## 5. `conversation.py` split — baad mein poora hua (dusra session, dusra plan)

Section pehle yahan kehta tha ki poora `conversation.py` split "is phase mein nahi
kiya" — ye ab **complete ho chuka hai**, ek dedicated, approved plan ke through
(`~/.claude/plans/expressive-seeking-lemon.md`), 15 chhote phases mein, har phase ke
baad poora 8-file test-suite green confirm kar ke, koi test file change kiye bina.

**`app/conversation.py` ab ek PACKAGE hai** (`app/conversation/__init__.py` + 9 sibling
files), na ki ek 2661-line ki single file:

```
app/conversation/
  __init__.py            — orchestrator + 9 "mutated names" (neeche dekho) + _phrase
  shared.py               — _match_choice
  language.py             — language detection + confirm/choose/start handlers
  location.py             — location capture + resolve + choosing_location handler
  doctor_search.py        — doctor/hospital-name search (full flow)
  specialty_browsing.py   — symptom/specialty search + browse + sort-prompt
  doctor_list.py          — doctor math/formatting (fee, rating, distance, description)
  slot_selection.py       — date & slot selection (full flow)
  patient_details.py      — patient-details text parsing (pure)
  booking_confirmation.py — confirmation-line formatting (pure)
  checkin.py               — OPD check-in + all QR-trigger flows (full)
```

`__init__.py`: 2,661 → ~1,353 lines. Baaki sab 6 sibling files mein `handle_message`,
`_advance_booking_flow`, `_step_for_action`, `_transition_to`, `_get_or_create_clipboard`,
`_init_clipboard_from_legacy` (core orchestration), aur **9 "mutated names"** — jo test
suite directly reassign karta hai (`conversation.db = MockDB()` jaisa): `db`,
`whatsapp_client`, `_clinic_now`, `_safe_city_index`, `_fetch_doctors_near`,
`_render_doctor_list`, `_get_offered_slots`, `_advance_booking_flow`,
`_send_patient_details_flow`. In 9 ko permanently `__init__.py` mein rehna hai — kabhi
move nahi karna (neeche wajah dekho).

### Kyun sirf "code move karna" kaafi nahi tha (Python monkeypatch ka subtlety)

Jab `conversation.py` ek hi file thi, `conversation.db = MockDB()` jaisa monkeypatch
kaam karta tha kyunki Python ek unqualified call (`db.get_conversation_state(...)`) ko
us function ke **apne module `__dict__`** mein call-time pe resolve karta hai — sab
functions ek hi module mein the, isliye sab ek hi namespace share karte the.

File split karte hi ye TOOT jaata agar naive tarike se karte: agar `_usable_shifts`
(jo `_clinic_now()` call karti hai) `slot_selection.py` mein chali jaati AUR `_clinic_now`
bhi wahi chala jaata, toh `conversation._clinic_now = mock` sirf `__init__.py`'s apne
re-export ko reassign karta — `slot_selection.py`'s andar wali call abhi bhi apne
module ke original `_clinic_now` ko dekhti, patch ka koi asar nahi hota. **Test crash
nahi hota, bas silently test kuch aur hi verify karne lagta** — sabse dangerous class
ka bug.

**Fix — har cross-file call jo `__init__.py` mein wapas jaati hai, function-body-local
import use karti hai:**

```python
async def _usable_shifts(...):
    from app import conversation   # yahan import, top pe nahi
    now = conversation._clinic_now()   # har call pe fresh lookup — patches dikhte hain
```

Ye pattern is file mein hi pehle se precedented tha (`_DOCUMENT_TRIGGERS`'s resolver
lookup comment, `app/conversation/checkin.py`) — bas ab consistently har sibling file
mein use kiya gaya, har jagah jo `__init__.py`'s kisi bhi name (9 mutated names ho ya
sirf core-orchestration function ho, jaise `_transition_to`) ko call karti hai.

**Verify kiya** ki ye genuinely kaam karta hai — `_usable_shifts` (jo `_clinic_now` ko
call karti hai) ke liye already ek test tha jo exactly isi shape ko test karta hai
(`test_specialty_groups.py`'s `_shifts_at()` helper: `conversation._clinic_now = mock`
phir `conversation._usable_shifts(...)` call karta hai) — split ke baad ye test
unchanged pass hua, confirm karta hai ki lazy-import pattern ne patch ko cross-file
bhi zinda rakha.

## 6. Files jo `flow_policy.py` fix mein bane/badle

- **NAYA**: `app/flow_policy.py` — arbitration logic + apna self-test (`python3 app/flow_policy.py`)
- **`app/intent_router.py`**: `clear_session(wa_id)` function add hua
- **`app/conversation/__init__.py`**: `route_intent()` call se pehle `flow_policy.is_global_override()` check
- **`test_nlu_integration.py`**: 2 naye tests — bug reproduce karta hua (`test_global_intent_escapes_awaiting_clarification_loop`) aur non-regression check (`test_legitimate_followup_answer_not_treated_as_a_global_override`, taaki normal multi-turn follow-ups jaise "gyno" galti se clear na ho jaayein)

## 7. Ab agla kya karein (agar aur split karna ho)

3 functions jaan-boojh kar `__init__.py` mein hi rehne diye — `_send_doctor_list`,
`_handle_awaiting_patient_details`, `_handle_confirming`. Har ek 3-5 mutated names
ek saath touch karta hai aur file ke sabse elaborate/dense tests inhi pe hain
(jaise `_handle_awaiting_patient_details` ek test se exercise hota hai jo
`_advance_booking_flow` AUR `_send_patient_details_flow` dono ko saath patch karta
hai aur unki interaction assert karta hai). Marginal line-count benefit ke liye risk
lene layak nahi laga, is phase mein.

Agar future mein inhe bhi move karna ho: same lazy-import discipline follow karo
(`from app import conversation` function ke andar, `conversation.<name>` se access),
aur move karne se PEHLE unke specific tests ko dhyan se padho ki wo kis exact
interaction ko verify kar rahe hain.
