import logging

import httpx

from app.config import settings

logger = logging.getLogger("whatsapp_client")

GRAPH_API_URL = f"https://graph.facebook.com/v19.0/{settings.whatsapp_phone_number_id}/messages"

# WhatsApp interactive-message limits (Meta Cloud API).
_MAX_LIST_ROWS = 10
_MAX_BUTTONS = 3
_MAX_ROW_TITLE = 24
_MAX_BUTTON_TITLE = 20


async def _send(client: httpx.AsyncClient, payload: dict) -> httpx.Response:
    response = await client.post(
        GRAPH_API_URL,
        headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
        json=payload,
    )
    logger.info(
        "WhatsApp send to %s -> %s",
        payload.get("to") or f"msg:{payload.get('message_id')}",
        response.status_code,
    )
    if response.status_code >= 400:
        logger.error("WhatsApp send failed: %s", response.text)
    return response


async def send_text(client: httpx.AsyncClient, to: str, body: str) -> None:
    await _send(
        client,
        {"messaging_product": "whatsapp", "to": to, "text": {"body": body}},
    )


async def send_typing_indicator(client: httpx.AsyncClient, message_id: str) -> None:
    """Marks the inbound message read and shows "typing…" to the sender — one combined
    call (per Meta's Cloud API), not two. Meta dismisses the indicator once we actually
    reply or after 25s, whichever comes first. Best-effort: a failure here shouldn't stop
    the reply itself, so callers should swallow exceptions from this rather than propagate."""
    await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        },
    )


_MAX_ROW_DESC = 72


def _row_dict(row: tuple) -> dict:
    """rows accepts either (id, title) or (id, title, description) — description is the
    small grey subtitle line WhatsApp renders under each row title, used for doctor cards
    (rating/fee/distance) so the sort order in Section 5 of the feature request is actually
    visible to the patient, not just an invisible ordering."""
    row_id, title, *rest = row
    entry = {"id": row_id, "title": title[:_MAX_ROW_TITLE]}
    if rest and rest[0]:
        entry["description"] = str(rest[0])[:_MAX_ROW_DESC]
    return entry


async def send_list(
    client: httpx.AsyncClient,
    to: str,
    body_text: str,
    button_label: str,
    rows: list[tuple],
    section_title: str = "Options",
) -> None:
    """rows: list of (id, title) or (id, title, description) — id is what comes back as
    list_reply.id on selection."""
    section_rows = [_row_dict(row) for row in rows[:_MAX_LIST_ROWS]]
    await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "list",
                "body": {"text": body_text},
                "action": {
                    "button": button_label,
                    "sections": [{"title": section_title, "rows": section_rows}],
                },
            },
        },
    )


async def send_buttons(
    client: httpx.AsyncClient, to: str, body_text: str, buttons: list[tuple[str, str]]
) -> None:
    """buttons: list of (id, title) — id is what comes back as button_reply.id on selection."""
    reply_buttons = [
        {"type": "reply", "reply": {"id": btn_id, "title": title[:_MAX_BUTTON_TITLE]}}
        for btn_id, title in buttons[:_MAX_BUTTONS]
    ]
    await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {"text": body_text},
                "action": {"buttons": reply_buttons},
            },
        },
    )


async def send_location_request(client: httpx.AsyncClient, to: str, body_text: str) -> None:
    """Meta's native location_request_message — the button this renders opens the phone's
    own location picker, which defaults to "Send your current location" (one-tap GPS fill).
    That one-tap default is the closest real equivalent to "auto-detect" WhatsApp allows —
    there's no way for a bot to read a phone's GPS without this explicit, patient-initiated
    share; a true silent auto-detect isn't something Meta's platform (or basic consent
    practice) permits. Patients who'd rather not share GPS can just reply with a typed
    city/area name instead — handled as free text alongside this in conversation.py."""
    await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "location_request_message",
                "body": {"text": body_text},
                "action": {"name": "send_location"},
            },
        },
    )


async def send_location(
    client: httpx.AsyncClient, to: str, latitude: float, longitude: float, name: str, address: str
) -> None:
    """Sends a droppable map pin — used at booking confirmation so the patient has the
    clinic's location without needing to ask reception for directions."""
    await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "location",
            "location": {
                "latitude": latitude,
                "longitude": longitude,
                "name": name[:1000],
                "address": address[:1000],
            },
        },
    )


async def send_document(
    client: httpx.AsyncClient, to: str, url: str, filename: str, caption: str | None = None
) -> bool:
    """Sends a document by URL (Meta fetches it) — a plain message, not a template, since
    this is only ever called from inside an active 24h conversation window (the patient just
    sent us a message to trigger it). Used for the QR-scan pull flows (DISCHARGE/RX/RXV
    codes in conversation.py) — distinct from the template-based pushes easyHMSAPI's own
    IWhatsAppMessagingService sends directly on upload. Returns True if successful."""
    document: dict = {"link": url, "filename": filename}
    if caption:
        document["caption"] = caption
    response = await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "document",
            "document": document,
        },
    )
    return response.status_code < 400


async def send_flow(
    client: httpx.AsyncClient,
    to: str,
    body_text: str,
    flow_id: str,
    flow_cta: str,
    screen_id: str,
    flow_token: str,
    initial_data: dict | None = None,
) -> bool:
    """Sends a standalone WhatsApp Flow interactive message. Returns True if successful."""
    parameters = {
        "flow_message_version": "3",
        "flow_token": flow_token,
        "flow_id": flow_id,
        "flow_cta": flow_cta,
        "flow_action": "navigate",
        "flow_action_payload": {
            "screen": screen_id,
        },
    }
    if initial_data:
        parameters["flow_action_payload"]["data"] = initial_data

    response = await _send(
        client,
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "flow",
                "body": {"text": body_text},
                "action": {
                    "name": "flow",
                    "parameters": parameters,
                },
            },
        },
    )
    return response.status_code < 400
