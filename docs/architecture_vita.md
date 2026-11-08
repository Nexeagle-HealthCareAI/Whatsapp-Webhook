# WhatsApp bot + Vita: system architecture

Vita (chat agent in the NLP Router) finds the doctor; this bot books. The hand-over happens when the patient picks a doctor.

## 1. Context: who talks to whom

```mermaid
flowchart LR
  P([Patient on WhatsApp]) <--> M[Meta Cloud API]
  M -- webhook --> G[Bot gateway<br/>FastAPI]
  G --> Q[(Redis<br/>booking:jobs)]
  Q --> W[Bot worker<br/>conversation.handle_message]
  W <--> S[(SQL Server<br/>conversation_state)]
  W -- text and taps --> V[NLP Router<br/>Vita /v2/agent/chat]
  V --> L[Groq LLM]
  V --> H[1HMS public API]
  W --> H
  W --> N[Groq / Sarvam NLU]
  W --> O[(Redis<br/>whatsapp:outbox)]
  O --> X[Bot sender]
  X --> M
  SCH[Scheduler] --> O
```

## 2. Bot containers (docker compose)

| Service | Job |
|---|---|
| fastapi | Receives Meta's webhook, verifies the signature, dedupes, queues the job. |
| worker | Runs the conversation. The only service that talks to Vita (`VITA_*` env). |
| sender | Drains the outbox at a Meta-safe rate. Order of messages is the order queued. |
| scheduler | Follow-up messages the day after a visit. |
| redis | Job queue, outbox, dedupe, locks. |

## 3. Conversation steps and where Vita sits

```mermaid
stateDiagram-v2
  [*] --> choosing_language
  choosing_language --> choosing_location
  choosing_location --> choosing_search_mode
  choosing_search_mode --> vita_chat: number on VITA_ALLOWED_PHONES
  choosing_search_mode --> buttons: otherwise, or vita_off
  state "symptom / name / browse (old steps)" as buttons
  buttons --> choosing_doctor
  vita_chat --> vita_chat: text, tap, location
  vita_chat --> buttons: Vita down, or cancel / reschedule words
  vita_chat --> HANDOVER: doctor_selected
  choosing_doctor --> HANDOVER
  state "day / shift, patient form, confirm" as HANDOVER
  HANDOVER --> booked
  booked --> [*]: state cleared
```

## 4. One Vita turn

```mermaid
sequenceDiagram
  participant P as Patient
  participant B as Bot worker
  participant V as Vita (router)
  participant H as 1HMS
  P->>B: "skin ka doctor chahiye"
  B->>V: POST /v2/agent/chat {text, session_id, city}
  V-->>B: kind=doctor_list, whatsapp[...]
  B->>P: each reply.whatsapp message (via outbox)
  P->>B: taps row doc:<id>
  B->>V: {reply_id: "doc:<id>", session_id}
  V-->>B: kind=doctor_selected, doctor{...}
  Note over B: Vita's own message is not sent
  B->>H: GET /public/doctors/<id>
  H-->>B: doctor (same shape as the bot's own list)
  B->>P: day / shift, patient form (bot's own steps)
```

## 5. Responsibilities

| | Vita | Bot |
|---|---|---|
| Understand symptoms, names, specialty, area | yes | old steps only |
| Emergency text (108/112) and location request | yes | own safety check outside the Vita step |
| Doctor list, show more, refine | yes | old list |
| Day, shift, patient details, confirm, book | no | yes |
| Cancel, reschedule, check-in, QR, follow-up | no | yes |

## 6. Failure behaviour

| Failure | What the patient sees |
|---|---|
| Vita unreachable, 401/403/429, 5xx, bad reply | One notice, then the three search buttons for the rest of the conversation. |
| 422 (expired session) on a typed message | New Vita session, no notice. |
| 422 on a tap on an old row | The introduction again. |
| Groq down inside Vita | Vita answers from its code steps (`engine: fallback`); the bot does not notice. |
| 1HMS cannot confirm the picked doctor | Error notice; stays with Vita; nothing is booked. |

## 7. Configuration and rollout

| Setting | Notes |
|---|---|
| `VITA_API_URL`, `VITA_API_KEY` | Router base URL and a `chat` scope client key. |
| `VITA_ALLOWED_PHONES` | Comma-separated numbers (last 10 digits matched), or `*`. Empty = off. |

Rollout: dev with own numbers, then a small allowlist on prod, then `*`. Prod workflow does not pass `VITA_*` yet, so prod stays off until added on purpose.
Limits: router `chat` scope 30 calls/min per key; Vita session 30 min; Meta list 10 rows, button 3.
