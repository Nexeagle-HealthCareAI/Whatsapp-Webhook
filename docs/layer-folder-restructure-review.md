# Review: "one folder per layer" restructuring idea

Analysis only — no files were changed to produce this. Answers one question: if every
one of the 7 layers gets its own `app/<layer_name>/` folder, does that hold together, and
what would actually have to move?

## Verdict

The idea is sound and already **partly done** — three layers already live in their own
folder today, just not under a uniform naming scheme. A full pass to make all 7 uniform is
doable, but it isn't one mechanical move: it hits 3 real friction points (below) that need
a decision before any file moves, plus a couple of pieces that were never really "layer
code" in the first place.

## Where each layer actually stands right now

| Layer | Current location | Already its own folder? |
|---|---|---|
| 🚪 Front Door | `app/webhook.py` + `app/routes/{whatsapp_ingest,qr_redirects,qr_handlers,hms_events}.py`, plus `worker.py` at **repo root**, outside `app/` entirely | Partial — `app/routes/` exists but isn't named for the layer, and `worker.py` isn't under `app/` at all |
| 👂 Listener | `app/listener/{nlu_client,nlu_config,nlu_validator,model_config}.py`, with thin re-export shims left at `app/nlu_client.py` etc. for backward compatibility | **Yes**, already done |
| ⚖️ Referee | `app/intent_router.py`, `app/flow_policy.py` — both at `app/` root | No |
| 🧭 Conductor | `app/conversation/__init__.py` | Shares a folder with Layer 5 (see below) |
| 🛠️ Specialists | `app/conversation/{language,location,doctor_search,specialty_browsing,doctor_list,slot_selection,patient_details,booking_confirmation,checkin}.py` | Shares a folder with Layer 4 — Conductor and Specialists are currently one package, not two |
| 🧠 Decision Maker | `app/booking_slots.py`, `app/resolver.py`, `app/geo.py`, `app/city_resolver.py`, `app/normalizer.py` — all at `app/` root | No |
| 📡 Messengers | `app/hms_client.py`, `app/whatsapp_client.py`, `app/redis_client.py`, `app/symptom_client.py`, `app/city_index.py` at root, plus `app/db/` which is **already its own package** (`__init__.py`, `analytics.py`, `appointments.py`, `checkin_queue.py`, `conversation_state.py`, `dedup.py`) | Partial — one file group (`db`) already split out, the rest isn't |

So 2.5 of 7 layers are already folder-shaped. The rest are flat files at `app/` root.

## The 3 friction points, before any code moves

**1. Cross-cutting concerns don't have a layer to live under.** `app/safety.py`,
`app/i18n.py`, `app/config.py` are used by *every* layer, not owned by one. A strict "every
file lives under `app/<layer_name>/`" rule has no honest home for these three — putting
any of them inside one layer's folder (e.g. `app/conductor/i18n.py`) would misrepresent
that it's Conductor-owned when 9+ files across every layer import it. The two real options:
leave them at `app/` root (as now — arguably the correct signal: "not owned by a layer"),
or give them their own `app/shared/` (or `app/common/`) folder as an explicit 8th bucket.
Either is fine; picking neither and forcing them into a layer folder would be the actual
mistake.

**2. Conductor and Specialists are one folder today, not two.** `app/conversation/`
holds both the Conductor's orchestration (`__init__.py` — `handle_message`,
`STEP_REGISTRY`, `_step_for_action`) and all 9 Specialist handler files as flat siblings.
Splitting these into genuinely separate `app/conductor/` and `app/specialists/` folders is
a second version of the exact same move already done once this project's life (the
original `conversation.py` → package split) — and it carries the same specific risk that
split had to solve for: several Specialist functions call back into names that live in the
Conductor (`_advance_booking_flow`, `_render_doctor_list`, `db`, `whatsapp_client`, etc.)
via a **lazy, function-body-local import** (`from app import conversation` inside the
function, not at module top) specifically so test-suite monkeypatching
(`conversation.db = MockDB()`) still reaches them. Moving Specialists to a *different
top-level package* than Conductor means every one of those lazy imports needs to change
from `from app import conversation` to whatever the new Conductor package is called — the
pattern still works, but it's a real, careful migration, not a folder drag-and-drop.

