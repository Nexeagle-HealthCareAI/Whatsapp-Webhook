import json


async def append_conversation_event(
    session_id: str,
    phone_number: str,
    direction: str,
    message_type: str,
    content: str | None,
    step: str | None,
    at: float,
) -> None:
    """Upserts dbo.conversation_sessions and appends one entry to its transcript_json array
    in a single statement -- JSON_MODIFY's 'append $' targets the array in place, so there's
    no separate read-then-write round trip that could race against another append for the
    same session. conversation_logger.py processes its queue strictly one job at a time
    (not concurrently, unlike worker.py/sender.py) specifically so appends for the same
    session_id always land in the order they actually happened; `at` (the enqueue-time
    timestamp) is stored too so that order is verifiable/re-sortable later regardless."""
    from app.db import get_pool
    pool = await get_pool()
    event_json = json.dumps({"direction": direction, "type": message_type, "content": content, "step": step, "at": at})
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.conversation_sessions AS target
            USING (SELECT ? AS session_id) AS src
            ON target.session_id = src.session_id
            WHEN MATCHED THEN
                UPDATE SET
                    last_activity_at = SYSUTCDATETIME(),
                    last_step = ?,
                    transcript_json = JSON_MODIFY(ISNULL(transcript_json, '[]'), 'append $', JSON_QUERY(?))
            WHEN NOT MATCHED THEN
                INSERT (session_id, phone_number, started_at, last_activity_at, last_step, transcript_json)
                VALUES (?, ?, SYSUTCDATETIME(), SYSUTCDATETIME(), ?, JSON_QUERY(?));
            """,
            (
                session_id,
                step, event_json,
                session_id, phone_number, step, f"[{event_json}]",
            ),
        )


async def mark_session_converted(session_id: str, appointment_id: str) -> None:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "UPDATE dbo.conversation_sessions SET appointment_id = ? WHERE session_id = ?",
            (appointment_id, session_id),
        )
