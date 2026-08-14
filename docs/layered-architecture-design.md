# Layered architecture — WhatsApp-Webhook

**Audience**: any engineer touching this repo — today that's realistically 1-2 people
(108 commits from Md Aquib, 27 from Tasquil Noori, one further account with 1 commit, per
`git log`) — plus a future hire who needs to find the right file for a change without
reading all 7,246 lines first. This document exists so "where does my change go" has one
answer, and so nobody re-derives the layering from scratch the next time the codebase
grows. It is written for this specific repo — every module named below is real, and every
line reference was checked against the current tree, not assumed.

The layer count is **7 sequential layers + 3 cross-cutting concerns**. That number wasn't
picked in advance — it fell out of walking the actual request flow (Step 1 below) and
capping it against a 2-person team on ~7.2k lines. A bigger team or a different domain
would legitimately land somewhere else.

## The 7 layers, in plain words

Each layer gets a one-word **entity name** — a simple handle you can use in conversation
("that's a Conductor bug", "put it in the Messenger layer") instead of the longer
technical term. Both names mean the same thing everywhere in this document; the entity
name is just easier to say and remember.

| # | Entity | Technical name | What it's like |
|---|---|---|---|
| 1 | 🚪 **Front Door** | Ingress & Delivery | The receptionist — checks the message is really from WhatsApp, makes sure it isn't a duplicate, lets it in. |
| 2 | 👂 **Listener** | Understanding (NLU) | Hears what the patient said and guesses what they meant. Never assumed to be right. |
| 3 | ⚖️ **Referee** | Session Arbitration | Decides who gets to speak this turn — an in-progress question, or whatever the patient just said (which sometimes should just override everything). |
| 4 | 🧭 **Conductor** | Orchestration (Dispatch/FSM) | Knows which step of the booking the patient is on, and what should happen next. |
| 5 | 🛠️ **Specialists** | Domain Handlers | One expert per topic — language, location, doctor search, slots, check-in — each does its own job only. |
| 6 | 🧠 **Decision Maker** | Decide (pure logic) | Pure thinking, no phone calls — given facts, works out an answer. Nothing here ever waits on a network. |
| 7 | 📡 **Messengers** | Fetch / External Adapters | The only ones allowed to actually phone another system — 1HMS, WhatsApp, the database, Redis. |

Three more things don't sit *in* the flow at all — they run alongside every layer:

| Entity | Technical name | What it's like |
|---|---|---|
| 🚨 **Safety Guard** | Safety Guardrail | Watches every single message for a medical emergency, before anything else happens. |
| 🌐 **Translator** | Presentation (i18n) | Owns every word the patient actually reads, in 4 languages. |
| ⚙️ **Settings Book** | Config | The one place every other layer looks up its settings. |

## Why this layering, and not something fancier

Three heavier options were seriously on the table at different points this project's
life, and none of them clear their cost for this repo, for reasons already established —
one of them not by me, but by a decision this team already made and shipped:

**1. A full Hexagonal / Ports-and-Adapters rewrite.** Discussed in depth earlier this
session. The verdict stands: this codebase already gets Hexagonal's actual benefit — the
**Messenger** layer (`hms_client.py`, `whatsapp_client.py`, `db.py`, `symptom_client.py`)
already isolates exactly one external system per file behind an async-function interface,
and the **Decision Maker** layer (`resolver.py`, `geo.py`, `booking_slots.py`) is already
zero-mock-tested pure logic (`test_resolver.py` imports `resolve_doctor` /
`resolve_location_from_gps` / `resolve_location_from_text` directly, no mocking needed).
Formalizing this into `Port`/`Adapter` protocol classes and a DI container would rename
things that already work and add a class hierarchy this codebase has never needed (5
classes total in the whole tree, none with custom inheritance — see the module table
below). The seam Hexagonal wants already exists; only the label was missing, and the
label isn't worth the ceremony for 2 maintainers.

