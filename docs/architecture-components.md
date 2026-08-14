# System Components — WhatsApp-Webhook

This report describes every component shown in [`architecture-layers.mermaid`](architecture-layers.mermaid): what it is, what it's responsible for, and the rule that keeps it from growing into something else. There are 16 components in total — 7 pipeline layers a message passes through in order, 3 cross-cutting concerns that run alongside every layer, and 6 external systems the codebase depends on but does not own.

---

## Pipeline layers

### 🚪 Front Door
**Files**: `app/webhook.py`, `worker.py`

The system's entry and exit point for WhatsApp traffic. `webhook.py` exposes the `/webhook` HTTP endpoint that Meta calls on every inbound message. Each request's HMAC-SHA256 signature is verified before anything else runs, so only requests genuinely from Meta are accepted. A Redis `SET NX` keyed by message ID guards against Meta's automatic retries reprocessing the same message twice. Valid messages are normalized into a job and pushed onto a Redis queue. `worker.py` pulls jobs off that queue, performs a second, durable dedupe check against SQL Server as a backstop beyond Redis's TTL, and hands the message off to the Conductor. This layer also owns five QR-code redirect routes (check-in, discharge summary, prescription, visit summary, doctor booking) and one internal endpoint that lets 1HMS push live queue updates back to a patient.

**Rule**: no business logic here. If a function starts reasoning about doctors, symptoms, or booking steps, it belongs to a different layer.

### 👂 Listener
**Files**: `app/nlu_client.py`, `app/nlu_validator.py`, `app/nlu_config.py`, `app/model_config.py`

Turns a patient's free-text message into a structured guess at what they meant. `nlu_client.py` sends the message to Sarvam AI along with a system prompt describing every intent, entity, and language the bot understands. `model_config.py` isolates the model name, endpoint, temperature, and timeout, so switching providers is a one-file change. Nothing returned by the LLM is trusted outright — `nlu_validator.py` checks the intent, entities, and language against known-valid lists and forces anything unrecognized to a safe fallback.

**Rule**: this layer never produces booking facts (doctor names, fees, times) — only intent classification and language detection.

### ⚖️ Referee
**Files**: `app/intent_router.py`, `app/flow_policy.py`

Decides, turn by turn, whether the current message should be handled at face value or folded into a longer conversation already in progress. `intent_router.py` keeps a short-lived Redis session for multi-turn slot accumulation — for example, a patient naming a specialty in one message and a preferred date in the next, merged into a single booking request. `flow_policy.py` is a narrow, separate module that names three intents — cancel, navigate back, greeting — as always winning the turn regardless of what any in-progress session currently expects, so a patient can never get permanently stuck mid-flow.

**Rule**: any new multi-turn or follow-up mechanism must check `flow_policy` before trusting its own local state.

### 🧭 Conductor
**Files**: `app/conversation/__init__.py`

The single place that knows what step of a booking a patient is on and what happens next. Every message that clears the Listener and Referee arrives here. This layer loads the saved conversation state, checks for deterministic QR-code commands, runs language auto-detection, invokes the Safety Guard, and dispatches to the correct handler based on the current step. It owns state persistence — saving the step and context back to SQL Server after each turn — and the transition logic that moves a conversation from one step to the next.

**Rule**: step names are defined and consumed only here. No other layer should maintain its own notion of what step comes next.

### 🛠️ Specialists
**Files**: 9 files under `app/conversation/`

Each file is a subject-matter expert for one part of the conversation: `language.py` for language detection and confirmation, `location.py` for capturing and resolving a patient's city or GPS, `doctor_search.py` for name-based doctor and hospital search, `specialty_browsing.py` for symptom-to-specialty matching and category browsing, `doctor_list.py` for formatting and sorting doctor results, `slot_selection.py` for date and time-shift selection, `patient_details.py` for parsing name/age/gender text, `booking_confirmation.py` for the final confirmation message, and `checkin.py` for OPD check-in and every QR-triggered flow.

**Rule**: a handler reads and writes conversation context but never decides what step comes next — that decision stays with the Conductor.

### 🧠 Decision Maker
**Files**: `app/booking_slots.py`, `app/resolver.py`, `app/geo.py`

Answers questions using only facts it has already been given — no network calls, no database, no waiting. `booking_slots.py` is the "clipboard": it tracks which of six booking slots are filled and decides what to ask for next based on what's still blank, independent of any fixed step order. `resolver.py` turns already-fetched search results into a definitive match count — zero, one, or many — so the system knows whether to auto-select, ask a disambiguating question, or report no matches. `geo.py` is an 18-line haversine distance calculator used to sort doctors by proximity.

