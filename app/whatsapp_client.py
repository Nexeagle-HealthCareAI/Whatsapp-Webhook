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


async def _send(client: httpx.AsyncClient, payload: dict) -> None:
    response = await client.post(
        GRAPH_API_URL,
        headers={"Authorization": f"Bearer {settings.whatsapp_token}"},
        json=payload,
    )
    logger.info("WhatsApp send to %s -> %s", payload.get("to"), response.status_code)
    if response.status_code >= 400:
        logger.error("WhatsApp send failed: %s", response.text)


async def send_text(client: httpx.AsyncClient, to: str, body: str) -> None:
    await _send(
        client,
        {"messaging_product": "whatsapp", "to": to, "text": {"body": body}},
    )


async def send_list(
    client: httpx.AsyncClient,
    to: str,
    body_text: str,
    button_label: str,
    rows: list[tuple[str, str]],
    section_title: str = "Options",
) -> None:
    """rows: list of (id, title) — id is what comes back as list_reply.id on selection."""
    section_rows = [
        {"id": row_id, "title": title[:_MAX_ROW_TITLE]} for row_id, title in rows[:_MAX_LIST_ROWS]
    ]
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
