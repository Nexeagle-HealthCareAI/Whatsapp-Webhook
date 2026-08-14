# SOLID Rebuild Plan — audited by layer

Every layer from [`architecture-components.md`](architecture-components.md) checked
against all five SOLID principles, component by component, with real file/line evidence.
Legend: ✅ compliant · ⚠️ partial (real but not urgent) · ❌ violation · – not applicable
(no inheritance in this component, so Liskov doesn't apply — forcing a finding here would
be noise, not signal).

## Layer-by-layer audit

### 🚪 Front Door — `webhook.py`, `worker.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ⚠️ | ⚠️ | – | ✅ | ✅ |

- **S — partial**: one file carries three distinct jobs — inbound message ingestion
  (`receive_webhook`, `_verify_signature`), five QR-code redirect routes, and the
  `/events/token-called` push endpoint from 1HMS. At 319 lines this is still readable, but
  it's three responsibilities held together only by "they're all HTTP routes."
- **O — partial**: `_document_qr_redirect` (webhook.py:63) is a shared helper for 3 of the
  5 QR routes (discharge, prescription, visit-summary). `checkin_qr_redirect` (:36) and
  `doctor_booking_qr_redirect` (:110) don't use it — each re-implements the same
  check-settings / try-except-`HmsApiError` / build-redirect pattern by hand. A 6th QR type
  has two different templates to copy from, and no guarantee the copy is faithful.
- **L — N/A**: `TokenCalledEvent` (webhook.py:267) extends Pydantic's `BaseModel` only.
- **I — clean**: every route function takes exactly the path/query params it needs.
- **D — clean**: depends on `hms_client`/`db`/`whatsapp_client`'s functions, never raw
  `httpx`/`aioodbc` directly.

### 👂 Listener — `nlu_client.py`, `nlu_validator.py`, `nlu_config.py`, `model_config.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ⚠️ | ✅ | – | ✅ | ✅ |

- **S — partial**: `nlu_client.py` bundles three different jobs — classification
  (`classify_message`), LLM *phrasing/generation* of patient-facing text
  (`generate_conversational_response`, `generate_step_prompt`), and two **pure,
  zero-`await`** utility functions (`normalize_datetime_to_date`:39,
  `normalize_time_of_day`:97) that don't belong in an I/O-calling file at all — see the
  cross-layer finding below.
- **O — clean**: `VALID_INTENTS`/`VALID_ENTITIES`/`VALID_LANGUAGES` are additive lookup
  lists; `SYSTEM_PROMPT` extends by adding an example, not editing branching logic.
- **L — N/A**: no classes with inheritance in this layer.
- **I — clean**: `validate_nlu_response(parsed, raw_text, model_name)` takes exactly what
  it needs, nothing more.
- **D — clean**: `nlu_validator.py` depends only on `nlu_config.py`'s constants — a clean,
  one-directional dependency.
- **Worth noting, not a violation**: `generate_step_prompt` (nlu_client.py:223) is called
  from `conversation/__init__.py:791` with the *template already computed first*
  (`fallback = t(fallback_key, lang, ...)`) — the LLM only ever supplies wording, and any
  failure falls straight back to the Translator's own text. `generate_conversational_response`
  (nlu_client.py:166, called at `conversation/__init__.py:688`) is looser — for genuinely
  off-topic small talk only, with a documented fallback to `t("error_nlu_fallback", lang)`
  on failure or empty response. Both are deliberate, guarded exceptions to "the Translator
  owns every patient-facing string" — real design decisions, not oversights. Flagged here
  so a future reader doesn't mistake them for the same kind of drift as the findings below.

### ⚖️ Referee — `intent_router.py`, `flow_policy.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ⚠️ | ❌ | – | ✅ | ❌ |

- **S — partial**: `intent_router.py` runs a raw SQL query (:302-313) — that's the
  Messenger layer's job, not the Referee's.
- **O — violation (High)**: adding one new global intent means editing 4 independently
  maintained collections across 3 files — `nlu_config.py:10` (`VALID_INTENTS`),
  `intent_router.py:11-20` (`REQUIRED_ENTITIES`), `intent_router.py:87`
  (`_NO_SLOT_SAFETY_NET`), `flow_policy.py:43` (`GLOBAL_INTENTS`). None enforces the others
  stay in sync — this exact desync already caused the "hi"/"cancel" stuck-loop bug that
  `flow_policy.py` was built to fix, then the same disconnected-collections shape was
  reproduced immediately by the fix itself.