**3. `worker.py` (and `scheduler.py`) aren't just Python modules — they're deploy
entrypoints.** Confirmed in `docker-compose.yml`: `worker.py` is launched as
`command: ["python", "worker.py"]` and `scheduler.py` as `command: ["python",
"scheduler.py"]`, and `Dockerfile` does `COPY worker.py .` / `COPY scheduler.py .` at the
repo root, separately from the `app/` package. Moving `worker.py` under
`app/front_door/worker.py` means updating `docker-compose.yml`'s command and the
`Dockerfile`'s copy step too — a deploy-config change, not just an import-path change. Not
a reason to avoid it, just not something a pure code-organization pass should do silently.

## Renaming risk — the one thing worth flagging explicitly

`app/conversation/` is referenced by name in dozens of places — every test file's
monkeypatch convention (`conversation.db = ...`, `conversation._clinic_now = ...`),
`worker.py`'s own call into it, and the module's own extensive internal cross-references.
**Renaming that folder to `app/conductor/` for label-consistency with the architecture
diagram is high-blast-radius for a purely cosmetic win.** The two already-proven, low-risk
patterns in this codebase's own history are worth reusing instead of a hard rename:

- **Re-export shim** (used for the Listener split): keep `app/conversation/` where it is,
  under its current name, and treat "which folder = which layer" as a *documented mapping*
  (already exists: `docs/architecture-components.md`) rather than a naming requirement.
- If a rename is still wanted later, do it the way `app/db.py` → `app/db/` was done: package
  conversion first with zero code moved, full suite green, *then* moves — never a
  same-commit rename-and-restructure.

## What a full target tree would look like, if pursued

```
app/
  front_door/        # webhook.py + today's app/routes/*.py content
                      # (worker.py optionally stays at repo root -- see friction point 3)
  listener/           # already exists, unchanged
  referee/             # intent_router.py, flow_policy.py
  conductor/           # app/conversation/__init__.py's orchestration content only
  specialists/         # app/conversation/'s 9 sibling handler files
  decision_maker/      # booking_slots.py, resolver.py, geo.py, city_resolver.py, normalizer.py
  messengers/           # hms_client.py, whatsapp_client.py, redis_client.py,
                        # symptom_client.py, city_index.py, and today's app/db/ package
  shared/ (or root)     # safety.py, i18n.py, config.py, types.py -- cross-cutting, not
                        # layer-owned
  main.py                # FastAPI composition root -- arguably front_door, arguably its
                          # own thing; not urgent to decide
```

## Suggested order, if this gets picked up

Same discipline every other move in this codebase has used — smallest and best-isolated
first, full test suite green after each step, re-export shim at the old import path so
nothing downstream has to change in the same commit:

1. `app/routes/` → rename/fold into `app/front_door/` (small, already isolated, `webhook.py`
   already just a composition root over it).
2. `app/intent_router.py` + `app/flow_policy.py` → `app/referee/` (2 files, already
   self-contained, `test_nlu_integration.py` already patches the whole module reference so
   a re-export shim keeps it green).
3. `app/booking_slots.py`, `resolver.py`, `geo.py`, `city_resolver.py`, `normalizer.py` →
   `app/decision_maker/` (all pure, zero-I/O, lowest risk of the whole list — matches why
   this layer scored cleanest in every SOLID audit so far).
4. `app/hms_client.py`, `whatsapp_client.py`, `redis_client.py`, `symptom_client.py`,
   `city_index.py` → `app/messengers/`, alongside the already-split `app/db/`.
5. The Conductor/Specialists split (friction point 2) — last and most involved, needs the
   same lazy-import discipline as the original `conversation.py` split, and deserves its
   own dedicated plan document the way that split got one, not a fold-in to a bigger pass.

`worker.py`/`scheduler.py`'s repo-root placement and the cross-cutting-concerns folder
question (friction point 1) are both open decisions, not blockers — worth settling before
step 1, since they affect the target tree shown above either way.
