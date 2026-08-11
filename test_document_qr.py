"""
Sanity checks for the discharge-summary / prescription QR pull flows: the early
DISCHARGE/RX/RXV triggers in app.conversation.handle_message (_DOCUMENT_TRIGGERS /
_handle_document_trigger). Same style as test_checkin.py -- stubs aioodbc/redis before
importing app modules, mocks hms_client/db/whatsapp_client calls directly, no pytest.
Run directly: python3 test_document_qr.py
"""

import asyncio
import os
import sys
import types
from unittest.mock import AsyncMock, patch

import httpx

# Stub ODBC database before importing app modules
_fake_odbc = types.ModuleType("aioodbc")
_fake_odbc.Pool = object


async def _create_pool(*a, **k):
    raise NotImplementedError


_fake_odbc.create_pool = _create_pool
sys.modules.setdefault("aioodbc", _fake_odbc)

# Stub Redis before importing app modules
_fake_redis_mod = types.ModuleType("redis")
_fake_redis_mod.asyncio = types.ModuleType("redis.asyncio")


class MockRedis:
    def __init__(self):
        self.data = {}

    @classmethod
    def from_url(cls, *args, **kwargs):
        return _mock_redis_instance

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    async def delete(self, key):
        self.data.pop(key, None)
        return True


_mock_redis_instance = MockRedis()
_fake_redis_mod.asyncio.Redis = MockRedis
sys.modules.setdefault("redis", _fake_redis_mod)
sys.modules.setdefault("redis.asyncio", _fake_redis_mod.asyncio)

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__))))

for _key in [
    "WHATSAPP_TOKEN", "WHATSAPP_PHONE_NUMBER_ID", "WHATSAPP_VERIFY_TOKEN",
    "WHATSAPP_APP_SECRET", "SQLSERVER_CONN_STRING", "INTERNAL_EVENTS_TOKEN",
]:
    os.environ.setdefault(_key, "test")

from app import conversation  # noqa: E402
from app.hms_client import HmsApiError  # noqa: E402

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)
        print(f"FAIL: {message}")
    else:
        print(f"PASS: {message}")


def run(coro):
    return asyncio.run(coro)


class _RecordingWhatsApp:
    """Records every send_* call instead of hitting the real Graph API."""

    def __init__(self):
        self.texts: list[str] = []
        self.documents: list[tuple] = []

    async def send_text(self, client, to, body):
        self.texts.append(body)

    async def send_document(self, client, to, url, filename, caption=None):
        self.documents.append((to, url, filename, caption))
        return True

    async def send_typing_indicator(self, client, message_id):
        pass


class _FailingDocumentWhatsApp(_RecordingWhatsApp):
    """Same as above, but send_document reports a delivery failure (Meta/network error)."""

    async def send_document(self, client, to, url, filename, caption=None):
        self.documents.append((to, url, filename, caption))
        return False


class _RecordingDb:
    """In-memory stand-in for app.db's conversation_state calls."""

    def __init__(self, initial_state: dict | None = None):
        self._state = initial_state

    async def get_conversation_state(self, phone):
        return self._state

    async def save_conversation_state(self, phone, step, context):
        self._state = {"current_step": step, "context": context}

    async def clear_conversation_state(self, phone):
        self._state = None


def test_trigger_patterns_match_and_ignore():
    print("\n--- DISCHARGE/RX/RXV trigger patterns ---")
    m = conversation._DISCHARGE_TRIGGER_PATTERN.match("DISCHARGE abc123")
    check(m is not None and m.group(1) == "abc123", "DISCHARGE <token> matches")
    check(conversation._DISCHARGE_TRIGGER_PATTERN.match("discharge") is None, "bare 'discharge' with no token does not match")

    m2 = conversation._PRESCRIPTION_TRIGGER_PATTERN.match("RX guid-1")
    check(m2 is not None and m2.group(1) == "guid-1", "RX <id> matches")
    check(conversation._PRESCRIPTION_TRIGGER_PATTERN.match("RXV guid-2") is None, "RXV text does not also match the RX pattern")

    m3 = conversation._VISIT_SUMMARY_TRIGGER_PATTERN.match("rxv guid-3")
    check(m3 is not None and m3.group(1) == "guid-3", "lowercase rxv <id> matches case-insensitively")
    check(conversation._VISIT_SUMMARY_TRIGGER_PATTERN.match("RX guid-4") is None, "RX text does not also match the RXV pattern")