**2. The full `booking_slots.py` clipboard cutover (Phase 3-B) — already proposed,
investigated, and explicitly rejected**, per project memory
([[whatsapp-bot-clipboard-architecture]]). The plan was to replace the Conductor's 3-way
`current_step` dispatch with a single `next_action()`-driven walk, eliminating the very
duplication this document's "one finding that matters most" section flags. Investigating
it showed the `elif current_step` chain is mostly button/list-tap dispatch — a tap carries
no ambiguity to resolve, so the pure-slot-filling rewrite bought almost no user-facing
gain for roughly 1,700 lines of rewrite risk. This document does **not** re-propose that
cutover; the fix recommended below (Step 8) is deliberately smaller than the option this
team already looked at and turned down.

**3. Microservices / one process per layer.** The whole pipeline already runs as two
processes (`webhook.py`'s FastAPI app and `worker.py`'s asyncio consumer — Front Door is
literally already split across a process boundary), connected only by a Redis list
(`settings.booking_jobs_key`) — that boundary is real and already pays for itself (webhook
ingestion never blocks on NLU/1HMS latency). Splitting the other six layers into separate
services would multiply deploy surfaces and network hops for a system with one
clinic-facing traffic pattern and no team ownership boundary that would benefit from it.
Nothing in Step 0's facts justifies this cost.

What this layering *does* do differently from today's informal structure: it names the
**Decision Maker** layer as a first-class, protected boundary — pure, zero-I/O, cheap-to-
test-with-plain-asserts — and calls out that `city_index.py` and `symptom_client.py` sound
like they belong there by name/proximity but don't (both perform real I/O — see the module
table). That distinction is already implicit in `resolver.py`'s own docstring ("Pure
functions, koi I/O nahi... 'fetch' (city_index.py, hms_client.py) 'decide' (yeh file) se
alag rehta hai"); this document just makes it explicit and enforceable.

## Diagram

See [`architecture-layers.mermaid`](architecture-layers.mermaid) for the full flowchart.

**Read it as three kinds of line, not one:**

1. **Solid + numbered (1→9)** — one message's actual journey, front door to reply, in
   order. This is the only thing on the diagram that's numbered, on purpose, so a number
   always means "the main road."
2. **Double-headed** — a call to an external system that comes back with an answer
   (Sarvam, 1HMS, SQL Server, Redis, NexEagleWebsite). Earlier drafts of this diagram drew
   these as one-way arrows into the external box — Sarvam in particular looked like it
   swallowed the request and never answered. That was a diagram bug, not a system bug: the
   code always awaits a response (`nlu_client.classify_message` returns the LLM's
   classification, `hms_client` calls return JSON, `db.py` calls return rows). Every
   external call is now a single two-headed arrow so "ask, get an answer back" reads as
   one relationship. Meta WhatsApp is the one deliberate exception — the inbound webhook
   and the outbound reply are genuinely two separate, decoupled HTTP calls (the webhook
   already returns `{"status":"ok"}` before any reply is generated), so it keeps two
   one-directional arrows rather than a false round-trip.
3. **Dotted** — either a shortcut (the Conductor or Decision Maker calling a later layer
   directly, skipping steps for some flows) or a cross-cutting lookup (Safety, the
   Translator, the Settings Book). Never numbered, never part of the main count.

Each of the 7 sequential layers also has its own colour, running blue → purple in flow
order, so you can tell where you are in the pipeline without reading a single label. The 3
cross-cutting concerns are hexagons in a separate red/amber/grey family, off to the side —
never inside the numbered flow — to make clear they aren't "one more step," they run
alongside everything. A legend box is included directly in the diagram so none of this
needs to be explained verbally when walking a team through it.

### Layer definitions