- **L — N/A**: `RoutedResult` (intent_router.py:60), no inheritance.
- **I — clean**: `route_intent(phone, raw_nlu_result, input_value, lang, current_step)`
  matches exactly what its one caller supplies.
- **D — violation (High)**: `intent_router.py:302-313` calls `db.get_pool()` and runs SQL
  directly, bypassing `db.py`'s own `has_pending_appointment()` (`db.py:84-92`), which
  already does the same query. The Referee is depending on a concrete database driver
  instead of the Messenger's abstraction.

### 🧭 Conductor — `app/conversation/__init__.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ⚠️ | ❌ | – | ⚠️ | ✅ |

- **S — partial**: 1,353 lines carrying orchestration, QR-trigger interception, language
  auto-swap, the Safety Guard invocation, the NLU call, global-intent handling, and a
  casual-chat fallback — six distinct jobs held together only by "this all has to happen
  before step dispatch."
- **O — violation (High)**: three separate step-name dispatch surfaces —
  `_step_for_action` (:134), the `elif current_step` chain (:706-737), `_trigger_step_prompt`
  (:1274+) — must all independently stay in sync for every step, with **no error** on a
  missed case, just silent fallthrough to a default. This already shipped a live bug (the
  missing `("retry","location")` case, fixed in `e42fe21`, with a regression test added
  after the fact) — direct historical proof this isn't a theoretical risk.
- **L — N/A**.
- **I — partial (Low)**: handlers receive the entire `context` dict rather than the 1-3
  keys they read — a pervasive, deliberate convention across ~40 functions, not an
  isolated slip.
- **D — clean**: every dependency reaches the Decision Maker, Messenger, or Specialist
  layers correctly — no raw `httpx`/`aioodbc` call found inside this file.

### 🛠️ Specialists — 9 files under `app/conversation/`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ✅ | ✅ | – | ⚠️ | ✅ |

- **S — clean**: one file per conversational concern, verified by the 15-phase package
  split completed this project's life — each file's function list maps 1:1 to one topic.
- **O — clean** *within this layer*: a new handler is a new function in the right file.
  (Registering that handler with the Conductor still costs 3 touch points — that's the
  Conductor's finding above, not this layer's.)
- **L — N/A**.
- **I — partial (Low)**: same context-dict grab-bag as the Conductor — inherited from how
  handlers are called, not a defect specific to this layer.
- **D — clean, verified**: `grep` across all 9 files confirms zero direct `httpx`/`aioodbc`
  calls — every I/O request goes through the Messenger layer's functions.

### 🧠 Decision Maker — `booking_slots.py`, `resolver.py`, `geo.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ✅ | ✅ | – | ✅ | ✅ |

- **S — clean**: `booking_slots.py` = slot state, `resolver.py` = matching/cardinality,
  `geo.py` = distance math. Three distinct, non-overlapping jobs, no shared state.
- **O — clean**: `SLOT_ORDER`/`INVALIDATES` (booking_slots.py) are additive dicts;
  `match_doctor_by_query`/`match_hospital_by_query` (resolver.py) extend independently of
  each other.
- **L — N/A**: `Resolution` (resolver.py:34), no inheritance.
- **I — clean**: every function signature is narrow and specific to its one job.
- **D — clean, verified**: zero `await` anywhere in any of the three files — genuinely
  zero I/O, confirmed by grep, not assumed.

**This is the reference layer.** Every other layer's "done" state should look like this
one: `test_resolver.py` imports its functions directly with no mocks at all, and
`booking_slots.py` has its own dependency-free `__main__` self-test.

### 📡 Messengers — `hms_client.py`, `whatsapp_client.py`, `db.py`, `redis_client.py`, `symptom_client.py`, `city_index.py`

| | S | O | L | I | D |
|---|---|---|---|---|---|
| Verdict | ⚠️ | ✅ | – | ✅ | ⚠️ |

