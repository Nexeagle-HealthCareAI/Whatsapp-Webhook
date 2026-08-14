# db.py Split — Work Prompt

A ready-to-paste prompt for executing SOLID Rebuild Phase 7 (the `db.py` bounded-context
split) — either in a fresh session with repo access, or as a work order for an engineer.
Everything below the `---` is the prompt itself.

---

## ROLE

You are implementing one specific, already-scoped refactor in the WhatsApp-Webhook
codebase: splitting `app/db.py` into a package, one file per bounded context. This is not
an analysis task — the finding is already made, the target structure is already decided.
Your job is to execute it safely, in small verified steps, and stop exactly at the
boundary described below.

## CONTEXT — why this refactor exists

`app/db.py` is 408 lines and 21 functions owning every SQL Server query in the
codebase, spanning **5 unrelated bounded contexts** with no shared logic between them —
only the file:

| Bounded context | Functions | Lines |
|---|---|---|
| Conversation state | `get_conversation_state`, `save_conversation_state`, `clear_conversation_state` | 20-61 |
| Message dedup | `is_message_processed`, `mark_message_processed` | 62-83 |
| Appointments | `has_pending_appointment`, `get_upcoming_active_appointment`, `create_pending_appointment`, `mark_appointment_booked`, `mark_appointment_failed`, `get_appointment_by_hms_id`, `get_booked_appointments_for_phone`, `mark_appointment_cancelled_locally`, `mark_appointment_rescheduled_locally` | 84-229 |
| Check-in / queue | `upsert_checkin_notification`, `save_queue_status`, `list_due_followups`, `mark_followup_sent` | 230-317 |
| NLU analytics logging | `log_nlu_interaction`, `update_last_nlu_log_correctness`, `mark_session_nlu_correctness_on_booking` | 318-408 |

Plus `get_pool()` (line 13) — a shared connection-pool factory every function above calls.

**Appointments is the largest and still growing** — it was 5 functions when this finding
was first raised; a concurrent "wire real appointment cancel/reschedule" change added 4
more in the same session. This imbalance gets more expensive to untangle the longer it's
left, which is why this is worth doing even though nothing is currently broken.

**Re-verify these facts against the current repo before starting** — this document is a
point-in-time snapshot; line numbers and the function count may have moved since it was
written.

## WHY THIS IS LOWER-RISK THAN IT LOOKS

Confirmed by grepping every test file in the repo (`test_*.py`): **no test imports any
`db.py` function by name.** Every test that needs to control database behavior does it by
replacing the *entire* `db` module reference on whichever module calls it —
`conversation.db = MockDB()`, `intent_router.db = mock_db`, `worker.db = mock_db`. None of
them do `from app.db import get_conversation_state` or patch an individual function.

This means, unlike the earlier `app/conversation.py` → package split (which needed a
careful "9 mutated names, lazy-import" discipline because specific functions were
individually monkeypatched), **`get_pool` and every other function here can be imported
normally, at the top of each new sibling file, with no lazy-import trick required.** As
long as the new `app/db/__init__.py` re-exports every public function at the package's top
level, every existing `db.<function_name>()` call site keeps working unchanged, and every
existing whole-module mock keeps working unchanged.

## TARGET STRUCTURE

```
app/db/
  __init__.py          # get_pool() (shared infra) + re-exports every function below,
                        # so `from app import db; db.get_conversation_state(...)` etc.
                        # keeps working from every existing call site unchanged.
  conversation_state.py  # get_conversation_state, save_conversation_state, clear_conversation_state
  dedup.py                # is_message_processed, mark_message_processed
  appointments.py         # has_pending_appointment, get_upcoming_active_appointment,
                           # create_pending_appointment, mark_appointment_booked,
                           # mark_appointment_failed, get_appointment_by_hms_id,
                           # get_booked_appointments_for_phone,
                           # mark_appointment_cancelled_locally,
                           # mark_appointment_rescheduled_locally
  checkin_queue.py         # upsert_checkin_notification, save_queue_status,
                           # list_due_followups, mark_followup_sent
  analytics.py             # log_nlu_interaction, update_last_nlu_log_correctness,
                           # mark_session_nlu_correctness_on_booking
```