| Entity | Job in one sentence | Rule of thumb |
|---|---|---|
| 🚪 Front Door | Turn a raw Meta webhook POST (or a queued job) into a validated, deduped call into the Conductor. | No business logic here — if a function starts reasoning about doctors/slots/language, it's in the wrong layer. |
| 👂 Listener | Turn free text into a structured, *validated* intent+entities guess. | Never trusted for booking facts (doctor names, fees, times) — only for navigation/intent, per project memory. Everything it returns is re-validated against `VALID_INTENTS`/`VALID_ENTITIES`/`VALID_LANGUAGES` before use. |
| ⚖️ Referee | Decide whose "what's happening right now" wins: a multi-turn NLU accumulation, or the current message. | Any new turn-taking sub-state MUST consult `flow_policy.is_global_override()` before trusting its own local state — that's the whole reason this module exists. |
| 🧭 Conductor | Own `current_step`, decide what handler runs next, persist state. | If a step name needs to be added/changed, it belongs here — never invent a second place that also knows step names (see Finding, below). |
| 🛠️ Specialists | Do the actual conversational work for one step (search, format, ask, confirm). | One file per conversational concern; a handler reads/writes `context` but never owns step-transition logic itself. |
| 🧠 Decision Maker | Turn already-fetched data + already-known facts into a decision, with zero I/O. | If a function in here needs `await`, it's not Decision-Maker code — move it to Messengers. This is the strictest, most enforceable rule in the whole layering. |
| 📡 Messengers | Own exactly one external system's wire format and failure modes. | One file per external system. A second file that also calls the same system (e.g. a second SQL query builder) is a layering violation — see Finding. |
| 🚨 Safety Guard *(cross-cutting)* | Intercept clinical emergencies before any slot/NLU work happens. | Runs first, every text message, unconditionally — never gated behind a step. |
| 🌐 Translator *(cross-cutting)* | Own every user-facing string in 4 languages. | No `f"..."` patient-facing text outside `i18n.py`'s `t()` — a literal string in a handler is a layering violation of this concern. |
| ⚙️ Settings Book *(cross-cutting)* | Own environment-driven settings. | `Settings` (Pydantic) only — no module re-derives its own env-var reads. |

## Module classification

