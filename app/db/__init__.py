import aioodbc

from app.config import settings
from app.db.dedup import is_message_processed, mark_message_processed
from app.db.conversation_state import get_conversation_state, save_conversation_state, clear_conversation_state
from app.db.analytics import log_nlu_interaction, update_last_nlu_log_correctness, mark_session_nlu_correctness_on_booking
from app.db.conversation_log import append_conversation_event, mark_session_converted
from app.db.checkin_queue import upsert_checkin_notification, save_queue_status, list_due_followups, mark_followup_sent
from app.db.appointments import (
    has_pending_appointment,
    get_upcoming_active_appointment,
    create_pending_appointment,
    mark_appointment_booked,
    mark_appointment_failed,
    get_appointment_by_hms_id,
    get_booked_appointments_for_phone,
    mark_appointment_cancelled_locally,
    mark_appointment_rescheduled_locally,
)

_pool: aioodbc.Pool | None = None


async def get_pool() -> aioodbc.Pool:
    global _pool
    if _pool is None:
        _pool = await aioodbc.create_pool(dsn=settings.sqlserver_conn_string, autocommit=True)
    return _pool