Every function keeps its exact name, signature, and behavior. This is a pure move —
nothing about *what* any function does should change, only *which file* it lives in.

## STEPS

Same discipline as every other refactor in this codebase's history: one bounded context
per commit, full test suite green after every single step, never a big-bang rewrite.

1. **Phase 0 — package conversion, no code moved.** Convert `app/db.py` →
   `app/db/__init__.py` with identical contents. Run the full test suite. Confirms the
   package conversion alone doesn't break anything before any function actually moves.
2. **Move message dedup** (`dedup.py`) — smallest, most isolated context, only 2
   functions, only `worker.py` calls them. Best first move to prove the pattern works.
3. **Move conversation state** (`conversation_state.py`) — 3 functions, called from the
   Conductor (`app/conversation/__init__.py`).
4. **Move NLU analytics logging** (`analytics.py`) — 3 functions, called only from the
   Conductor's NLU-logging block.
5. **Move check-in / queue** (`checkin_queue.py`) — 4 functions, called from
   `app/conversation/checkin.py` and `scheduler.py`.
6. **Move appointments** (`appointments.py`) — largest and last, 9 functions, called from
   multiple places (Referee, Specialists' booking/cancel/reschedule handlers). Doing this
   last means the pattern is well-proven by the time you touch the highest-traffic context.

After each step: update `app/db/__init__.py`'s re-exports for whatever just moved, run the
full test suite, confirm every file green, commit before starting the next step.

## VERIFICATION (after every single phase, not just at the end)

```bash
python3 tests/test_webhook.py
python3 tests/test_specialty_groups.py
python3 tests/test_nlu_integration.py
python3 tests/test_checkin.py
python3 tests/test_resolver.py
python3 tests/test_doctor_booking_qr.py
python3 tests/test_document_qr.py
python3 tests/test_hospital_search.py
python3 tests/test_appointment_cancel_reschedule.py
python3 -m app.decision_maker.booking_slots
python3 -m app.referee.flow_policy
```

(Paths current as of the tests/ consolidation and the app/decision_maker, app/referee
layer-folder moves -- this document predates both; db.py's own split described below is
already complete, kept here as a historical record of the approach used.)

All must pass after every phase. (`test_hospital_search.py` has one pre-existing,
unrelated flaky assertion that only fails when a real `SARVAM_API_KEY` is set in `.env` —
known, documented, not something this refactor should touch or "fix" as a side effect.)

Additionally: after moving `get_upcoming_active_appointment` specifically (part of the
appointments move), re-run `test_nlu_integration.py`'s
`test_booking_vs_reschedule_ambiguity` and read its output directly — don't just trust
"exit code 0" — to confirm the mock/patch chain that exercises this function is still
actually being exercised, not silently skipped.

## OUT OF SCOPE — do not do these as part of this task

- Do not change any function's signature, return type, or SQL query logic. This is a
  file-organization move only.
- Do not touch any other layer (Referee, Conductor, Specialists, etc.) beyond updating
  their `import` lines if the import path needs to change (it shouldn't, if
  `app/db/__init__.py`'s re-exports are complete — call sites use `db.<function>()`
  through the module reference, not a direct import of the function).
- Do not add tests for functions that don't already have coverage, unless a specific move
  step turns out to be unverifiable without one — if that happens, add the minimal test
  needed and note it, don't treat this task as a general test-coverage expansion.
- Do not "improve" `get_pool()`'s connection-pooling behavior, add retries, or change
  error handling anywhere. Behavior-preservation only.
- Do not start this until Phase 2's remaining item (relocating `symptom_client.py`'s
  `match_category` to the Decision Maker layer) or any other unrelated pending item is
  confused with this one — this prompt covers `db.py` only.

## DELIVERABLE

6 commits (Phase 0 + 5 context moves), each with the full test suite green, ending with
`app/db.py` no longer existing as a single file and `app/db/` as a package of 6 files
(`__init__.py` + 5 bounded-context siblings), zero test file changes, zero behavior
changes anywhere in the system.