| Entity | Files today | Status | Comment |
|---|---|---|---|
| 🚪 Front Door | `app/webhook.py` (319 ln), `worker.py` (~70 ln) | Clean, but **untested** | `_verify_signature` (webhook.py:150), `_extract_messages_and_contacts` (:160), `_input_type_and_value` (:175), `receive_webhook` (:205) and `worker.py`'s `handle_job` (:15) have no dedicated test file — see Finding below. |
| 👂 Listener | `app/nlu_client.py` (345), `app/nlu_validator.py` (100), `app/nlu_config.py` (172), `app/model_config.py` (29) | Clean | `nlu_validator.py`'s whole job is "never trust the LLM's raw output" (`nlu_validator.py:44`'s `hallucinated_intent` check) — a textbook Listener-layer boundary. |
| ⚖️ Referee | `app/intent_router.py` (344), `app/flow_policy.py` (98) | Mostly clean, one leak | `intent_router.py:302-313` runs a raw SQL query via `db.get_pool()` directly instead of calling `db.py`'s own `has_pending_appointment()` (`db.py:84-92`) — the Referee reaching straight past the Messenger boundary into raw SQL. Same finding as the prior SOLID review's DIP violation, restated here as a **layer-boundary breach**. |
| 🧭 Conductor | `app/conversation/__init__.py` (1,353 ln) | Needs work | Owns `handle_message` (:239), `_step_for_action` (:134), `_trigger_step_prompt` (:1274), `_advance_booking_flow` (:172) — three separate step-name dispatch surfaces live here; see Finding. |
| 🛠️ Specialists | `app/conversation/{language,location,doctor_search,specialty_browsing,doctor_list,slot_selection,patient_details,booking_confirmation,checkin}.py` (9 files, 92-308 ln each) | Clean | Each file maps to one conversational concern; none call `httpx`/`aioodbc` directly (verified — every I/O call in these files goes through the Messenger layer). |
| 🧠 Decision Maker | `app/booking_slots.py` (202), `app/resolver.py` (230), `app/geo.py` (22) | Clean, best-tested layer in the repo | Zero `await` in any of the three; `test_resolver.py` and `booking_slots.py`'s own `__main__` self-test exercise them with plain asserts, no mocks. |
| 📡 Messengers | `app/hms_client.py` (285), `app/whatsapp_client.py` (232), `app/db.py` (344), `app/redis_client.py` (10), `app/symptom_client.py` (90), `app/city_index.py` (197) | Mostly clean, one naming trap | `city_index.py` and `symptom_client.py` sound Decision-Maker-ish (names suggest "resolve"/"route") but both do real I/O (`city_index.py` pages `hms_client`'s doctor list; `symptom_client.py` makes its own `httpx` call to NexEagleWebsite's proxy) — correctly classified as Messengers, but worth flagging so nobody copies pure-logic conventions into them by analogy. |
| 🚨 Safety Guard | `app/safety.py` (114) | Clean, narrow | Single call site (`conversation/__init__.py:300`) but designed to scale ("Safety Interceptor Gateway... designed to scale to full clinical triage integrations" — safety.py's own docstring) — cross-cutting by intent even though only wired once today. |
| 🌐 Translator | `app/i18n.py` (873) | Clean | 9 files import `t()`/`LANGUAGE_LABELS`; zero inline patient-facing strings found in Specialists or the Conductor during this review. |
| ⚙️ Settings Book | `app/config.py` (96), `app/model_config.py` (29 — Listener-specific, since its only consumer is `nlu_client.py`) | Clean | `Settings` imported by 11 files across every layer. |

Every file under `app/` is accounted for above; nothing is left unclassified.

## The one finding that matters most

**The Front Door (`webhook.py` + `worker.py`) has zero automated test coverage, and it's
the layer holding this system's only two safety-critical guarantees: webhook authenticity
(HMAC signature verification, `webhook.py:150`) and delivery exactly-once-ness (the
`booking:dedupe:{message_id}` Redis `SET NX` at `webhook.py:227`, backed by
`db.is_message_processed` at `worker.py:22`).**

This is the top pick over two other real candidates, and here's why it outranks them:

- It outranks the Referee-to-Messenger SQL leak (`intent_router.py:302-313`) because that
  leak is at least exercised indirectly — every test that sets up an active-appointment
  scenario runs that code path, even though the raw SQL text itself isn't asserted on. The
  Front Door's HMAC/dedupe logic is exercised by *nothing*: no test constructs a signed or
  unsigned payload and checks `_verify_signature`'s boolean result, and no test proves a
  replayed `message_id` is actually dropped rather than double-processed.
- It outranks the Conductor's 3-way step-dispatch duplication (`_step_for_action` /
  `handle_message`'s `elif current_step` / `_trigger_step_prompt`) because that one, while
  real, has already been investigated once this project's life (the clipboard-cutover
  memory above) and has a documented historical bug *with a regression test added after
  the fact* (`e42fe21`'s fix for the missing `("retry","location")` case). The Front Door
  has no incident on record yet — which is exactly the risk: a broken dedupe or a
  signature check that silently accepts unsigned payloads would show up as a *production*
  incident (double bookings, or a spoofed message pretending to be a patient) before it
  showed up as a failing test, because there's no failing test to catch it first.

The fix is small relative to the risk: one new `test_webhook.py` that (a) constructs a
payload, computes a valid HMAC over it with a known `whatsapp_app_secret`, and asserts
`_verify_signature` accepts it and rejects a tampered/unsigned one; and (b) calls
`handle_job` twice with the same `message_id` against a mock `db.is_message_processed`
that flips from `False`→`True`, asserting the second call is a no-op. Both functions are
already pure enough to test directly — `_verify_signature` takes bytes and a header string,
no I/O; `handle_job` only needs `db` mocked, matching every other test file's established
pattern of reassigning `conversation.db`/`intent_router.db`.

## The four qualities

### Testable

**Rule**: no more than 2 mocked dependencies to exercise one function in the Conductor,
Specialists, or Decision Maker layers; Messengers and the Front Door are allowed more
since they're the I/O boundary by design, but must still be testable via a single
object-reassignment, matching this project's established convention.

- **Decision Maker — holds, cleanly.** `test_resolver.py` imports `resolve_doctor` etc.
  directly with **zero** mocks — plain data in, `Resolution` out.
- **Specialists — holds.** `test_specialty_groups.py`'s `_shifts_at()` helper patches
  exactly one name (`conversation._clinic_now`) to exercise `_usable_shifts`.
- **Conductor — holds, but densely.** `_handle_awaiting_patient_details` needs
  `_advance_booking_flow` *and* `_send_patient_details_flow` patched together to assert
  their interaction — 2 mocks, at the stated ceiling, and it's already the most elaborate
  test in the suite (flagged for exactly this reason in the conversation.py-split plan's
  "stopping point" section — kept in `__init__.py` rather than moved, specifically because
  of this test density).
- **Front Door — fails the rule entirely, because there's nothing to measure.** Zero tests
  exist, so "how many mocks would it need" is unanswered. This is restated evidence for
  the finding above, not a new one.

### Scalable

**Rule**: use the Step 0 traced example — adding one new global intent (like
`cancel_appointment`) — and count files touched. Additive (new entries in existing
collections) is fine; needing new if/elif branches anywhere is the failure signal.

Verified by grep, not estimated: adding a new global intent today touches **4
independently-maintained collections across 3 files** — `nlu_config.py:10`
(`VALID_INTENTS`), `intent_router.py:11-20` (`REQUIRED_ENTITIES`), `intent_router.py:87`
(`_NO_SLOT_SAFETY_NET`), and `flow_policy.py:43` (`GLOBAL_INTENTS`) — plus a manual add to
the Conductor's "Prioritize NLU global intents" `if/elif` block
(`conversation/__init__.py:429-497+`). None of the four collections structurally requires
the others to be updated; nothing errors if one is forgotten, it just silently doesn't
behave as global. The Referee/Conductor boundary is the layer that scales worst today.

By contrast, adding a new **language** is genuinely additive: one entry to
`nlu_config.py:38`'s `VALID_LANGUAGES`, one to `i18n.py:15`'s `LANGUAGE_LABELS`, and new
values for `i18n.py`'s ~40 `t()` keys — all in the Translator, no dispatch logic touched,
no new branches. The Translator scales cleanly; the Referee/Conductor's intent-registration
doesn't.

### Readable

**Rule, pulled from this repo's own file-size distribution** (see the line-count table in
the module classification section): every file in the Listener, Referee, Specialists,
Decision Maker, Messenger layers sits under 350 lines; the two outliers are
`conversation/__init__.py` (1,353) and `i18n.py` (873). `i18n.py`'s size is expected and
fine — it's a flat, append-only string table, not logic density (the Translator is allowed
to be long; it's never allowed to be *tangled*). The Conductor at 1,353 lines, carrying
real branching logic (three separate step-name dispatch surfaces), is the one file where
size correlates with actual comprehension risk. **Rule of thumb for this repo: a Conductor
file over ~800 lines, or any file outside the Translator over ~350 lines, is a signal to
look for a second responsibility hiding inside it** — that threshold is exactly what
flagged the original `conversation.py` (2,661 lines pre-split) and is why the split
happened.

