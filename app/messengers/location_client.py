"""
app/messengers/location_client.py
------------------------------------
Wraps the location-search API (see app.config.settings.location_api_base_url) -- a real
geocoder that turns a typed place name into up to `limit` canonical matches (city/district/
town, state, coordinates), unlike app.decision_maker.city_resolver.match_typed_city, which
only ever matches against the (much smaller) set of cities that already have a doctor
registered in 1HMS, and only ever returns one guess or nothing.

app.conversation.location.py is the caller, and treats any failure here (network error,
timeout, non-2xx) as "the search didn't work this time" and falls back to the old local
city_index matching -- this client itself just raises, same as hms_client/symptom_client.
"""
import logging
from functools import lru_cache

import httpx

from app.config import settings

logger = logging.getLogger("location_client")


@lru_cache
def _get_client() -> httpx.AsyncClient:
    """One shared, reused connection for the process's whole lifetime -- same lazy-singleton
    pattern as hms_client/symptom_client/redis_client."""
    return httpx.AsyncClient(base_url=settings.location_api_base_url, timeout=5)


async def search_locations(query: str, limit: int = 5) -> list[dict]:
    """Returns up to `limit` matches, each a dict with at least "name" and "type"
    (city/district/town); "state", "district", "pincodes", "coordinates" ({"latitude",
    "longitude"} or None) are passed through as the API returns them. Raises on any
    transport/HTTP error -- the caller decides how to degrade, not this function."""
    client = _get_client()
    response = await client.get("/search", params={"city": query, "limit": limit})
    response.raise_for_status()
    data = response.json()
    return data.get("matches") or []