def test_discharge_not_available_preserves_state():
    print("\n--- Unresolvable DISCHARGE token ---")
    db_mock = _RecordingDb(initial_state={"current_step": "choosing_doctor", "context": {"lang": "en", "doctor_id": "d1"}})
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_discharge_summary_url", AsyncMock(side_effect=HmsApiError("not found"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DISCHARGE badtoken", "msg1")

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one 'not available' message")
    check(len(wa_mock.documents) == 0, "never calls send_document for an unresolvable token")
    check(db_mock._state["current_step"] == "choosing_doctor", "existing in-progress state is left untouched")


def test_discharge_valid_token_sends_document():
    print("\n--- Valid DISCHARGE token delivers the document ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_discharge_summary_url", AsyncMock(return_value="https://storage.example/d.pdf")):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DISCHARGE goodtoken", "msg2")

    run(_run())
    check(len(wa_mock.documents) == 1, "sends exactly one document")
    check(wa_mock.documents[0][1] == "https://storage.example/d.pdf", "sends the resolved URL")
    check(wa_mock.documents[0][2] == "Discharge_Summary.pdf", "uses the discharge filename")
    check(len(wa_mock.texts) == 0, "no separate 'not available' text is sent alongside a successful delivery")


def test_prescription_valid_id_sends_document():
    print("\n--- Valid RX id delivers the document ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_prescription_attachment_url", AsyncMock(return_value="https://storage.example/rx.pdf")):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "RX guid-1", "msg3")

    run(_run())
    check(len(wa_mock.documents) == 1, "sends exactly one document")
    check(wa_mock.documents[0][2] == "Prescription.pdf", "uses the prescription filename")


def test_visit_summary_valid_id_sends_document():
    print("\n--- Valid RXV id delivers the document via the visit-summary resolver ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()
    attachment_resolver = AsyncMock(side_effect=AssertionError("wrong resolver called for RXV"))

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_prescription_attachment_url", attachment_resolver), \
             patch.object(conversation.hms_client, "get_visit_summary_url", AsyncMock(return_value="https://storage.example/rxv.pdf")):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "RXV appt-1", "msg4")

    run(_run())
    check(attachment_resolver.await_count == 0, "RXV never calls the RX (PrescriptionAttachment) resolver")
    check(len(wa_mock.documents) == 1 and wa_mock.documents[0][1] == "https://storage.example/rxv.pdf", "delivers via the visit-summary resolver")


def test_prescription_not_available_message():
    print("\n--- Unresolvable RX id ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _RecordingWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_prescription_attachment_url", AsyncMock(side_effect=HmsApiError("not found"))):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "RX badid", "msg5")

    run(_run())
    check(len(wa_mock.texts) == 1, "sends exactly one 'not available' message")
    check(len(wa_mock.documents) == 0, "never calls send_document for an unresolvable id")


def test_send_failure_sends_generic_error_not_not_available():
    print("\n--- Document exists but the WhatsApp send itself fails ---")
    db_mock = _RecordingDb(initial_state=None)
    wa_mock = _FailingDocumentWhatsApp()

    async def _run():
        with patch.object(conversation, "db", db_mock), \
             patch.object(conversation, "whatsapp_client", wa_mock), \
             patch.object(conversation.hms_client, "get_discharge_summary_url", AsyncMock(return_value="https://storage.example/d.pdf")):
            async with httpx.AsyncClient() as client:
                await conversation.handle_message(client, "919876543210", "Test", "text", "DISCHARGE goodtoken", "msg6")

    run(_run())
    check(len(wa_mock.documents) == 1, "still attempts the send")
    check(len(wa_mock.texts) == 1, "sends a follow-up text when the send itself fails")
    from app.i18n import t as _t
    check(wa_mock.texts[0] == _t("error_hms", "en"), "sends the generic retry message, not the 'not available' message")


if __name__ == "__main__":
    test_trigger_patterns_match_and_ignore()
    test_discharge_not_available_preserves_state()
    test_discharge_valid_token_sends_document()
    test_prescription_valid_id_sends_document()
    test_visit_summary_valid_id_sends_document()
    test_prescription_not_available_message()
    test_send_failure_sends_generic_error_not_not_available()

    print("\n" + "=" * 50)
    if failures:
        print(f"DOCUMENT QR TESTS FAILED with {len(failures)} errors")
        sys.exit(1)
    else:
        print("ALL DOCUMENT QR TESTS PASSED")
