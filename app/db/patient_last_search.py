from datetime import datetime, timezone
from typing import Any


async def save_last_location(
    phone_number: str, city: str | None, location_text: str | None,
    lat: float | None, lng: float | None,
) -> None:
    """Records whatever location a patient's search just resolved to -- independent of
    conversation_state, which gets wiped on a full restart. Feeds the "still looking near
    {city}?" reuse prompt on a later "Book Appointment" tap (see app/conversation/
    last_search.py). Called unconditionally whenever a location resolves, not just for that
    feature, so the record stays fresh for whenever it's actually needed."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.patient_last_search AS target
            USING (SELECT ? AS phone_number) AS src
            ON target.phone_number = src.phone_number
            WHEN MATCHED THEN
                UPDATE SET last_city = ?, last_location_text = ?,
                           last_patient_lat = ?, last_patient_lng = ?,
                           location_updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (phone_number, last_city, last_location_text, last_patient_lat, last_patient_lng, location_updated_at)
                VALUES (?, ?, ?, ?, ?, SYSUTCDATETIME());
            """,
            (
                phone_number,
                city, location_text, lat, lng,
                phone_number, city, location_text, lat, lng,
            ),
        )


async def save_last_specialty(phone_number: str, specialty_category: str) -> None:
    """Records whatever specialty a patient's symptom/browse search just resolved to. Same
    "independent of conversation_state" reasoning as save_last_location above -- see that
    function's docstring."""
    from app.db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            """
            MERGE dbo.patient_last_search AS target
            USING (SELECT ? AS phone_number) AS src
            ON target.phone_number = src.phone_number
            WHEN MATCHED THEN
                UPDATE SET last_specialty_category = ?, specialty_updated_at = SYSUTCDATETIME()
            WHEN NOT MATCHED THEN
                INSERT (phone_number, last_specialty_category, specialty_updated_at)
                VALUES (?, ?, SYSUTCDATETIME());
            """,
            (phone_number, specialty_category, phone_number, specialty_category),
        )


async def get_last_search(phone_number: str) -> dict[str, Any] | None:
    from app.db import get_pool
    pool = await get_pool()
    columns = [
        "last_city", "last_location_text", "last_patient_lat", "last_patient_lng",
        "location_updated_at", "last_specialty_category", "specialty_updated_at",
    ]
    async with pool.acquire() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT {', '.join(columns)} FROM dbo.patient_last_search WHERE phone_number = ?",
            (phone_number,),
        )
        row = await cur.fetchone()
        return dict(zip(columns, row)) if row else None


def is_last_search_fresh(row: dict[str, Any] | None, *, now: datetime | None = None, max_age_hours: float = 24) -> bool:
    """Pure, no I/O -- the one thing callers actually branch on. True only when BOTH a
    location (city or lat) AND a specialty are on record, AND both were set within
    max_age_hours of `now`. Deliberately an AND, not "whichever is fresher" -- a location
    reuse combined with a stale specialty (or vice versa) is exactly the kind of mismatched
    combination that makes the confirm prompt misleading rather than helpful."""
    if not row:
        return False
    has_location = bool(row.get("last_city") or row.get("last_patient_lat") is not None)
    has_specialty = bool(row.get("last_specialty_category"))
    if not (has_location and has_specialty):
        return False
    location_at = row.get("location_updated_at")
    specialty_at = row.get("specialty_updated_at")
    if location_at is None or specialty_at is None:
        return False

    now = now or datetime.now(timezone.utc)
    cutoff_seconds = max_age_hours * 3600
    for stamp in (location_at, specialty_at):
        # SYSUTCDATETIME() round-trips through aioodbc as a naive datetime -- attach UTC
        # rather than assume the caller's `now` is naive too.
        aware_stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)
        if (now - aware_stamp).total_seconds() > cutoff_seconds:
            return False
    return True
