"""
app/city_index.py
Turns a patient's shared GPS coordinates into a city name that /public/doctors?city= will
actually match, WITHOUT hard-coding a town list — the index is built from 1HMS's own data.
"""

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.messengers.redis_client import get_redis
from app.decision_maker.city_resolver import build_from_doctors, nearest_city, cities_within, match_typed_city

logger = logging.getLogger("city_index")

_REDIS_KEY = "booking:city_index"


def _headers() -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.hms_api_key:
        headers["X-Api-Key"] = settings.hms_api_key
    return headers


async def _fetch_all_doctors() -> list[dict[str, Any]]:
    """Pages through the whole public doctor directory. Only ever called on a cache miss
    (once a day in practice), never on the hot path of a normal booking — see get_index."""
    doctors: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=settings.hms_api_base_url, timeout=30) as client:
        for page in range(1, settings.city_index_max_pages + 1):
            response = await client.get(
                "/public/doctors",
                params={"page": page, "pageSize": settings.city_index_page_size},
                headers=_headers(),
            )
            response.raise_for_status()
            batch = response.json().get("doctors", [])
            doctors.extend(batch)
            if len(batch) < settings.city_index_page_size:
                break
        else:
            logger.warning(
                "Stopped building city index at the %d-page cap — some cities may be missing. "
                "Raise CITY_INDEX_MAX_PAGES if the doctor directory has grown.",
                settings.city_index_max_pages,
            )
    return doctors


async def get_index(force_refresh: bool = False) -> dict[str, list[list[float]]]:
    redis = get_redis()
    if not force_refresh:
        cached = await redis.get(_REDIS_KEY)
        if cached:
            return json.loads(cached)

    doctors = await _fetch_all_doctors()
    index = build_from_doctors(doctors)
    if index:
        await redis.set(_REDIS_KEY, json.dumps(index), ex=settings.city_index_ttl_seconds)
        logger.info("Built city index: %d cities from %d doctors", len(index), len(doctors))
    else:
        logger.warning("City index came back empty — leaving cache untouched")
    return index


_DOCTORS_CACHE_KEY = "hms:all_doctors"


async def get_all_doctors(force_refresh: bool = False) -> list[dict[str, Any]]:
    redis = get_redis()
    if not force_refresh:
        cached = await redis.get(_DOCTORS_CACHE_KEY)
        if cached:
            try:
                return json.loads(cached)
            except Exception:
                pass

    doctors = await _fetch_all_doctors()
    if doctors:
        try:
            await redis.set(_DOCTORS_CACHE_KEY, json.dumps(doctors), ex=settings.city_index_ttl_seconds)
        except Exception:
            pass
    return doctors
