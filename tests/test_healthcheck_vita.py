"""Deploy-time Vita check (app/healthcheck_vita.py): passes with Vita off or a router that has Vita and
accepts the key; fails on a wrong URL, a router without Vita, a refused key, or an unreachable router.
Runs under pytest or directly: python3 tests/test_healthcheck_vita.py"""

import os
import sys
from unittest.mock import patch

import httpx

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
for _key in ["WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET",
             "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN"]:
    os.environ.setdefault(_key, "test")

from app import healthcheck_vita  # noqa: E402
from app.config import settings  # noqa: E402

URL = "http://host.docker.internal:5003/v2/agent/chat"


def _run(url, status=None, text="", error=None, key="k-1"):
    seen = []

    def handler(request):
        seen.append(request)
        if error:
            raise error
        return httpx.Response(status, text=text)
    with patch.object(settings, "vita_chat_url", url), patch.object(settings, "vita_api_key", key):
        ok, why = healthcheck_vita.check(transport=httpx.MockTransport(handler))
    return ok, why, seen


def test_vita_off_passes():
    ok, why, seen = _run(None)
    assert ok and "off" in why and not seen


def test_router_with_vita_and_a_good_key_passes():
    ok, _, seen = _run(URL, 404, '{"title":"Not found","status":404,"detail":"Session '
                          '\'vita-deploy-readiness-check\' not found or has expired"}')
    assert ok
    assert str(seen[0].url) == "http://host.docker.internal:5003/v2/agent/session/vita-deploy-readiness-check"
    assert seen[0].headers["x-api-key"] == "k-1" and seen[0].method == "GET"     # nothing created


def test_router_without_vita_fails():
    ok, why, _ = _run(URL, 404, '{"detail":"Not Found"}')
    assert not ok and "no Vita" in why
    ok, _, _ = _run(URL, 404, '{"detail":"Not Found","instance":"/v2/agent/session/vita-deploy-readiness-check"}')
    assert not ok                                     # the path echoed back is not Vita answering


def test_refused_key_fails():
    for status in (401, 403):
        ok, why, _ = _run(URL, status, "nope")
        assert not ok and "VITA_API_KEY" in why


def test_unreachable_router_fails():
    ok, why, _ = _run(URL, error=httpx.ConnectError("refused"))
    assert not ok and "unreachable" in why


def test_wrong_url_shape_fails():
    ok, why, seen = _run("http://host.docker.internal:5003/v2/agent")
    assert not ok and "must end with /v2/agent/chat" in why and not seen


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL: {t.__name__} {exc}")
    print(f"\n{failed} failure(s)")
    sys.exit(1 if failed else 0)
