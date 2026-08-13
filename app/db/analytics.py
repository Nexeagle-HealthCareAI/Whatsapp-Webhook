import json

async def log_nlu_interaction(
    phone: str,
    session_id: str | None,
    utterance: str,
    nlu_brain: str,
    intent: str | None,
    confidence: float | None,
    doctor_name: str | None,
    specialty: str | None,
    symptom: str | None,
    formatted_date: str | None,
    routed_step: str | None = None,
    is_correct: int | None = None,
    user_feedback: str | None = None
) -> None:
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            INSERT INTO dbo.nlu_logs (
                phone_number, session_id, utterance, nlu_brain,
                detected_intent, confidence, doctor_name, specialty,
                symptom, formatted_date, routed_step, is_correct, user_feedback
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            (
                phone, session_id, utterance, nlu_brain,
                intent, confidence, doctor_name, specialty,
                symptom, formatted_date, routed_step, is_correct, user_feedback
            )
        )


async def update_last_nlu_log_correctness(phone: str, is_correct: int, feedback: str) -> None:
    """Updates the is_correct flag and user_feedback string on the most recent NLU log for a phone number."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT TOP 1 id FROM dbo.nlu_logs WHERE phone_number = ? ORDER BY created_at DESC",
            (phone,)
        )
        row = await cur.fetchone()
        if row:
            log_id = row[0]
            await cur.execute(
                "UPDATE dbo.nlu_logs SET is_correct = ?, user_feedback = ? WHERE id = ?",
                (is_correct, feedback, log_id)
            )


async def mark_session_nlu_correctness_on_booking(phone: str, booked_doctor_name: str) -> None:
    """Checks previous NLU logs in the active conversation and marks them as correct/incorrect
    by comparing extracted doctor_name with the booked doctor name."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT context_json FROM dbo.conversation_state WHERE phone_number = ?",
            (phone,)
        )
        row = await cur.fetchone()
        if not row or not row[0]:
            return
        context_json = row[0]
        try:
            context = json.loads(context_json)
        except Exception:
            return
        
        session_id = context.get("session_id")
        if not session_id:
            return
            
        await cur.execute(
            "SELECT id, doctor_name FROM dbo.nlu_logs WHERE session_id = ?",
            (session_id,)
        )
        logs = await cur.fetchall()
        for log_id, extracted_doc in logs:
            if extracted_doc:
                ext_clean = extracted_doc.lower().replace("dr", "").replace(".", "").strip()
                booked_clean = booked_doctor_name.lower().replace("dr", "").replace(".", "").strip()
                if ext_clean in booked_clean or booked_clean in ext_clean:
                    await cur.execute(
                        "UPDATE dbo.nlu_logs SET is_correct = 1, user_feedback = 'booked_match' WHERE id = ?",
                        (log_id,)
                    )
                else:
                    await cur.execute(
                        "UPDATE dbo.nlu_logs SET is_correct = 0, user_feedback = 'booked_mismatch' WHERE id = ?",
                        (log_id,)
                    )