- **S — violation (Medium), two separate instances**:
  - `db.py` spans 5 unrelated bounded contexts (conversation-state, message-dedup,
    appointments, check-in/queue, NLU-analytics-logging) with no shared helpers between
    them, only the file.
  - `city_index.py` is worse: of its 7 functions, **4 are pure decision logic with zero
    I/O** — `build_from_doctors` (:78), `nearest_city` (:136), `cities_within` (:147),
    `match_typed_city` (:171) — mixed into the same file as its 3 genuinely I/O functions
    (`_fetch_all_doctors`, `get_index`, `get_all_doctors`). This file isn't just
    "Messenger with a confusing name" (the earlier finding) — it's actually two layers'
    worth of code living in one file. See the cross-layer finding below.
- **O — clean**: a new 1HMS/WhatsApp capability is a new function; nothing existing needs
  editing.
- **L — N/A**: `HmsApiError` (hms_client.py:20) extends `Exception` only.
- **I — clean**: signatures are narrow; the two widest (`db.create_pending_appointment`,
  `db.upsert_checkin_notification`) match their single caller's actual needs 1:1, not
  padded with unused params.
- **D — partial**: every file here is correctly the sole owner of its external system — the
  one violation is `intent_router.py` reaching *into* this layer's territory with its own
  SQL, which is Layer 3's finding, listed here only for completeness.

## Cross-layer finding: pure logic keeps ending up inside I/O files

This isn't one bug, it's a pattern, found in **three separate files across two layers**:

| File | Layer it's classified as | Pure functions found inside it |
|---|---|---|
| `city_index.py` | Messenger | `build_from_doctors`, `nearest_city`, `cities_within`, `match_typed_city` (4 of 7 functions) |
| `nlu_client.py` | Listener | `normalize_datetime_to_date`, `normalize_time_of_day` (2 of 8 functions) |
| `symptom_client.py` | Messenger | `match_category` (1 of 2 functions) |

Every one of these functions could sit beside `resolver.py`/`geo.py` in the Decision Maker
layer today with zero code change beyond the move — none of them touch `await`, a
database, or an HTTP client. Recognizing this as one systemic pattern (not three unrelated
one-offs) is what makes it worth a dedicated rebuild phase rather than three separate
ad-hoc fixes.

## Scorecard

| Layer | S | O | L | I | D | Priority |
|---|---|---|---|---|---|---|
| ⚖️ Referee | ⚠️ | ❌ | – | ✅ | ❌ | **Highest** |
| 🧭 Conductor | ⚠️ | ❌ | – | ⚠️ | ✅ | **Highest** |
| 📡 Messengers | ⚠️ | ✅ | – | ✅ | ⚠️ | Medium |
| 👂 Listener | ⚠️ | ✅ | – | ✅ | ✅ | Medium |
| 🚪 Front Door | ⚠️ | ⚠️ | – | ✅ | ✅ | Low |
| 🛠️ Specialists | ✅ | ✅ | – | ⚠️ | ✅ | Low |
| 🧠 Decision Maker | ✅ | ✅ | – | ✅ | ✅ | None — reference layer |
| 🚨 Safety Guard | ✅ | ✅ | – | ✅ | ✅ | None |
| 🌐 Translator | ✅ | ✅ | – | ✅ | ✅ | None |
| ⚙️ Settings Book | ✅ | ✅ | – | ✅ | ✅ | None |

---

## Status (re-verified against current code)

| Phase | Status |
|---|---|
| 1 — Front Door test coverage | ✅ Done (test_webhook.py now covers the full webhook HTTP surface + QR redirects via TestClient) |
| 2 — Relocate misplaced pure functions | ⚠️ Partial — city_index.py's 4 functions moved to city_resolver.py ✅; nlu_client.py's 2 normalize functions moved to app/normalizer.py ✅; symptom_client.py's match_category still pending |
| 3 — Referee → Messenger DIP fix | ✅ Done (intent_router.py now calls db.get_upcoming_active_appointment()) |
| 4 — Front Door QR-redirect consolidation | ✅ Done, exceeded plan — app/routes/qr_handlers.py introduces a BaseRedirectHandler ABC with 3 concrete subclasses, the codebase's first real inheritance hierarchy |
| 5 — Global-intent consolidation | ✅ Done (INTENT_REGISTRY in app/listener/nlu_config.py is now the single source; REQUIRED_ENTITIES/_NO_SLOT_SAFETY_NET/GLOBAL_INTENTS all derive from it) |
| 6 — Conductor step-dispatch registry | ⚠️ Substantially done — STEP_REGISTRY now unifies handle_message's dispatch and _trigger_step_prompt (the original duplication). _step_for_action (a related but separate action→step-name mapping) still has the same silent-fallthrough shape as before — see follow-up task |
| 7 — db.py bounded-context split | ⏳ Not started (lowest priority, unchanged) |