**Rule**: nothing in this layer ever contains the word `await`. If a function needs data from outside, that fetch belongs to the Messengers layer.

### 📡 Messengers
**Files**: `app/hms_client.py`, `app/whatsapp_client.py`, `app/db.py`, `app/redis_client.py`, `app/symptom_client.py`, `app/city_index.py`

The only code allowed to talk to another system. `hms_client.py` is the sole client for the 1HMS hospital-management API. `whatsapp_client.py` sends every outbound WhatsApp message — text, interactive lists, buttons, location requests — through Meta's Graph API. `db.py` owns every SQL Server query, spanning conversation state, message dedup, appointments, check-in/queue, and analytics logging. `redis_client.py` provides the shared Redis connection. `symptom_client.py` calls a separate proxy that maps a described symptom to a specialty. `city_index.py` builds and caches a coordinates-to-city lookup by paging 1HMS's own doctor directory, working around the public API's requirement for an exact city name.

**Rule**: one file owns one external system's wire format and failure handling. A second call site to the same system elsewhere in the codebase is a boundary violation.

---

## Cross-cutting concerns

These do not sit at any single point in the pipeline — they run alongside every layer.

### 🚨 Safety Guard
**File**: `app/safety.py`

Screens every incoming text message for a clinical emergency before any other processing happens. Matches the message against a set of regular expressions covering cardiac, respiratory, and trauma red flags across English, Hindi, Hinglish, and Bengali phrasing. On a match, the bot responds immediately with emergency guidance and the normal booking flow is skipped for that turn. This check runs before the Listener even sees the message, so a language-detection failure or an LLM outage can never suppress it.

**Rule**: always runs first, unconditionally, on every text message — never gated behind a conversation step.

### 🌐 Translator
**File**: `app/i18n.py`

Owns every word a patient actually reads: a flat dictionary of message templates across four languages — English, Hindi, Hinglish, and Bengali — accessed everywhere through a single lookup function, covering roughly 40 distinct message keys for every prompt, confirmation, and error the bot sends.

**Rule**: no layer constructs a patient-facing string inline. Every user-visible sentence traces back to a key in this file.

### ⚙️ Settings Book
**File**: `app/config.py`

The one place every other layer reads its configuration from: API credentials, connection strings, and feature thresholds, loaded from environment variables and read by files across every layer of the system.

**Rule**: no module reads an environment variable directly outside this file.

---

## External systems

These are depended on but not owned by this codebase — each is reached through exactly one Messenger.

| Entity | What it is | Reached through |
|---|---|---|
| **Meta WhatsApp Cloud API** | The transport in and out. Delivers patient messages via webhook, receives replies via the Graph API — two separate, decoupled calls, not one request/response. | Front Door (in), Messengers (out) |
| **Sarvam AI (LLM)** | The language-understanding engine. Given raw text and a system prompt, returns a classified intent, extracted entities, and a detected language with a confidence level. | Listener |
| **1HMS gateway API** | The hospital's own backend — doctor directories, specialties, availability, booking, discharge/prescription documents. Every read and write of real hospital data flows through here. | Messengers |
| **SQL Server** | Durable storage for conversation state, appointments, message dedup records, check-in/queue status, and NLU interaction logs. | Messengers |
| **Redis** | Fast, short-lived storage — the job queue between the Front Door's two processes, webhook dedup keys, and the Referee's multi-turn session state. | Front Door, Messengers, Referee |
| **NexEagleWebsite symptom-routing API** | A separate proxy that maps a patient's described symptom to a matching medical specialty category. | Messengers |

---

## Conclusion

Sixteen components, one rule each: the Front Door lets messages in and out safely, the Listener guesses what they mean, the Referee decides whose turn it is, the Conductor tracks where the conversation is, the Specialists do the actual work of one topic each, the Decision Maker reasons over facts it already has, and the Messengers are the only ones who ever leave the building. Safety, the Translator, and the Settings Book run underneath all seven, not inside any one of them. Every external system — Meta, Sarvam, 1HMS, SQL Server, Redis, NexEagleWebsite — is reached through exactly one of those seven layers, never more than one. That one-owner-per-concern rule is what makes this report possible to write at all: every component here has exactly one job, stated in one sentence, with nothing left to guess at.