### Understandable

**Rule**: every common change type from Step 0 must appear in the "Where do I make my
change" table below, with no gaps — an incomplete table means an incomplete
understandability claim. Checked below; all identified common change types are covered.

### Scorecard — after this document's recommended changes (Step 8)

| Entity | Testable | Scalable | Readable | Understandable |
|---|---|---|---|---|
| 🚪 Front Door | ✅ (after `test_webhook.py`) | ✅ | ✅ | ✅ |
| 👂 Listener | ✅ | ✅ | ✅ | ✅ |
| ⚖️ Referee | ✅ | ✅ | ✅ | ✅ |
| 🧭 Conductor | ⚠️ (2-mock ceiling, by design — see above) | ⚠️ (intent registration still 3-file) | ✅ | ✅ |
| 🛠️ Specialists | ✅ | ✅ | ✅ | ✅ |
| 🧠 Decision Maker | ✅ | ✅ | ✅ | ✅ |
| 📡 Messengers | ✅ (after routing `intent_router.py`'s query through `db.py`) | ✅ | ✅ | ✅ |

## Where do I make my change

| Change type | File(s) | Notes |
|---|---|---|
| New WhatsApp message type (e.g. a new interactive reply shape) | Front Door — `app/webhook.py`'s `_input_type_and_value` (:175) | Front-Door-only; nothing downstream needs to know until it becomes a `(type, value)` pair. |
| New global intent (always-works, any step) | `app/nlu_config.py` (`VALID_INTENTS`), `app/intent_router.py` (`REQUIRED_ENTITIES`, `_NO_SLOT_SAFETY_NET`), `app/flow_policy.py` (`GLOBAL_INTENTS`), Conductor's "Prioritize NLU global intents" block | 4 touch points today — see Scalable finding above; this is the one change type this layering doesn't yet make cheap. |
| New conversational step | Conductor — `app/conversation/__init__.py`'s `_step_for_action` (:134), the `elif current_step` chain (:706+), `_trigger_step_prompt` (:1274) | 3 touch points, same file — a missed one fails silently (see `e42fe21`'s historical fix). Add the new handler function to the relevant Specialist file. |
| New clipboard slot (lang/location/doctor/date/shift/patient-style) | Decision Maker — `app/booking_slots.py`'s `SLOT_ORDER`, `INVALIDATES` | Pure, no I/O — easy to hand-test. |
| New external system integration | New file in the Messenger layer, one file per system, following `hms_client.py`'s retry/error pattern (`_retry_network_errors`, `HmsApiError`) | Never add a second raw-SQL/raw-httpx call site for an existing system — route through the existing Messenger file (see the `intent_router.py:302-313` finding for what NOT to do). |
| New patient-facing string / new language | Translator — `app/i18n.py`: add key to every language dict, or add the language to `LANGUAGE_LABELS` + `nlu_config.py`'s `VALID_LANGUAGES` | Additive, no logic touched. |
| New clinical emergency keyword/category | Safety Guard — `app/safety.py`'s `EMERGENCY_TRIGGERS` | Cross-cutting; runs before the Listener, unconditionally. |
| New report/analytics field on an NLU interaction | Messengers — `app/db.py`'s `log_nlu_interaction`, called from `conversation/__init__.py:362` | The Messenger layer owns the SQL; the Conductor only supplies values. |
| New QR-trigger flow (like DISCHARGE/RX/RXV) | Specialists — `app/conversation/checkin.py`'s `_DOCUMENT_TRIGGERS`, plus a new `GET /...` redirect route in the Front Door (`app/webhook.py`) | Spans the Front Door (redirect route) and Specialists (trigger handler) by design — the QR code has to resolve server-side (Front Door) before the WhatsApp message even arrives (Specialist). |