## The rebuild — 7 phases, ordered by risk

Same discipline as every refactor this project has done: smallest and best-protected
first, full test suite green after every phase, one commit per phase, nothing left
half-done.

**Phase 1 — Front Door safety net.** Add `test_webhook.py`: a constructed payload with a
valid/invalid HMAC signature against `_verify_signature`, and a duplicate `message_id`
against `worker.py`'s `handle_job`. Zero refactor risk — pure test addition. Unblocks
Phase 4 below.

**Phase 2 — Move the misplaced pure functions.** Relocate `city_index.py`'s
`build_from_doctors`/`nearest_city`/`cities_within`/`match_typed_city`,
`nlu_client.py`'s `normalize_datetime_to_date`/`normalize_time_of_day`, and
`symptom_client.py`'s `match_category` into the Decision Maker layer (either a new
`app/city_geo.py` sibling to `resolver.py`, or appended to `resolver.py` itself). Fixes
the cross-layer finding above. Protected by: `test_resolver.py`, `test_specialty_groups.py`
(which already imports `city_index`). Mechanical move — verify no test monkeypatches these
by name before moving, same discipline the `conversation.py` package split used.

**Phase 3 — Referee → Messenger DIP fix.** Replace `intent_router.py:302-313`'s raw SQL
with a call to `db.py`'s existing `has_pending_appointment()`. Protected by:
`test_nlu_integration.py`'s `has_active_appt`-mocked tests, which patch at the `db`
boundary already. Smallest diff on this list: delete ~10 lines, add one call.

**Phase 4 — Front Door OCP/DRY fix.** Generalize `checkin_qr_redirect` and
`doctor_booking_qr_redirect` to share `_document_qr_redirect`'s pattern (or extract one
common helper all 5 QR routes call). Do this *after* Phase 1, so the change has test
coverage underneath it for the first time.

**Phase 5 — Referee/Conductor global-intent consolidation.** Make `VALID_INTENTS`,
`REQUIRED_ENTITIES`, `_NO_SLOT_SAFETY_NET`, and `GLOBAL_INTENTS` derive from one canonical
registry instead of being 4 independently maintained collections. Protected by:
`test_nlu_integration.py`'s global-intent tests plus `flow_policy.py`'s own `__main__`
self-test.

**Phase 6 — Conductor step-dispatch registry (the big one).** Replace `_step_for_action`
/ the `elif current_step` chain / `_trigger_step_prompt` with one `STEP_REGISTRY` that
raises loudly on an unmapped step instead of silently falling through. Protected by: the
full 7-file test suite, which already exercises every existing step by name — a
behavior-preserving registry conversion should leave every test green, and any red test
pinpoints exactly which step was transcribed wrong. Highest payoff on this list: removes
the exact bug class that has already shipped once.

**Phase 7 — Messenger SRP split (optional, lowest priority).** Split `db.py` by bounded
context (conversation-state, dedup, appointments, checkin/queue, analytics) into a
`db/` package, mirroring the `conversation.py` split's re-export discipline. Protected by:
confirmed — no test file imports `db.py` functions by name anywhere, so this is actually
lower-risk than it sounds. Included for completeness of the SOLID rebuild; not urgent on
its own, since nothing here is currently causing bugs.

**Not included**: the Decision Maker, Specialists, Safety Guard, Translator, and Settings
Book layers score clean across all five principles today — no rebuild work is proposed for
them. Touching a layer that already scores clean, in the middle of a SOLID rebuild, for
consistency rather than a real finding, would be the exact mistake this plan is trying to
avoid elsewhere.
