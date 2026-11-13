# Vita as the front door of the doctor search

Vita is the chat agent in the NLP Router (`POST /v2/agent/chat`). For the numbers it is switched on for, the step
where the bot asks "how would you like to find a doctor?" becomes a conversation: the patient says what they need in
their own words (symptoms, a doctor's name, a specialty, an area) and Vita searches and suggests.

**Booking is not Vita's.** The moment the patient picks a doctor, the bot takes over with its own day/shift,
patient-details form, confirm and booking steps, unchanged. Cancel, reschedule, check-in, QR codes and the follow-up
reply are also unchanged.

## Switching it on

| Setting | Meaning |
|---|---|
| `VITA_API_URL` | The router's base URL, e.g. `http://151.185.45.77:5003` (dev). |
| `VITA_API_KEY` | A router client key with the `chat` scope (`scripts/make_api_key.py whatsapp-bot chat` in the router repo). |
| `VITA_ALLOWED_PHONES` | Comma-separated numbers (any format, matched on the last 10 digits), or `*` for everyone. |

Any of the three empty: the bot behaves exactly as before. Only the dev deploy workflow passes them; the prod
workflow does not, so prod stays off until it is wired on purpose.

## What happens on a turn (step `vita_chat`)

1. The patient's message goes to Vita **before** the bot's own language detection and NLU (they would answer the same
   sentence a second time). A typed message is `text`, a tapped row or button is `reply_id`, a shared location is
   `lat`/`lng`. The Vita session id and the city the bot already has travel with it.
2. Each message in Vita's `reply.whatsapp` is queued as it is (the bot adds only `to` and `messaging_product`).
3. `doctor_selected`: Vita's own reply is **not** sent. The doctor is read from 1HMS (`/public/doctors/{id}`) and the
   bot's `_handle_choosing_doctor` runs as if the patient had tapped that doctor in the bot's own list.
4. "cancel", "reschedule", "my appointment" (English, Hinglish, Hindi, Bengali) leave Vita for the rest of the
   conversation and go through the bot's own intent handling. Exact `back` / `cancel` / `quit` stay the bot's.

## Falling back

If Vita is unreachable, answers 4xx/5xx (except 422, below) or sends something unusable, the patient gets one notice
and the three search buttons, and Vita is off for the rest of that conversation (`vita_off` in the saved context; the
state is cleared when the conversation ends, so the next one tries Vita again). A 422 means an expired session: a typed
message starts a new Vita session, a tap on an old row is answered with the introduction again.

## Tests

`python3 tests/test_vita_chat.py` (part of `pytest tests/`) fakes Vita. `tests/e2e` on the `feature/aquib-vita-e2e`
branch calls the live router.
