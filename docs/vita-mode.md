# Vita mode

With Vita mode on, patient conversations on WhatsApp are handled by **Vita**, the patient agent in the
1HMS NLP Router (`POST /v2/agent/chat`, `channel: "whatsapp"`), instead of this bot's own NLU, safety
triage and step machine. The contract is `1HMS-NLP-Router/docs/vita_whatsapp.md`. Code: `app/vita_bridge.py`,
hooked in at the top of `conversation.handle_message` (`_maybe_hand_to_vita`).

## Settings (all optional)

| Env var | Default | Meaning |
|---|---|---|
| `VITA_CHAT_URL` | empty = **off** | Full URL, e.g. `http://<router-host>:5003/v2/agent/chat`. Empty: the bot behaves exactly as before. The router's port must be reachable from the worker container. |
| `VITA_API_KEY` | empty | Sent as `X-Api-Key`. Needs the `chat` scope; add `verified_mobile` so Vita books without its own WhatsApp code. |
| `VITA_FALLBACK` | `message` | When Vita fails (5xx, timeout, unknown reply): `message` sends one apology ("try again in a minute ... In an emergency call 108"); `legacy` lets the old bot answer that message (transition only). |
| `VITA_TIMEOUT_SECONDS` | `12` | Not wired through docker-compose; set in `.env`/compose only if needed. |

Set them as GitHub secrets (`deploy.yml` for Dev, `deploy-prod.yml` for Prod); the deploy writes them to `.env`
and docker-compose passes them to the `worker` service.

## What goes where

Messages that **stay with the bot** (its own flows, not conversation):

- `nfm_reply` (WhatsApp Flow submissions)
- buttons `hospbook_book`, `hospbook_status`, `start_booking`
- QR text triggers: `CHECKIN <code>`, `DRBOOK <id>`, `... QR ID of this hospital is <code>`, `DISCHARGE / RX / RXV <id>`
- any message while the saved step is a follow-up step, an appointment cancel / reschedule / status step,
  the hospital-QR action menu or an OPD check-in step
- any message while the patient is still in a flow started by one of the above in the last 30 minutes and the
  bot still has a step saved (so a QR booking finishes with the bot)

**Everything else goes to Vita**, and then the bot's own safety triage and NLU do not run for that message
(one brain, one safety layer: Vita puts 108 / 112 first in emergencies).

## The loop

- text → `text`; tapped row / button → `reply_id` (exactly as received); shared location → `lat`, `lng`.
- Always `channel: "whatsapp"` and `verified_mobile` = the sender's number; `ui_language` when the bot knows the
  patient's language.
- Vita's `session_id` is kept per number in Redis (`vita:wa:session:<phone>`, 30 minutes, refreshed each message).
- Every message in the reply's `whatsapp` list is queued in order through `outbound_queue` with `to` added; a reply
  with only `message` is sent as text.
- A 422 for a stale `doc:` row sends "That list has expired, please search again" (in the patient's language).
