"""Deploy-time check for Vita mode (app/vita_bridge.py), run inside the worker container like
app/healthcheck_symptom.py: with VITA_CHAT_URL set, the router must be reachable from here and must
accept this bot's key. Otherwise every patient message would quietly get the "please try again"
fallback. Vita mode off (no VITA_CHAT_URL) passes.

The probe asks for a session that cannot exist, so nothing is created and no model call is made:
  404 naming the session -> Vita is there and the key has the "chat" scope   (pass)
  404 without it         -> that router has no Vita (/v2/agent/chat missing)  (fail)
  401 / 403              -> key missing, unknown, or without the "chat" scope (fail)
Exit code 0 = pass; 1 = fail, with the reason on stderr."""

import sys

import httpx

from app.config import settings

PROBE_SESSION = "vita-deploy-readiness-check"


def check(transport=None) -> tuple[bool, str]:
    url = (settings.vita_chat_url or "").strip()
    if not url:
        return True, "Vita mode off (VITA_CHAT_URL not set)"
    if not url.endswith("/v2/agent/chat"):
        return False, f"VITA_CHAT_URL must end with /v2/agent/chat: {url}"
    probe = url[: -len("/chat")] + f"/session/{PROBE_SESSION}"
    headers = {"X-Api-Key": settings.vita_api_key} if settings.vita_api_key else {}
    try:
        with httpx.Client(timeout=8, transport=transport) as client:
            res = client.get(probe, headers=headers)
    except httpx.HTTPError as exc:
        return False, f"router unreachable from the worker at {url}: {exc.__class__.__name__}"
    try:
        detail = str((res.json() or {}).get("detail") or "")
    except ValueError:
        detail = ""
    if res.status_code == 404 and detail.lower().startswith("session"):     # not the echoed path
        return True, f"Vita reachable at {url}, key accepted"
    if res.status_code == 404:
        return False, f"{url}: this router has no Vita (deploy 1HMS-NLP-Router with /v2/agent/chat)"
    if res.status_code in (401, 403):
        return False, f"router refused VITA_API_KEY ({res.status_code}): it needs the chat scope (and verified_mobile)"
    if res.status_code == 200:
        return True, f"Vita reachable at {url}"
    return False, f"router answered {res.status_code} at {probe}"


if __name__ == "__main__":
    ok, why = check()
    print(why, file=sys.stdout if ok else sys.stderr)
    sys.exit(0 if ok else 1)
