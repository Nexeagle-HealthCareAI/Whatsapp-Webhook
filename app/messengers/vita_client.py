import logging
from functools import lru_cache

import httpx

from app.config import settings

logger = logging.getLogger("vita_client")


class VitaUnavailable(Exception):
    """Vita could not be asked, or answered with something this bot cannot use. The caller carries on without it."""


class VitaRejected(VitaUnavailable):
    """Vita refused the request itself (HTTP 4xx), e.g. 422 for a tapped row from an expired session."""

    def __init__(self, status_code: int):
        super().__init__(f"Vita answered HTTP {status_code}")
        self.status_code = status_code


@lru_cache
def _get_client() -> httpx.AsyncClient:
    """One shared connection for the process's lifetime, same lazy-singleton pattern as symptom_client."""
    return httpx.AsyncClient(base_url=(settings.vita_api_url or "").rstrip("/"), timeout=settings.vita_timeout_seconds)


def is_configured() -> bool:
    return bool(settings.vita_api_url and settings.vita_api_key)


async def chat(
    *,
    text: str | None = None,
    reply_id: str | None = None,
    lat: float | None = None,
    lng: float | None = None,
    session_id: str | None = None,
    city: str | None = None,
) -> dict:
    """One turn with Vita (POST /v2/agent/chat as the WhatsApp channel). Exactly one of text, reply_id or a location.

    Returns Vita's reply: kind, session_id, whatsapp (ready-to-send Cloud API messages) and, for doctor_selected,
    doctor. Raises VitaRejected for a 4xx and VitaUnavailable for anything else that went wrong. The key and the
    patient's words are never logged.
    """
    body: dict = {"channel": "whatsapp"}
    if reply_id is not None:
        body["reply_id"] = reply_id
    elif lat is not None and lng is not None:
        body["lat"], body["lng"] = lat, lng
    elif text is not None:
        body["text"] = text
    if session_id:
        body["session_id"] = session_id
    if city:
        body["city"] = city
    try:
        response = await _get_client().post("/v2/agent/chat", json=body, headers={"X-Api-Key": settings.vita_api_key or ""})
    except httpx.HTTPError as exc:
        logger.warning("Vita unreachable: %s", type(exc).__name__)
        raise VitaUnavailable(str(type(exc).__name__)) from exc
    if 400 <= response.status_code < 500:
        logger.warning("Vita rejected the request: HTTP %s", response.status_code)
        raise VitaRejected(response.status_code)
    if response.status_code >= 500:
        logger.warning("Vita failed: HTTP %s", response.status_code)
        raise VitaUnavailable(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise VitaUnavailable("not JSON") from exc
    if not isinstance(data, dict) or not data.get("kind"):
        raise VitaUnavailable("unexpected reply")
    return data
