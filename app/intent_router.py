import json
import logging
import time
import re
from app.redis_client import get_redis
from app import db

logger = logging.getLogger("intent_router")

REQUIRED_ENTITIES = {
    "book_appointment": [("doctor_name", "specialty"), "datetime"],  # doctor_name OR specialty, AND datetime
    "check_availability": [("doctor_name", "specialty")],
    "cancel_appointment": [],  # resolved via DB lookup
    "reschedule_appointment": ["datetime"],
    "change_selection": ["new_doctor_name"],
    "ask_pricing": [("doctor_name", "specialty")],
}

class RoutedResult:
    def __init__(self, action: str, intent: str, entities: dict, followup_prompt: str | None = None):
        self.action = action  # "ask_followup" or "proceed_to_business_logic"
        self.intent = intent
        self.entities = entities
        self.followup_prompt = followup_prompt

    def __repr__(self):
        return f"<RoutedResult action={self.action} intent={self.intent} entities={self.entities}>"


def get_followup_prompt(intent: str, missing_slot) -> str:
    """Returns a friendly conversational question in Hinglish/Hindi for a missing slot."""
    if isinstance(missing_slot, (tuple, list)):
        if "doctor_name" in missing_slot or "specialty" in missing_slot:
            return "Aap kis doctor se milna chahte hain ya kis specialty ke liye consult karna chahte hain? (Jaise: Dr. Avinash ya Gynaecologist)"
    
    if missing_slot == "datetime":
        return "Aap appointment kab ki book karna chahte hain? Kripya date aur time batayein (jaise: aaj, kal, parso ya koi specific date)."
    
    if missing_slot == "new_doctor_name":
        return "Aap kis naye doctor ko select karna chahte hain? Kripya unka naam batayein."
        
    return f"Kripya {missing_slot} ke baare mein batayein."


async def route_intent(wa_id: str, validated_nlu_result: dict, raw_text: str = "") -> RoutedResult:
    redis = get_redis()
    redis_key = f"nlu:session:{wa_id}"
    
    # 1. Load existing session state
    stored_state = None
    stored_str = await redis.get(redis_key)
    if stored_str:
        try:
            stored_state = json.loads(stored_str)
        except Exception as e:
            logger.error("Failed to parse NLU session state: %s", e)

    new_intent = validated_nlu_result.get("intent", "out_of_scope")
    new_confidence = validated_nlu_result.get("confidence", "low")
    new_entities = validated_nlu_result.get("entities", {}) or {}

    current_intent = new_intent
    current_entities = new_entities
    awaiting_clarification = False
    resolved_clarification_this_turn = False

    # 2. Check if we have an existing state to merge/resolve
    if stored_state:
        stored_intent = stored_state.get("intent")
        stored_entities = stored_state.get("entities", {}) or {}
        awaiting_clarification = stored_state.get("awaiting_clarification", False)

        if awaiting_clarification:
            # The user was asked to clarify "Naya book karna hai ya reschedule"
            raw_normalized = raw_text.lower().strip()
            # Simple keyword matching for clarification
            is_reschedule = any(k in raw_normalized for k in ["reschedule", "change", "shift", "badal", "parso", "kal", "time"]) or new_intent == "reschedule_appointment"
            is_new_booking = any(k in raw_normalized for k in ["new", "naya", "dusra", "another", "fresh", "book"])

            if is_reschedule:
                current_intent = "reschedule_appointment"
                current_entities = {**stored_entities, **new_entities}
                awaiting_clarification = False
            elif is_new_booking:
                current_intent = "book_appointment"
                current_entities = stored_entities  # keep slots, drop clarification
                awaiting_clarification = False
                resolved_clarification_this_turn = True
            else:
                # Keep asking
                active_date = stored_state.get("active_appt_date", "")
                return RoutedResult(
                    action="ask_followup",
                    intent=stored_intent,
                    entities=stored_entities,
                    followup_prompt=f"Aapki already ek appointment hai {active_date}. Naya book karna hai ya usko reschedule karna hai?"
                )
        else:
            # General state merging:
            # Switch to new intent if new intent is a distinct high/medium confidence intent (not out_of_scope)
            if new_intent and new_intent != "out_of_scope" and new_intent != stored_intent and new_confidence in ("high", "medium"):
                current_intent = new_intent
                current_entities = new_entities
            else:
                current_intent = stored_intent
                current_entities = {**stored_entities, **new_entities}

    # 3. Check for required entities
    requirements = REQUIRED_ENTITIES.get(current_intent, [])
    missing_slot = None
    for req in requirements:
        if isinstance(req, (tuple, list)):
            # Check if any of the elements in the tuple is present
            if not any(current_entities.get(opt) for opt in req):
                missing_slot = req
                break
        else:
            if not current_entities.get(req):
                missing_slot = req
                break

    if missing_slot:
        # Save updated state and ask follow up
        state_to_save = {
            "intent": current_intent,
            "entities": current_entities,
            "awaiting_clarification": False,
            "updated_at": time.time()
        }
        await redis.set(redis_key, json.dumps(state_to_save), ex=900)  # 15 minutes TTL
        
        prompt = get_followup_prompt(current_intent, missing_slot)
        return RoutedResult(
            action="ask_followup",
            intent=current_intent,
            entities=current_entities,
            followup_prompt=prompt
        )

    # 4. Check for active upcoming appointment conflict (Only if all slots are present for book_appointment)
    if current_intent == "book_appointment" and not awaiting_clarification and not resolved_clarification_this_turn:
        # Check active booked/pending appointments for this user in the DB
        has_active = False
        active_date_str = ""
        try:
            pool = await db.get_pool()
            async with pool.acquire() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT TOP 1 preferred_date FROM dbo.pending_appointments "
                    "WHERE phone_number = ? AND status IN ('pending', 'booked') AND preferred_date >= CAST(GETDATE() AS DATE) "
                    "ORDER BY preferred_date ASC",
                    (wa_id,)
                )
                row = await cur.fetchone()
                if row:
                    has_active = True
                    active_date_str = row[0].strftime("%Y-%m-%d") if hasattr(row[0], "strftime") else str(row[0])
        except Exception as exc:
            logger.error("DB check for active appointments failed: %s", exc)

        if has_active:
            # Persist clarification state and ask clarifying question
            state_to_save = {
                "intent": "book_appointment",
                "entities": current_entities,
                "awaiting_clarification": True,
                "active_appt_date": active_date_str,
                "updated_at": time.time()
            }
            await redis.set(redis_key, json.dumps(state_to_save), ex=900)
            
            prompt = f"Aapki already ek appointment hai {active_date_str}. Naya book karna hai ya usko reschedule karna hai?"
            return RoutedResult(
                action="ask_followup",
                intent=current_intent,
                entities=current_entities,
                followup_prompt=prompt
            )

    # 5. Success! Clear session state and proceed to business logic
    await redis.delete(redis_key)
    return RoutedResult(
        action="proceed_to_business_logic",
        intent=current_intent,
        entities=current_entities
    )
