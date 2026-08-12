# Architecture notes — `flow_policy.py` aur "single authority" design

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

## 5. Kya scope se BAAHAR rakha (deliberately, is phase mein)

Poori proposed architecture mein `conversation.py` (2380 lines) ko domain-wise 7 files
mein split karna bhi tha (`doctor_search.py`, `patient_details.py`, `checkin.py`,
waghera). **Ye is phase mein NAHI kiya** — jaan-boojh ke:

- 60+ existing tests directly `conversation.<module>` attributes ko monkeypatch karte
  hain (`conversation.db = MockDB()` jaisa) — ek full split un saare patch-targets ko
  ek saath badal deta, regression risk bahut high hoti bina proportionate benefit ke
  is single session mein.
- Asli, demonstrated bug (`awaiting_clarification` loop) `flow_policy.py` se hi fix ho
  gaya — file-split us bug ko fix karne ke liye zaroori nahi tha.

**Agla step (jab bhi karna ho)**: `conversation.py` split — ek alag, dedicated session
mein, phase-by-phase (jaisa is poore session mein har bada change hua hai), ek module
ek baar mein migrate karke, har step pe poora test-suite green rakhte hue.

## 6. Files jo is change mein bane/badle

- **NAYA**: `app/flow_policy.py` — arbitration logic + apna self-test (`python3 app/flow_policy.py`)
- **`app/intent_router.py`**: `clear_session(wa_id)` function add hua
- **`app/conversation.py`**: `route_intent()` call se pehle `flow_policy.is_global_override()` check
- **`test_nlu_integration.py`**: 2 naye tests — bug reproduce karta hua (`test_global_intent_escapes_awaiting_clarification_loop`) aur non-regression check (`test_legitimate_followup_answer_not_treated_as_a_global_override`, taaki normal multi-turn follow-ups jaise "gyno" galti se clear na ho jaayein)