## Migration approach

Ordered lowest-risk/highest-payoff first. Each step names what protects it, and the list
ends with an explicit "leave alone" — this document is not a license to touch every layer
just because it's now named.

1. **Add `test_webhook.py` covering `_verify_signature` and `worker.py`'s dedupe check.**
   No refactor, pure test-addition — zero behavior-change risk. This directly closes the
   top finding above (the untested Front Door) and should happen before anything else on
   this list, since it's the one gap with no existing safety net at all.
2. **Route `intent_router.py`'s active-appointment query through `db.py`'s existing
   `has_pending_appointment()` instead of the inline SQL at `intent_router.py:302-313`.**
   Protected by: every test that already exercises the `awaiting_clarification`/reschedule
   path via the `has_active_appt` mock flag (`test_nlu_integration.py`) — since those tests
   mock at the `db` boundary already, swapping the call site underneath them shouldn't
   change any assertion. Smallest possible diff: delete ~10 lines, add one function call.
3. **Registry-ify the Conductor's 3-way step dispatch** (`_step_for_action` / the `elif
   current_step` chain / `_trigger_step_prompt`) into one `STEP_REGISTRY: dict[str, ...]`
   with a loud `KeyError` on an unmapped step, instead of the current silent-fallthrough
   default. Protected by: the full 7-file test suite, which already exercises every
   existing step by name — a registry conversion that keeps every current step's behavior
   identical should leave every test green; any test that goes red pinpoints exactly which
   step's mapping was transcribed wrong. This is explicitly **smaller** than the
   previously-rejected Phase 3-B clipboard cutover (memory:
   [[whatsapp-bot-clipboard-architecture]]) — it does not touch `next_action()` or change
   *how* a step is chosen, only removes the duplication in *where the step-name mapping
   lives*.
4. **(Optional, low-priority) Rename or re-document `city_index.py`/`symptom_client.py`'s
   relationship to the Decision Maker layer** — a one-line module-docstring addition on
   each ("this is a Messenger, not a Decision Maker, despite the name") so a future
   contributor doesn't add impure code to `resolver.py` by analogy. No code change, no test
   risk.

**Leave alone**: the Decision Maker and Specialist layers. The Decision Maker is already
the best-tested, cleanest-boundaried code in the repo — nothing here is the bottleneck,
and "improving" it while migration work is happening elsewhere risks introducing the exact
kind of transitive-monkeypatch bug this project's own `conversation.py`-split plan
identified and guarded against. The Specialists were just reorganized (the 15-phase
package split) with zero test changes as its explicit success criterion; re-touching them
now, for consistency with this document's naming rather than a real defect, would spend
risk budget on a layer that isn't causing any of the problems above.
